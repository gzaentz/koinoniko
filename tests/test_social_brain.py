from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from http import HTTPStatus
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.categorization import XPostLLMCategorizer, parse_x_category_response
from leadpoet_social_brain.content_queue import create_post_x, plan_week_x, post_queue_item_x
from leadpoet_social_brain.cli import main as cli_main
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContentQueueItem, DraftRequest, EngagementMetrics, PostNote, SocialPost, TrendItem, utc_now
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
from leadpoet_social_brain.openrouter_provider import OpenRouterProvider, create_provider
from leadpoet_social_brain.platforms.x_api import (
    MISSING_X_WRITE_CREDENTIALS_MESSAGE,
    XApiClient,
    XPostedTweet,
    XWriteClient,
    normalize_x_api_post,
    sync_x_metrics,
    sync_x_user_posts,
)
from leadpoet_social_brain.providers import load_provider_from_env
from leadpoet_social_brain.providers import resolve_model_routing
from leadpoet_social_brain.skills import generation_skills, load_skill
from leadpoet_social_brain.storage.sqlite_store import (
    DB_ENV_VAR,
    RAILWAY_VOLUME_MOUNT_ENV_VAR,
    SQLiteStore,
    resolve_db_path,
)
from leadpoet_social_brain.trend_scout import WebTrendGroups, refresh_web_trends, web_trend_groups
from leadpoet_social_brain.web.app import (
    CREATE_POST_MISSING_PROVIDER_MESSAGE,
    DEFAULT_AUTH_USERNAME,
    MISSING_X_READ_CREDENTIALS_MESSAGE,
    SESSION_COOKIE,
    TASK_AUTH_CONFIG_MESSAGE,
    TASK_AUTH_REQUIRED_MESSAGE,
    TASK_SECRET_CONFIG_MESSAGE,
    TASK_SECRET_ENV,
    TASK_SECRET_REQUIRED_MESSAGE,
    TASK_TOKEN_ENV,
    auth_is_configured,
    create_session_cookie_value,
    handle_api_request,
    make_handler,
    request_has_task_secret,
    request_has_task_token,
    request_is_authenticated,
    validate_session_cookie_value,
    verify_credentials,
)
from leadpoet_social_brain.web.task_client import TASK_DEFAULTS, main as task_client_main


class FakeProvider:
    def __init__(self, texts: list[str] | None = None):
        self.texts = texts or [
            "Leadpoet gets useful when sales AI knows why this account cares today.",
            "The better sales system will win on timing, proof, and buyer context.",
            "More outbound is easy. Better judgment is the part that compounds.",
        ]
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        texts = list(self.texts)
        while len(texts) < variants:
            texts.append(f"Provider draft {len(texts) + 1}: Leadpoet should make buyer timing obvious.")
        return texts[:variants]


class SequentialProvider:
    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        selected = self.texts[:variants]
        self.texts = self.texts[variants:]
        return selected


class RoleAwareProvider:
    def __init__(self):
        self.calls: list[dict[str, str | int | None]] = []

    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        self.calls.append({"prompt": prompt, "variants": variants, "model": model})
        if "Provider smoke test" in prompt:
            return ["OK"]
        if "Do not write the post yet" in prompt:
            return [
                json.dumps(
                    {
                        "core_point": "Sales AI needs account judgment, not more volume.",
                        "intended_audience": "founders and sales leaders",
                        "why_timely": "teams are questioning AI SDR volume",
                        "post_angle": "timing beats automation volume",
                        "hook_direction": "plain contrast",
                        "what_to_avoid": "generic AI hype",
                        "success_criteria": "sounds specific and founder-led",
                    }
                )
            ]
        if "Write 3 X draft options for Gavin" in prompt:
            return [
                "Most sales AI still acts like more volume is the product.\n\nThe useful version knows why this account cares today.",
                "The hard part of sales AI is not sending.\n\nIt is knowing which buyer has a real reason to listen.",
                "A sales LLM should not make outbound louder.\n\nIt should make timing obvious.",
            ][:variants]
        if "Choose the best X post for Gavin" in prompt:
            return [
                json.dumps(
                    {
                        "ranked_drafts": [1, 2, 3],
                        "quality_notes": "Specific, plain, and not corporate.",
                        "warnings": [],
                        "suggested_final_version": (
                            "Most sales AI still acts like more volume is the product.\n\n"
                            "The useful version knows why this account cares today."
                        ),
                    }
                )
            ]
        return ["Default routed draft about buyer timing."][:variants]


class FakeXApiClient:
    def __init__(self, tweets, metrics_by_id=None):
        self.tweets = tweets
        self.metrics_by_id = metrics_by_id or {}
        self.resolved_usernames: list[str] = []
        self.iter_user_posts_calls: list[dict[str, object]] = []

    def resolve_user_id(self, username: str) -> str:
        self.resolved_usernames.append(username)
        return "42"

    def iter_user_posts(self, user_id: str, **kwargs):
        self.iter_user_posts_calls.append(kwargs)
        yield from self.tweets

    def fetch_tweets_by_ids(self, tweet_ids):
        return {
            tweet_id: {"id": tweet_id, "public_metrics": self.metrics_by_id[tweet_id]}
            for tweet_id in tweet_ids
            if tweet_id in self.metrics_by_id
        }


class FakeXWriteClient:
    def __init__(self):
        self.posts: list[str] = []

    def post_tweet(self, text: str) -> XPostedTweet:
        self.posts.append(text)
        return XPostedTweet(
            id=f"posted{len(self.posts)}",
            text=text,
            url=f"https://x.com/i/web/status/posted{len(self.posts)}",
        )


class SocialBrainTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> SQLiteStore:
        return SQLiteStore(Path(tmpdir) / "brain.sqlite3")

    def handler_guard(
        self,
        db_path: Path,
        path: str,
        headers: dict[str, str] | None = None,
        guard: str = "_require_auth",
    ) -> tuple[bool, dict[str, object]]:
        handler_class = make_handler(db_path)
        handler = object.__new__(handler_class)
        handler.path = path
        handler.headers = headers or {}
        captured: dict[str, object] = {}

        def serve_json(status: int, payload: dict[str, object]) -> None:
            captured["status"] = status
            captured["payload"] = payload

        def redirect(location: str, *, extra_headers=None) -> None:
            captured["status"] = 303
            captured["location"] = location
            captured["headers"] = extra_headers or []

        handler._serve_json = serve_json
        handler._redirect = redirect
        return getattr(handler, guard)(), captured

    def test_sqlite_store_resolves_env_db_and_sets_pragmas(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_db = Path(tmpdir) / "volume" / "social_brain.sqlite3"
            explicit_db = Path(tmpdir) / "explicit" / "brain.sqlite3"
            railway_volume = Path(tmpdir) / "railway-volume"
            with patch.dict(
                os.environ,
                {
                    DB_ENV_VAR: str(env_db),
                    RAILWAY_VOLUME_MOUNT_ENV_VAR: str(railway_volume),
                },
                clear=False,
            ):
                self.assertEqual(resolve_db_path(), env_db)
                self.assertEqual(resolve_db_path(explicit_db), explicit_db)

                store = SQLiteStore()
                self.assertEqual(store.path, env_db)
                self.assertTrue(env_db.parent.exists())
                self.assertEqual(store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

            with patch.dict(
                os.environ,
                {RAILWAY_VOLUME_MOUNT_ENV_VAR: str(railway_volume)},
                clear=True,
            ):
                self.assertEqual(
                    resolve_db_path(),
                    railway_volume / "social_brain.sqlite3",
                )
                self.assertEqual(store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                store.close()

                explicit_store = SQLiteStore(explicit_db)
                self.assertEqual(explicit_store.path, explicit_db)
                self.assertTrue(explicit_db.parent.exists())
                explicit_store.close()

    def test_cli_uses_social_brain_db_when_db_arg_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "railway-volume" / "social_brain.sqlite3"
            with patch.dict(os.environ, {DB_ENV_VAR: str(db_path)}, clear=False):
                output = io.StringIO()
                with redirect_stdout(output):
                    cli_main(["init"])

            self.assertTrue(db_path.exists())
            self.assertIn(str(db_path), output.getvalue())

    def test_web_auth_uses_stored_password_hash_and_signed_session_cookie(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            user = store.save_web_auth_user(DEFAULT_AUTH_USERNAME, "test-password")

            self.assertTrue(auth_is_configured(store))
            self.assertNotEqual(user.password_hash, "test-password")
            self.assertTrue(verify_credentials(store, DEFAULT_AUTH_USERNAME, "test-password"))
            self.assertFalse(verify_credentials(store, DEFAULT_AUTH_USERNAME, "wrong-password"))
            self.assertFalse(verify_credentials(store, "other-user", "test-password"))

            token = create_session_cookie_value(store, DEFAULT_AUTH_USERNAME)
            self.assertTrue(validate_session_cookie_value(store, token))
            self.assertTrue(request_is_authenticated({"Cookie": f"{SESSION_COOKIE}={token}"}, store))
            self.assertFalse(request_is_authenticated({"Cookie": f"{SESSION_COOKIE}=tampered"}, store))
            store.close()

    def test_task_auth_uses_bearer_or_task_token_header(self):
        with patch.dict(os.environ, {TASK_TOKEN_ENV: "secret-task-token"}, clear=False):
            self.assertTrue(
                request_has_task_token({"Authorization": "Bearer secret-task-token"})
            )
            self.assertTrue(
                request_has_task_token({"X-Social-Brain-Task-Token": "secret-task-token"})
            )
            self.assertFalse(
                request_has_task_token({"Authorization": "Bearer wrong-token"})
            )

    def test_web_auth_fails_closed_without_stored_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            self.assertFalse(auth_is_configured(store))
            self.assertFalse(verify_credentials(store, DEFAULT_AUTH_USERNAME, "anything"))
            self.assertFalse(request_is_authenticated({"Cookie": ""}, store))
            store.close()

    def test_cli_set_web_login_stores_password_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            output = io.StringIO()
            with redirect_stdout(output):
                cli_main(
                    [
                        "--db",
                        str(db_path),
                        "set-web-login",
                        "--username",
                        DEFAULT_AUTH_USERNAME,
                        "--password",
                        "test-password",
                    ]
                )

            store = SQLiteStore(db_path)
            user = store.get_web_auth_user(DEFAULT_AUTH_USERNAME)
            self.assertIsNotNone(user)
            self.assertNotEqual(user.password_hash, "test-password")
            self.assertTrue(store.verify_web_auth_user(DEFAULT_AUTH_USERNAME, "test-password"))
            self.assertIn(DEFAULT_AUTH_USERNAME, output.getvalue())
            store.close()

    def test_web_static_uses_logo_for_favicon_and_brand(self):
        static_dir = Path("leadpoet_social_brain/web/static")
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        favicon_svg = (static_dir / "koinoniko-favicon.svg").read_text(encoding="utf-8")

        self.assertTrue((static_dir / "koinoniko-logo.svg").exists())
        self.assertTrue((static_dir / "koinoniko-logo.png").exists())
        self.assertTrue((static_dir / "koinoniko-favicon.svg").exists())
        self.assertIn('viewBox="0 0 64 64"', favicon_svg)
        self.assertIn('rx="13"', favicon_svg)
        self.assertIn('fill="#ffffff"', favicon_svg)
        self.assertIn('clip-path="url(#rounded-tile)"', favicon_svg)
        self.assertIn("<title>Koinoniko</title>", index_html)
        self.assertIn('rel="icon" type="image/svg+xml" href="/static/koinoniko-favicon.svg"', index_html)
        self.assertIn('href="/static/styles.css?v=', index_html)
        self.assertIn('src="/static/app.js?v=', index_html)
        self.assertIn('src="/static/koinoniko-logo.png" alt="" class="brand-logo" width="34" height="34"', index_html)
        self.assertIn("<strong>Koinoniko</strong>", index_html)
        self.assertNotIn("<span>Leadpoet</span>", index_html)
        self.assertNotIn("<strong>Social Brain</strong>", index_html)
        self.assertIn('id="sidebarToggle"', index_html)
        self.assertIn("koinonikoSidebarCollapsed", index_html + app_js)
        self.assertIn('data-view="dashboard"', index_html)
        self.assertIn('<span class="nav-label">Dashboard</span>', index_html)
        self.assertIn('class="nav-icon"', index_html)
        self.assertIn('data-view="reviewer"', index_html)
        self.assertIn(">Queue</span>", index_html)
        self.assertIn('data-view="voice"', index_html)
        self.assertIn('<span class="nav-label">Voice</span>', index_html)
        self.assertIn('id="voiceView"', index_html)
        self.assertIn("Koinoniko Voice", index_html)
        self.assertIn("Voice Examples", index_html)
        self.assertIn("Update Voice", index_html)
        self.assertIn('class="voice-status-row"', index_html)
        self.assertIn('id="voiceSuggestionsDisclosure"', index_html)
        self.assertIn("<summary>Show suggested examples</summary>", index_html)
        self.assertNotIn("<h2>Suggested Voice Examples</h2>", index_html)
        self.assertNotIn("<h2>Queue</h2>", index_html)
        self.assertIn('id="reviewNavBadge"', index_html)
        self.assertIn('data-view="account"', index_html)
        self.assertIn('<span class="nav-label">Account</span>', index_html)
        self.assertIn('id="accountView"', index_html)
        self.assertIn('id="accountModel"', index_html)
        self.assertIn('id="planModal"', index_html)
        self.assertIn('id="createPostModal"', index_html)
        self.assertIn('id="reviewAddButton"', index_html)
        self.assertIn('id="reviewAddMenu"', index_html)
        self.assertIn('id="reviewAddPostButton"', index_html)
        self.assertIn('id="reviewAddPlanButton"', index_html)
        self.assertNotIn('id="createPostButton"', index_html)
        self.assertNotIn('id="createPostReviewerButton"', index_html)
        self.assertNotIn('id="createPlanButton"', index_html)
        self.assertNotIn('id="createPlanReviewerButton"', index_html)
        self.assertNotIn('id="openTrendsButton"', index_html)
        self.assertNotIn('id="manageTrendsFromPlanButton"', index_html)
        self.assertIn('id="trendsModal"', index_html)
        self.assertIn('id="trendModal"', index_html)
        self.assertIn('id="trendModalBody"', index_html)
        self.assertIn('id="postModal"', index_html)
        self.assertIn('id="queueModal"', index_html)
        self.assertIn('id="recentPostsTable"', index_html)
        self.assertIn('id="postVoiceFilter"', index_html)
        self.assertIn('id="dailyTrendList"', index_html)
        self.assertIn('id="weeklyTrendList"', index_html)
        self.assertNotIn("All X Posts", index_html)
        self.assertNotIn("Workflow", index_html)
        self.assertNotIn('id="viewTitle"', index_html)
        self.assertNotIn('id="viewMeta"', index_html)
        self.assertNotIn("<h1>Dashboard</h1>", index_html)
        self.assertNotIn("What has been posted, what is current, and what is working.", index_html + app_js)
        self.assertNotIn("Review, improve, approve, and schedule X posts.", index_html + app_js)
        self.assertNotIn("Teach Koinoniko what Gavin's best posts sound like.", index_html + app_js)
        self.assertNotIn("Review, improve, approve, schedule.", index_html)
        self.assertNotIn('id="openTrendsDashboardButton"', index_html)
        self.assertNotIn('id="refreshTrendsButton"', index_html)
        self.assertNotIn("Performance Signals", index_html)
        self.assertNotIn("What is working", index_html)
        self.assertIn("Refresh Trends", index_html)
        self.assertNotIn('id="syncPostsButton"', index_html)
        self.assertNotIn('id="syncMetricsButton"', index_html)
        self.assertNotIn('id="dashboardSyncLine"', index_html)
        self.assertNotIn("Last X sync", index_html + app_js)
        self.assertIn('class="top-actions"', index_html)
        self.assertIn('id="accountSyncPostsButton"', index_html)
        self.assertIn('id="accountSyncMetricsButton"', index_html)
        self.assertIn('id="accountSyncAllButton"', index_html)
        self.assertIn('id="accountRefreshTrendsButton"', index_html)
        self.assertIn('id="accountSyncGrid"', index_html)
        self.assertNotIn('<span class="eyebrow">Settings</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Data</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Advanced</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Security</span>', index_html)
        self.assertNotIn("Services Koinoniko uses to generate, sync, publish, and refresh context.", index_html)
        self.assertNotIn("Pull posts created on X, refresh public metrics, and update trend context.", index_html)
        self.assertNotIn("Choose which models power Standard and Best Version generation.", index_html)
        self.assertNotIn("Single-user access for the hosted Koinoniko workspace.", index_html)
        self.assertIn('id="refreshButton"', index_html)
        self.assertIn('id="accountStrategistModel"', index_html)
        self.assertIn('id="accountNaturalWriterModel"', index_html)
        self.assertIn('id="accountEvaluatorModel"', index_html)
        self.assertNotIn('id="accountModelMeta"', index_html)
        self.assertNotIn("Model names depend on your provider", index_html)
        self.assertNotIn("Default:", app_js)
        self.assertNotIn("Strategist:", app_js)
        self.assertNotIn("Writer:", app_js)
        self.assertNotIn("Evaluator:", app_js)
        self.assertNotIn('id="resetAccountModelsButton"', index_html)
        self.assertNotIn('id="testProviderButton"', index_html)
        self.assertNotIn("Reset to Env Defaults", index_html)
        self.assertNotIn("Test Provider", index_html)
        self.assertIn('id="planQuality"', index_html)
        self.assertIn("Improve with AI", app_js)
        self.assertIn("Generate Best Version", app_js)
        self.assertIn("Make More Natural", app_js)
        self.assertIn("Make More Founder-Led", app_js)
        self.assertIn("/api/content/create-post", app_js)
        self.assertIn("No posts to review.", app_js)
        self.assertIn("saveAccount", app_js)
        self.assertIn("/api/account/sync-x-posts", app_js)
        self.assertIn("/api/account/sync-x-metrics", app_js)
        self.assertIn("/api/account/sync-x-all", app_js)
        self.assertIn("/api/voice/status", app_js)
        self.assertIn("/api/voice/update", app_js)
        self.assertIn("/api/voice/examples", app_js)
        self.assertIn("/api/voice/current", app_js)
        self.assertIn("/api/voice/suggestions", app_js)
        self.assertIn("/api/taxonomy/topics", app_js)
        self.assertIn("/api/taxonomy/post-types", app_js)
        self.assertIn("/api/posts/", app_js)
        self.assertIn("Mark as Voice Example", app_js)
        self.assertIn("Why this worked", app_js)
        self.assertIn("What made this post sound like Gavin and work with the audience?", app_js)
        self.assertIn("compact-meta-row", app_js)
        self.assertIn("compact-metrics-row", app_js)
        self.assertIn("postFieldControl", app_js)
        self.assertIn("postTypeOptions", app_js)
        self.assertNotIn("postVoiceTopic", app_js)
        self.assertNotIn("postVoicePostType", app_js)
        self.assertIn("auto_categorize", app_js)
        self.assertIn("Recategorized", app_js)
        self.assertIn("/api/trends/refresh", app_js)
        self.assertIn("connectionRow", app_js)
        self.assertIn("status-badge", app_js)
        self.assertIn("openTrendDetails", app_js)
        self.assertIn("Stored Source Detail", app_js)
        self.assertIn("escapeHtml(topicLabel(item.topic || \"\"))", app_js)
        self.assertIn("detail(\"Topic\", topicLabel(trend.topic || \"Unknown\"))", app_js)
        self.assertNotIn('id="addTrendButton"', index_html)
        self.assertIn("/api/publish-due", app_js)
        self.assertIn("/api/plan-week", app_js)
        self.assertNotIn("Railway variables", index_html)
        self.assertNotIn("Railway variables", app_js)
        self.assertNotIn("X_BEARER_TOKEN", app_js)
        self.assertNotIn("OPENROUTER_API_KEY", app_js)
        self.assertNotIn("imitate=true", index_html + app_js)
        self.assertNotIn("worked_reason", index_html + app_js)
        self.assertNotIn("rebuild-style-guide", index_html + app_js)
        self.assertNotIn("style guide", (index_html + app_js).lower())
        self.assertIn("Scheduled Content", index_html)
        self.assertNotIn('id="statusBlock"', index_html)
        self.assertNotIn('id="attentionList"', index_html)
        self.assertNotIn('id="scheduleSnapshot"', index_html)
        self.assertNotIn('id="analyticsDetails"', index_html)
        self.assertNotIn("AI Edit Panel", index_html)
        self.assertNotIn("Weekly Planner", index_html)
        self.assertNotIn("Content Queue", index_html)
        self.assertNotIn('id="analyticsView"', index_html)

    def test_web_static_serves_versioned_assets_without_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            handler_class = make_handler(db_path)
            handler = object.__new__(handler_class)
            handler.path = "/static/app.js?v=test"
            handler.wfile = io.BytesIO()
            captured: dict[str, object] = {"headers": []}

            def send_response(status: int) -> None:
                captured["status"] = status

            def send_header(key: str, value: str) -> None:
                captured["headers"].append((key, value))

            handler.send_response = send_response
            handler.send_header = send_header
            handler.end_headers = lambda: None
            handler.send_error = lambda status: captured.update({"status": status})

            handler._serve_static()

        self.assertEqual(captured["status"], HTTPStatus.OK)
        self.assertGreater(len(handler.wfile.getvalue()), 100)
        self.assertIn(("Cache-Control", "no-cache, no-store, must-revalidate"), captured["headers"])

    def test_openrouter_provider_uses_env_and_parses_variants(self):
        captured: dict[str, object] = {}

        def fake_request(url, payload, headers):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return {
                "choices": [
                    {
                        "message": {
                            "content": "First draft about buyer timing.\n---\nSecond draft about sales context."
                        }
                    }
                ]
            }

        provider = OpenRouterProvider(
            api_key="test-key",
            model="anthropic/claude-3.5-sonnet",
            temperature=0.6,
            max_tokens=700,
            site_url="https://leadpoet.com",
            app_name="Leadpoet Social Brain",
            request_json=fake_request,
        )

        drafts = provider.generate("Write X posts.", variants=2)

        self.assertEqual(drafts, ["First draft about buyer timing.", "Second draft about sales context."])
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        payload = captured["payload"]
        headers = captured["headers"]
        self.assertEqual(payload["model"], "anthropic/claude-3.5-sonnet")
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["max_tokens"], 700)
        self.assertIn("Return exactly 2 variants", payload["messages"][0]["content"])
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["HTTP-Referer"], "https://leadpoet.com")
        self.assertEqual(headers["X-Title"], "Leadpoet Social Brain")

        drafts = provider.generate("Write X posts.", variants=1, model="openai/gpt-4.1-mini")
        self.assertEqual(drafts, ["First draft about buyer timing.", "Second draft about sales context."][:1])
        self.assertEqual(captured["payload"]["model"], "openai/gpt-4.1-mini")

    def test_openrouter_create_provider_requires_key_and_allows_model_later(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "openai/gpt-4.1-mini",
                "OPENROUTER_TEMPERATURE": "0.7",
                "OPENROUTER_MAX_TOKENS": "900",
            },
            clear=False,
        ):
            provider = create_provider()

        self.assertEqual(provider.api_key, "test-key")
        self.assertEqual(provider.model, "openai/gpt-4.1-mini")
        self.assertEqual(provider.temperature, 0.7)
        self.assertEqual(provider.max_tokens, 900)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}, clear=False):
            with self.assertRaises(ValueError):
                create_provider()

        provider_without_model = OpenRouterProvider(api_key="test-key", model=None, request_json=lambda *_: {})
        with self.assertRaises(ValueError):
            provider_without_model.generate("Write.", variants=1)

    def test_provider_loader_uses_saved_model_override(self):
        with patch.dict(
            os.environ,
            {
                "SOCIAL_BRAIN_PROVIDER": "leadpoet_social_brain.openrouter_provider:create_provider",
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "openai/gpt-4.1-mini",
                "SOCIAL_BRAIN_MODEL": "",
            },
            clear=False,
        ):
            provider = load_provider_from_env(model_override="anthropic/claude-sonnet-4.5")
            self.assertEqual(os.environ.get("SOCIAL_BRAIN_MODEL"), "")

        self.assertEqual(provider.model, "anthropic/claude-sonnet-4.5")

    def test_provider_loader_accepts_builtin_openrouter_aliases(self):
        aliases = [
            "openrouter_provider:create_provider",
            "openrouter:create_provider",
            "openrouter",
        ]
        for alias in aliases:
            with self.subTest(alias=alias):
                with patch.dict(
                    os.environ,
                    {
                        "SOCIAL_BRAIN_PROVIDER": alias,
                        "OPENROUTER_API_KEY": "test-key",
                        "OPENROUTER_MODEL": "openai/gpt-4.1-mini",
                        "SOCIAL_BRAIN_MODEL": "",
                    },
                    clear=False,
                ):
                    provider = load_provider_from_env()
                self.assertIsInstance(provider, OpenRouterProvider)
                self.assertEqual(provider.model, "openai/gpt-4.1-mini")

    def test_model_settings_save_load_and_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.save_app_setting("model.default", "account/default")
            store.save_app_setting("model.strategist", "account/strategist")
            with patch.dict(
                os.environ,
                {
                    "SOCIAL_BRAIN_DEFAULT_MODEL": "env/default",
                    "SOCIAL_BRAIN_NATURAL_WRITER_MODEL": "env/writer",
                    "SOCIAL_BRAIN_EVALUATOR_MODEL": "env/evaluator",
                },
                clear=False,
            ):
                routing = resolve_model_routing(store)

            self.assertEqual(routing.default, "account/default")
            self.assertEqual(routing.strategist, "account/strategist")
            self.assertEqual(routing.natural_writer, "env/writer")
            self.assertEqual(routing.evaluator, "env/evaluator")
            store.close()

    def test_role_specific_blank_values_fall_back_to_default_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.save_app_setting("model.default", "account/default")
            routing = resolve_model_routing(store)

            self.assertEqual(routing.strategist, "account/default")
            self.assertEqual(routing.natural_writer, "account/default")
            self.assertEqual(routing.evaluator, "account/default")
            store.close()

    def test_model_routing_uses_provider_default_when_db_and_env_are_blank(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = OpenRouterProvider(api_key="test-key", model="provider/default", request_json=lambda *_: {})
            with patch.dict(
                os.environ,
                {
                    "SOCIAL_BRAIN_DEFAULT_MODEL": "",
                    "SOCIAL_BRAIN_STRATEGIST_MODEL": "",
                    "SOCIAL_BRAIN_NATURAL_WRITER_MODEL": "",
                    "SOCIAL_BRAIN_EVALUATOR_MODEL": "",
                    "SOCIAL_BRAIN_MODEL": "",
                    "OPENROUTER_MODEL": "",
                },
                clear=False,
            ):
                routing = resolve_model_routing(store, provider)

            self.assertEqual(routing.default, "provider/default")
            self.assertEqual(routing.strategist, "provider/default")
            store.close()

    def test_skill_files_load_correctly(self):
        skill = load_skill("global_voice.md")
        x_skills = generation_skills(platform="x", post_type="founder_insight")
        paths = [asset.path for asset in x_skills]
        bittensor_paths = [asset.path for asset in generation_skills(platform="x", post_type="bittensor_update")]

        self.assertEqual(skill.path, "skills/global_voice.md")
        self.assertIn("Write like Gavin personally wrote it.", skill.content)
        self.assertEqual(len(skill.sha256), 64)
        self.assertEqual(
            paths,
            [
                "skills/global_voice.md",
                "skills/platforms/x.md",
                "skills/post_types/founder_insight.md",
                "skills/review_filter.md",
            ],
        )
        self.assertIn("skills/post_types/bittensor_update.md", bittensor_paths)

    def test_generated_prompt_includes_relevant_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="context beats generic automation",
                )
            )

            self.assertIn("## skills/global_voice.md", prompt)
            self.assertIn("# Global Voice", prompt)
            self.assertIn("## skills/platforms/x.md", prompt)
            self.assertIn("# X Platform", prompt)
            self.assertIn("## skills/post_types/founder_insight.md", prompt)
            self.assertIn("# Founder Insight", prompt)
            self.assertIn("## skills/review_filter.md", prompt)
            self.assertIn("# Review Filter", prompt)
            store.close()

    def test_generation_prompt_is_example_first_not_label_heavy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            ingest_x_post(
                store,
                text="Most sales AI is still mail merge with better adjectives.",
                url="https://x.com/gavin/status/101",
                date="2026-06-20",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=1000, likes=40, replies=5, bookmarks=15),
                worked_reason="It named a weak default, then gave a sharper replacement model in plain language.",
                hook="enemy_hook",
                structure="enemy_then_better_model",
                tone="plainspoken",
                imitate=True,
            )

            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="account context beats generic automation",
                )
            )

            self.assertIn("Study these Gavin/Leadpoet examples first:", prompt)
            self.assertIn("Example:\nMost sales AI is still mail merge", prompt)
            self.assertIn("Why it worked:\nIt named a weak default", prompt)
            self.assertIn("Performance:\n1000 impressions", prompt)
            self.assertNotIn("Labels:", prompt)
            self.assertLess(prompt.index("Study these Gavin/Leadpoet examples first:"), prompt.index("Writing and review instructions:"))
            store.close()

    def test_ingest_x_post_stores_metrics_and_notes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            post = ingest_x_post(
                store,
                text="Most sales AI is mail merge with nicer adjectives.",
                url="https://x.com/gavin/status/1",
                date="2026-06-25",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(
                    impressions=1000,
                    likes=20,
                    replies=3,
                    reposts=4,
                    quotes=1,
                    bookmarks=12,
                ),
                worked_reason="Clear enemy and sharper model.",
                pattern_name="enemy_then_better_model",
                hook="enemy_hook",
                structure="enemy_then_better_model",
                tone="plainspoken",
                cta="none",
                formatting="single_paragraph",
                imitate=True,
            )

            metrics = store.latest_metrics_by_post_id([post.id])[post.id]
            notes = store.notes_by_post_id([post.id])[post.id]
            influences = store.list_post_influences([post.id])

            self.assertEqual(metrics.engagement_rate, 0.04)
            self.assertTrue(notes.imitate)
            self.assertEqual(notes.pattern_name, "enemy_then_better_model")
            self.assertIn("enemy_hook", {influence.name for influence in influences})
            store.close()

    def test_generation_fails_without_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()

            with self.assertRaises(ValueError) as direct_error:
                SocialBrainGenerator(store).generate(
                    DraftRequest(
                        platform="x",
                        topic="sales LLMs",
                        post_type="founder_insight",
                        angle="context beats generic automation",
                    ),
                    variants=1,
                )
            self.assertEqual(str(direct_error.exception), MISSING_PROVIDER_MESSAGE)

            with patch.dict(os.environ, {"SOCIAL_BRAIN_PROVIDER": ""}, clear=False):
                with self.assertRaises(SystemExit) as cli_error:
                    cli_main(
                        [
                            "--db",
                            str(Path(tmpdir) / "cli.sqlite3"),
                            "generate-x",
                            "--topic",
                            "sales LLMs",
                            "--post-type",
                            "founder_insight",
                        ]
                    )
            self.assertEqual(str(cli_error.exception), MISSING_PROVIDER_MESSAGE)
            store.close()

    def test_generator_uses_provider_and_preserves_context_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = FakeProvider()
            generator = SocialBrainGenerator(store, provider=provider)

            drafts = generator.generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="context beats generic automation",
                ),
                variants=2,
            )

            self.assertEqual(len(drafts), 2)
            self.assertEqual(len({draft.text for draft in drafts}), 2)
            self.assertTrue(all(len(draft.text) <= 280 for draft in drafts))
            self.assertTrue(all("—" not in draft.text for draft in drafts))
            self.assertTrue(all(draft.explanation.startswith("Influenced by") for draft in drafts))
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("Writing and review instructions:", provider.prompts[0])
            saved_draft = store.get_generated_draft(drafts[0].id)
            self.assertIsNotNone(saved_draft)
            self.assertEqual(saved_draft.text, drafts[0].text)
            self.assertEqual(saved_draft.context_snapshot["generation_mode"], "provider")
            self.assertIn("FakeProvider", saved_draft.context_snapshot["provider_name"])
            self.assertIn("quality_notes", saved_draft.context_snapshot)
            self.assertIn("selected_patterns", saved_draft.context_snapshot)
            self.assertIn("selected_context_sections", saved_draft.context_snapshot)
            self.assertIn("skills", saved_draft.context_snapshot)
            skill_paths = {skill["path"] for skill in saved_draft.context_snapshot["skills"]}
            self.assertIn("skills/global_voice.md", skill_paths)
            self.assertIn("skills/platforms/x.md", skill_paths)
            self.assertIn("skills/post_types/founder_insight.md", skill_paths)
            self.assertTrue(all(skill.get("sha256") for skill in saved_draft.context_snapshot["skills"]))
            store.close()

    def test_standard_generation_uses_default_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            store.save_app_setting("model.default", "openrouter/default")
            provider = RoleAwareProvider()

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight"),
                variants=1,
            )

            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.calls[0]["model"], "openrouter/default")
            self.assertEqual(drafts[0].context_snapshot["quality_mode"], "standard")
            self.assertEqual(drafts[0].context_snapshot["default_model"], "openrouter/default")
            store.close()

    def test_best_version_generation_calls_role_models_and_saves_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            store.save_app_setting("model.default", "openrouter/default")
            store.save_app_setting("model.strategist", "openrouter/strategy")
            store.save_app_setting("model.natural_writer", "openrouter/writer")
            store.save_app_setting("model.evaluator", "openrouter/evaluator")
            provider = RoleAwareProvider()

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats volume",
                ),
                best_version=True,
            )

            self.assertEqual(len(drafts), 1)
            self.assertEqual(
                [call["model"] for call in provider.calls],
                ["openrouter/strategy", "openrouter/writer", "openrouter/evaluator"],
            )
            snapshot = store.get_generated_draft(drafts[0].id).context_snapshot
            self.assertEqual(snapshot["quality_mode"], "best_version")
            self.assertEqual(snapshot["strategist_model"], "openrouter/strategy")
            self.assertEqual(snapshot["natural_writer_model"], "openrouter/writer")
            self.assertEqual(snapshot["evaluator_model"], "openrouter/evaluator")
            self.assertIn("strategy_summary", snapshot)
            self.assertIn("quality_notes", snapshot)
            self.assertIn("Most sales AI", drafts[0].text)
            store.close()

    def test_style_guide_rebuild_is_writing_guide_not_tag_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            ingest_x_post(
                store,
                text="Most sales AI is still mail merge with better adjectives.",
                url="https://x.com/gavin/status/201",
                date="2026-06-20",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=2000, likes=80),
                worked_reason="Names the weak default and gives a sharper model.",
                structure="enemy_then_better_model",
                imitate=True,
            )

            section = ContextManager(store).rebuild_style_guide_from_examples()

            self.assertIn("Koinoniko Voice", section.content)
            self.assertIn("Reusable writing principles", section.content)
            self.assertNotIn("Repeated patterns to consider", section.content)
            store.close()

    def test_plan_week_x_creates_review_queue_with_trend_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            trend = store.save_trend_item(
                TrendItem(
                    title="Sales teams shift budget from volume to signal quality",
                    source_type="news",
                    topic="sales LLMs",
                    summary="Teams are questioning high-volume outbound workflows.",
                    why_it_matters="It supports Leadpoet's view that timing beats volume.",
                    leadpoet_angle="Buyer timing is becoming the real sales AI edge.",
                    suggested_hooks="The AI SDR debate is really a timing debate.",
                    date=utc_now(),
                )
            )
            provider = FakeProvider(["Timing is the actual sales AI moat. Volume is just the old stack moving faster."])
            generator = SocialBrainGenerator(store, provider=provider)

            result = plan_week_x(
                store,
                generator,
                week_start="2026-06-29",
                posts=1,
                use_trends=True,
                trend_window_days=30,
                topic_mix="sales LLMs:1",
                cadence="daily_mornings",
                default_post_type="founder_insight",
                variants_per_slot=1,
            )
            queued = store.list_content_queue(status="needs_review")
            saved_draft = store.get_generated_draft(queued[0].draft_id)

            self.assertEqual(len(result.queued), 1)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].topic, "sales LLMs")
            self.assertEqual(queued[0].trend_ids, [trend.id])
            self.assertIn("Why worth publishing", queued[0].reviewer_notes)
            self.assertEqual(saved_draft.context_snapshot["selected_trends"][0]["id"], trend.id)
            store.close()

    def test_plan_week_x_requires_evergreen_opt_in_when_trends_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()

            with self.assertRaises(ValueError) as error:
                plan_week_x(
                    store,
                    SocialBrainGenerator(store, provider=FakeProvider()),
                    week_start="2026-06-29",
                    posts=1,
                    use_trends=True,
                    trend_window_days=7,
                    topic_mix="sales LLMs:1",
                    cadence="daily_mornings",
                    default_post_type="founder_insight",
                    variants_per_slot=1,
                )

            self.assertIn("No recent unused trends available", str(error.exception))
            store.close()

    def test_create_post_x_creates_needs_review_queue_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = FakeProvider(["Sales AI needs buyer timing, not a bigger pile of generated emails."])
            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=provider),
                instruction="Write about why sales AI needs buyer timing, not just more generated emails.",
                topic="Leadpoet",
                post_type="founder_insight",
                scheduled_at="2026-06-30T10:00",
                extra_context="Keep it plain and founder-led.",
            )

            queued = store.list_content_queue(status="needs_review")
            saved_draft = store.get_generated_draft(result.item.draft_id)

            self.assertEqual(result.item.status, "needs_review")
            self.assertEqual(result.item.scheduled_at, "2026-06-30T10:00")
            self.assertEqual(result.item.topic, "Leadpoet")
            self.assertEqual(result.item.post_type, "founder_insight")
            self.assertIn("buyer timing", result.item.angle)
            self.assertIn("Quality mode: Standard", result.item.reviewer_notes)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].id, result.item.id)
            self.assertEqual(saved_draft.context_snapshot["quality_mode"], "standard")
            self.assertIn("User instruction: Write about why sales AI needs buyer timing", provider.prompts[0])
            store.close()

    def test_create_post_x_warns_and_continues_without_trends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=FakeProvider(["Evergreen post about buyer timing."])),
                instruction="Make the buyer timing point.",
                topic="Leadpoet",
                post_type="founder_insight",
                use_trends=True,
            )

            self.assertEqual(result.warnings, ["No relevant unused trends found. Created an evergreen post instead."])
            self.assertEqual(result.item.status, "needs_review")
            self.assertEqual(result.item.trend_ids, [])
            store.close()

    def test_update_and_approve_queued_post_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Draft text",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                )
            )
            store.close()

            with redirect_stdout(io.StringIO()):
                cli_main(
                    [
                        "--db",
                        str(db_path),
                        "update-queued-post",
                        "--queue-id",
                        item.id,
                        "--text",
                        "Edited text",
                        "--notes",
                        "Looks good.",
                    ]
                )
                cli_main(
                    [
                        "--db",
                        str(db_path),
                        "approve-queued-post",
                        "--queue-id",
                        item.id,
                    ]
                )

            store = SQLiteStore(db_path)
            updated = store.get_content_queue_item(item.id)

            self.assertEqual(updated.text, "Edited text")
            self.assertEqual(updated.topic, "sales LLMs")
            self.assertEqual(updated.status, "approved")
            self.assertIsNotNone(updated.approved_at)
            store.close()

    def test_web_queue_listing_update_and_approve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Draft text",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                )
            )
            store.close()

            status, listed = handle_api_request("GET", "/api/queue?status=needs_review", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(listed["items"][0]["id"], item.id)

            status, updated = handle_api_request(
                "PATCH",
                f"/api/queue/{item.id}",
                json.dumps({"text": "Sharper draft text", "reviewer_notes": "Ready after one edit."}),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(updated["item"]["text"], "Sharper draft text")

            status, approved = handle_api_request("POST", f"/api/queue/{item.id}/approve", b"{}", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(approved["item"]["status"], "approved")

            store = SQLiteStore(db_path)
            versions = store.list_queued_post_versions(item.id)
            saved = store.get_content_queue_item(item.id)
            self.assertEqual(saved.status, "approved")
            self.assertEqual(versions[0].text, "Draft text")
            store.close()

    def test_web_mutation_endpoint_requires_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.save_web_auth_user(DEFAULT_AUTH_USERNAME, "test-password")
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Do not approve without a session.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                )
            )
            store.close()

            allowed, captured = self.handler_guard(
                db_path,
                f"/api/queue/{item.id}/approve",
            )

            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 401)
            self.assertEqual(captured["payload"]["error"], "Authentication required.")
            store = SQLiteStore(db_path)
            saved = store.get_content_queue_item(item.id)
            self.assertEqual(saved.status, "needs_review")
            store.close()

    def test_task_endpoint_requires_configured_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch.dict(os.environ, {TASK_TOKEN_ENV: ""}, clear=False):
                allowed, captured = self.handler_guard(
                    db_path,
                    "/api/tasks/weekly-report-x",
                    guard="_require_task_auth",
                )
            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 503)
            self.assertEqual(captured["payload"]["error"], TASK_AUTH_CONFIG_MESSAGE)

            with patch.dict(os.environ, {TASK_TOKEN_ENV: "secret-task-token"}, clear=False):
                allowed, captured = self.handler_guard(
                    db_path,
                    "/api/tasks/weekly-report-x",
                    guard="_require_task_auth",
                )
            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 401)
            self.assertEqual(captured["payload"]["error"], TASK_AUTH_REQUIRED_MESSAGE)

    def test_task_endpoint_weekly_report_uses_token_and_post_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.save_post(
                SocialPost(
                    platform="x",
                    text="Sales AI only works when it knows why this buyer cares today.",
                    url="https://x.com/gavin/status/taskweekly1",
                    published_at=utc_now(),
                    author="Gavin",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    status="published",
                )
            )
            store.close()

            with patch.dict(os.environ, {TASK_TOKEN_ENV: "secret-task-token"}, clear=False):
                allowed, captured = self.handler_guard(
                    db_path,
                    "/api/tasks/weekly-report-x",
                    {"Authorization": "Bearer secret-task-token"},
                    guard="_require_task_auth",
                )
                self.assertTrue(allowed)
                self.assertEqual(captured, {})

                status, payload = handle_api_request(
                    "GET",
                    "/api/tasks/weekly-report-x",
                    db_path=db_path,
                )
                self.assertEqual(status, 404)

                status, payload = handle_api_request(
                    "POST",
                    "/api/tasks/weekly-report-x",
                    json.dumps({"since_days": 30}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertIn("Weekly X report: last 30 days", payload["report"])
            self.assertIn("sales LLMs", payload["report"])

    def test_task_endpoint_sync_x_user_can_auto_categorize(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "taskcat123",
                        "text": "A subnet should make the incentive obvious.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    }
                ]
            )
            provider = FakeProvider([
                '{"topic":"Bittensor","post_type":"bittensor_update","confidence":0.9,"reason":"Subnet post."}'
            ])

            with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                    status, payload = handle_api_request(
                        "POST",
                        "/api/tasks/sync-x-user",
                        json.dumps(
                            {
                                "username": "gavinzaentz",
                                "limit": 100,
                                "auto_categorize": True,
                            }
                        ),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["imported"], 1)
            self.assertEqual(payload["result"]["topics"]["Bittensor"], 1)
            store = SQLiteStore(db_path)
            post = store.list_posts(platform="x", limit=1)[0]
            self.assertEqual(post.topic, "Bittensor")
            self.assertEqual(post.post_type, "bittensor_update")
            store.close()

    def test_web_llm_edit_does_not_overwrite_until_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Original queued post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                )
            )
            store.close()

            provider = FakeProvider(["Sales AI should know why this account cares today."])
            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                status, payload = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/revise",
                    json.dumps({"instruction": "Make it more specific.", "variants": 1}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(len(payload["versions"]), 1)
            self.assertIn("Original queued post.", provider.prompts[0])

            store = SQLiteStore(db_path)
            unchanged = store.get_content_queue_item(item.id)
            self.assertEqual(unchanged.text, "Original queued post.")
            self.assertEqual(store.list_queued_post_versions(item.id)[0].text, payload["versions"][0]["text"])
            store.close()

    def test_web_accept_revision_updates_queue_post(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Original approved post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            store.close()

            with patch(
                "leadpoet_social_brain.web.app.load_provider_from_env",
                return_value=FakeProvider(["Accepted revision about buyer timing."]),
            ):
                status, revisions = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/revise",
                    json.dumps({"instruction": "Tighten it.", "variants": 1}),
                    db_path=db_path,
                )
            self.assertEqual(status, 200)
            version_id = revisions["versions"][0]["id"]

            status, accepted = handle_api_request(
                "POST",
                f"/api/queue/{item.id}/versions/{version_id}",
                b"{}",
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(accepted["item"]["text"], "Accepted revision about buyer timing.")
            self.assertEqual(accepted["item"]["status"], "needs_review")
            self.assertIsNone(accepted["item"]["approved_at"])

    def test_web_best_version_revision_does_not_auto_approve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            store.save_app_setting("model.default", "openrouter/default")
            store.save_app_setting("model.strategist", "openrouter/strategy")
            store.save_app_setting("model.natural_writer", "openrouter/writer")
            store.save_app_setting("model.evaluator", "openrouter/evaluator")
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Original approved post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            store.close()

            provider = RoleAwareProvider()
            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                status, revisions = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/revise",
                    json.dumps(
                        {
                            "instruction": "Generate the strongest final version.",
                            "variants": 1,
                            "best_version": True,
                        }
                    ),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(len(revisions["versions"]), 1)
            self.assertEqual(
                [call["model"] for call in provider.calls],
                ["openrouter/strategy", "openrouter/writer", "openrouter/evaluator"],
            )
            store = SQLiteStore(db_path)
            unchanged = store.get_content_queue_item(item.id)
            self.assertEqual(unchanged.status, "approved")
            self.assertEqual(unchanged.text, "Original approved post.")
            self.assertIsNotNone(unchanged.approved_at)
            store.close()

    def test_web_create_post_endpoint_creates_review_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            store.close()

            provider = FakeProvider(["Buyer timing is the real sales AI advantage. More email is not strategy."])
            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                status, payload = handle_api_request(
                    "POST",
                    "/api/content/create-post",
                    json.dumps(
                        {
                            "instruction": "Write about buyer timing, not generated email volume.",
                            "topic": "Leadpoet",
                            "post_type": "founder_insight",
                            "scheduled_at": "2026-06-30T10:00",
                            "best_version": False,
                        }
                    ),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "needs_review")
            self.assertEqual(payload["generated_text"], "Buyer timing is the real sales AI advantage. More email is not strategy.")
            self.assertEqual(payload["item"]["status"], "needs_review")
            self.assertEqual(payload["item"]["scheduled_at"], "2026-06-30T10:00")
            self.assertEqual(payload["item"]["quality_mode"], "standard")
            self.assertIn("buyer timing", payload["item"]["focus"].lower())
            self.assertEqual(payload["queue_id"], payload["item"]["id"])

            store = SQLiteStore(db_path)
            saved = store.get_content_queue_item(payload["queue_id"])
            self.assertEqual(saved.status, "needs_review")
            self.assertIsNone(saved.approved_at)
            store.close()

    def test_web_create_post_endpoint_returns_clear_missing_provider_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=None):
                status, payload = handle_api_request(
                    "POST",
                    "/api/content/create-post",
                    json.dumps({"instruction": "Write a post about buyer timing."}),
                    db_path=db_path,
                )

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], CREATE_POST_MISSING_PROVIDER_MESSAGE)

    def test_web_analytics_endpoint_returns_expected_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            ingest_x_post(
                store,
                text="The sentence is the easy part. The timing is the work.",
                url="https://x.com/gavin/status/webanalytics",
                date="2026-06-25",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=2000, likes=80, replies=5, reposts=8),
                hook="specific_observation",
                structure="claim_then_list",
                tone="plainspoken",
                cta="none",
                formatting="short_paragraphs",
                imitate=True,
            )
            store.close()

            status, payload = handle_api_request("GET", "/api/analytics", db_path=db_path)

            self.assertEqual(status, 200)
            for key in ["by_post_type", "by_topic", "by_hook", "by_structure", "by_tone", "by_cta", "by_formatting", "hints"]:
                self.assertIn(key, payload)
            self.assertEqual(payload["by_hook"][0]["key"], "specific_observation")

    def test_web_dashboard_returns_recent_posts_metrics_and_trends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Sales AI only works when it knows why this account cares today. Generic automation misses timing.",
                url="https://x.com/gavin/status/dashboardrecent",
                date="2026-06-25T12:00:00+00:00",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=2500, likes=100, replies=8, reposts=12, quotes=3, bookmarks=15),
                hook="specific_observation",
                structure="claim_then_reason",
                tone="plainspoken",
                cta="none",
                formatting="short_paragraphs",
                imitate=True,
            )
            trend = store.save_trend_item(
                TrendItem(
                    title="AI sales teams move from volume to timing",
                    source_type="web_daily",
                    topic="sales LLMs",
                    summary="More teams are evaluating sales AI by account context instead of volume.",
                    why_it_matters="It supports the Leadpoet positioning.",
                    leadpoet_angle="Tie buyer timing to sales LLM usefulness.",
                    urgency="high",
                    date=utc_now(),
                )
            )
            store.close()

            status, payload = handle_api_request("GET", "/api/dashboard", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(payload["recent_posts"][0]["id"], post.id)
            self.assertIn("Sales AI only works", payload["recent_posts"][0]["summary"])
            self.assertEqual(payload["recent_posts"][0]["metrics"]["likes"], 100)
            self.assertEqual(payload["recent_posts"][0]["metrics"]["impressions"], 2500)
            self.assertEqual(payload["trends"][0]["id"], trend.id)
            self.assertEqual(payload["trend_groups"]["daily"][0]["id"], trend.id)
            self.assertIn("analytics_hints", payload)

    def test_web_can_update_recent_post_topic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Buyer timing beats more generated emails.",
                url="https://x.com/gavin/status/manualtopic",
                date="2026-06-26T12:00:00+00:00",
                author="Gavin",
                topic="uncategorized",
                post_type="quote_tweet",
            )
            store.close()

            status, payload = handle_api_request(
                "PATCH",
                f"/api/posts/{post.id}",
                json.dumps({"topic": "Sales"}),
                db_path=db_path,
            )

            self.assertEqual(status, 200)
            self.assertEqual(payload["post"]["topic"], "Sales")
            dashboard_status, dashboard = handle_api_request("GET", "/api/dashboard", db_path=db_path)
            self.assertEqual(dashboard_status, 200)
            self.assertEqual(dashboard["recent_posts"][0]["topic"], "Sales")

    def test_web_taxonomy_options_include_existing_values_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            ingest_x_post(
                store,
                text="Sales AI needs buyer timing.",
                url="https://x.com/gavin/status/taxonomy1",
                date="2026-06-26T12:00:00+00:00",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
            )
            store.save_content_queue_item(
                ContentQueueItem(
                    text="A queued technical post.",
                    topic="technical depth",
                    post_type="technical_explanation",
                    scheduled_at="2026-06-27T12:00:00+00:00",
                )
            )
            store.close()

            topic_status, topics = handle_api_request("GET", "/api/taxonomy/topics", db_path=db_path)
            type_status, post_types = handle_api_request("GET", "/api/taxonomy/post-types", db_path=db_path)

            self.assertEqual(topic_status, 200)
            self.assertEqual(type_status, 200)
            topic_values = {item["value"] for item in topics["items"]}
            type_values = {item["value"] for item in post_types["items"]}
            self.assertIn("sales LLMs", topic_values)
            self.assertIn("technical depth", topic_values)
            self.assertIn("Leadpoet", topic_values)
            self.assertIn("Bittensor", topic_values)
            self.assertIn("founder/company", topic_values)
            self.assertIn("uncategorized", topic_values)
            self.assertIn("founder_insight", type_values)
            self.assertIn("technical_explanation", type_values)
            self.assertIn("quote_tweet", type_values)
            self.assertIn("launch", type_values)
            self.assertIn("investor_facing", type_values)

    def test_web_can_update_post_topic_and_type_without_clearing_voice_note(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="The best sales AI knows why this buyer cares today.",
                url="https://x.com/gavin/status/draweredit1",
                date="2026-06-26T12:00:00+00:00",
                author="Gavin",
                topic="uncategorized",
                post_type="founder_insight",
            )
            store.save_note(
                PostNote(
                    post_id=post.id,
                    worked_reason="Clear contrast in Gavin's voice.",
                    hook="specific_observation",
                    structure="claim_then_reason",
                    tone="plainspoken",
                    cta="none",
                    formatting="single_paragraph",
                    pattern_name="buyer_timing",
                    imitate=True,
                )
            )
            store.close()

            status, payload = handle_api_request(
                "PATCH",
                f"/api/posts/{post.id}",
                json.dumps({"topic": "sales LLMs", "post_type": "quote_tweet"}),
                db_path=db_path,
            )

            self.assertEqual(status, 200)
            updated = payload["post"]
            self.assertEqual(updated["topic"], "sales LLMs")
            self.assertEqual(updated["post_type"], "quote_tweet")
            self.assertTrue(updated["voice"]["is_voice_example"])
            self.assertEqual(updated["voice"]["why_this_worked"], "Clear contrast in Gavin's voice.")
            self.assertEqual(updated["voice"]["hook"], "specific_observation")
            self.assertEqual(updated["voice"]["pattern"], "buyer_timing")

    def test_web_trend_scout_refreshes_daily_and_weekly_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"{timespan} AI sales trend {index}",
                        "url": f"https://example.com/{timespan}/{index}",
                        "seendate": "20260626120000",
                        "sourceCommonName": "Example News",
                    }
                    for index in range(7)
                ]

            refreshed = refresh_web_trends(store, fetch_articles=fake_fetch)
            grouped = web_trend_groups(store, now=datetime(2026, 6, 26, 12, 30, tzinfo=timezone.utc))

            self.assertEqual(len(refreshed.daily), 5)
            self.assertEqual(len(refreshed.weekly), 5)
            self.assertEqual(len(grouped.daily), 5)
            self.assertEqual(len(grouped.weekly), 5)
            self.assertTrue(all(trend.source_type == "web_daily" for trend in grouped.daily))
            self.assertTrue(all(trend.source_type == "web_weekly" for trend in grouped.weekly))
            self.assertTrue(all(trend.id.startswith("trend_web_daily_") for trend in grouped.daily))
            store.close()

    def test_web_trends_refresh_endpoint_returns_daily_and_weekly_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            def fake_refresh(store: SQLiteStore):
                daily = store.save_trend_item(
                    TrendItem(
                        title="Daily AI sales signal",
                        source_type="web_daily",
                        topic="sales AI",
                        summary="Daily signal.",
                        why_it_matters="It may affect sales workflows.",
                        leadpoet_angle="Connect it to buyer timing.",
                        date="2026-06-26T12:00:00+00:00",
                    )
                )
                weekly = store.save_trend_item(
                    TrendItem(
                        title="Weekly lead generation signal",
                        source_type="web_weekly",
                        topic="lead generation",
                        summary="Weekly signal.",
                        why_it_matters="It may affect pipeline strategy.",
                        leadpoet_angle="Connect it to context.",
                        date="2026-06-26T12:00:00+00:00",
                    )
                )
                return WebTrendGroups(daily=[daily], weekly=[weekly])

            with patch("leadpoet_social_brain.web.app.refresh_web_trends", side_effect=fake_refresh):
                status, payload = handle_api_request("POST", "/api/trends/refresh", b"{}", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(payload["trend_groups"]["daily"][0]["title"], "Daily AI sales signal")
            self.assertEqual(payload["trend_groups"]["weekly"][0]["title"], "Weekly lead generation signal")

    def test_refresh_trends_task_saves_daily_and_weekly_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            groups = WebTrendGroups(
                daily=[
                    TrendItem(
                        title="Daily AI sales signal",
                        source_type="web_daily",
                        topic="sales AI",
                        summary="Daily signal.",
                        why_it_matters="It may affect sales workflows.",
                        leadpoet_angle="Connect it to buyer timing.",
                    )
                ],
                weekly=[
                    TrendItem(
                        title="Weekly lead generation signal",
                        source_type="web_weekly",
                        topic="lead generation",
                        summary="Weekly signal.",
                        why_it_matters="It may affect pipeline strategy.",
                        leadpoet_angle="Connect it to context.",
                    )
                ],
                errors=["weekly GDELT fallback failed: HTTP 429 Too Many Requests"],
                sources=["Scrapingdog Google Trends"],
            )

            with patch("leadpoet_social_brain.web.app.refresh_web_trends", return_value=groups):
                status, payload = handle_api_request(
                    "POST",
                    "/tasks/refresh-trends",
                    json.dumps({"limit": 5}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["daily_trends"], 1)
            self.assertEqual(payload["weekly_trends"], 1)
            self.assertEqual(payload["errors"], groups.errors)
            self.assertEqual(payload["sync"]["status"], "partial")
            store = SQLiteStore(db_path)
            metadata = store.get_sync_metadata("trends_sync")
            self.assertEqual(metadata.status, "partial")
            self.assertEqual(metadata.summary["daily_trends"], 1)
            store.close()

    def test_web_dashboard_returns_all_posts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            for index in range(101):
                ingest_x_post(
                    store,
                    text=f"Stored dashboard post {index}",
                    url=f"https://x.com/gavin/status/allposts{index}",
                    date=f"2026-01-{(index % 28) + 1:02d}T12:00:00+00:00",
                    author="Gavin",
                    topic="Leadpoet",
                    post_type="founder_insight",
                )
            store.close()

            status, payload = handle_api_request("GET", "/api/dashboard", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(len(payload["recent_posts"]), 101)

    def test_web_sync_x_user_endpoint_can_auto_categorize(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "webcat123",
                        "text": "The best sales AI starts with why this buyer cares today.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 10, "reply_count": 1},
                    }
                ]
            )
            provider = FakeProvider([
                '{"topic":"sales LLMs","post_type":"founder_insight","confidence":0.9,"reason":"Sales AI post."}'
            ])

            with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                    status, payload = handle_api_request(
                        "POST",
                        "/api/sync-x-user",
                        json.dumps(
                            {
                                "username": "gavinzaentz",
                                "limit": 100,
                                "auto_categorize": True,
                            }
                        ),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["imported"], 1)
            self.assertEqual(payload["result"]["topics"]["sales LLMs"], 1)
            store = SQLiteStore(db_path)
            post = store.list_posts(platform="x", limit=1)[0]
            self.assertEqual(post.topic, "sales LLMs")
            self.assertEqual(post.post_type, "founder_insight")
            store.close()

    def test_account_status_endpoint_reports_sync_stats(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            ingest_x_post(
                store,
                text="A post created on X should still teach the system.",
                url="https://x.com/gavinzaentz/status/accountstatus1",
                date="2026-06-25T12:00:00+00:00",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
            )
            store.close()

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token", TASK_SECRET_ENV: "task-secret"}, clear=False):
                status, payload = handle_api_request("GET", "/api/account/status", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertTrue(payload["status"]["x_read_configured"])
            self.assertTrue(payload["status"]["daily_sync_configured"])
            self.assertEqual(payload["stats"]["total_x_posts_stored"], 1)
            self.assertEqual(payload["stats"]["latest_imported_x_post_date"], "2026-06-25T12:00:00+00:00")
            self.assertEqual(payload["sync"]["x_posts_sync"]["status"], "not_run")

    def test_account_status_reports_scrapingdog_connected_without_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "secret-scrapingdog-key"}, clear=False):
                status, payload = handle_api_request("GET", "/api/account/status", db_path=db_path)

            serialized = json.dumps(payload)
            self.assertEqual(status, 200)
            self.assertTrue(payload["status"]["scrapingdog_configured"])
            self.assertEqual(payload["connections"]["scrapingdog"]["status"], "connected")
            self.assertIn("Trend source", payload["connections"]["scrapingdog"]["description"])
            self.assertNotIn("secret-scrapingdog-key", serialized)
            self.assertNotIn("SCRAPINGDOG_API_KEY", serialized)

    def test_account_status_reports_scrapingdog_missing_and_supports_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "", "SCRAPING_DOG_API_KEY": ""}, clear=False):
                status, missing = handle_api_request("GET", "/api/account/status", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertFalse(missing["status"]["scrapingdog_configured"])
            self.assertEqual(missing["connections"]["scrapingdog"]["status"], "not_connected")

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "", "SCRAPING_DOG_API_KEY": "alias-key"}, clear=False):
                status, connected = handle_api_request("GET", "/api/account/status", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertTrue(connected["status"]["scrapingdog_configured"])
            self.assertEqual(connected["connections"]["scrapingdog"]["status"], "connected")
            self.assertNotIn("alias-key", json.dumps(connected))

    def test_voice_status_examples_update_and_current_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Most sales AI is still mail merge with better adjectives.",
                url="https://x.com/gavin/status/voice101",
                date="2026-06-20",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=2000, likes=80, replies=8, bookmarks=20),
            )
            store.close()

            status, payload = handle_api_request("GET", "/api/voice/status", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "needs_examples")
            self.assertEqual(payload["examples_count"], 0)

            status, saved = handle_api_request(
                "POST",
                f"/api/posts/{post.id}/voice",
                json.dumps(
                    {
                        "voice_example": True,
                        "why_this_worked": "Names the weak default and gives a sharper model.",
                        "topic": "sales LLMs",
                        "post_type": "founder_insight",
                    }
                ),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved["message"], "Saved as a Voice Example.")
            self.assertTrue(saved["post"]["voice"]["is_voice_example"])
            self.assertEqual(saved["post"]["voice"]["why_this_worked"], "Names the weak default and gives a sharper model.")

            status, payload = handle_api_request("GET", "/api/voice/status", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(payload["examples_count"], 1)
            self.assertEqual(payload["status"], "not_updated_yet")

            status, updated = handle_api_request("POST", "/api/voice/update", b"{}", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(updated["message"], "Voice updated from selected examples.")
            self.assertEqual(updated["examples_used"], 1)
            self.assertTrue(updated["current"]["exists"])
            self.assertIn("Reusable writing principles", {section["title"] for section in updated["current"]["sections"]})
            self.assertEqual(updated["status"]["status"], "ready")

            status, current = handle_api_request("GET", "/api/voice/current", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertTrue(current["exists"])
            self.assertEqual(current["title"], "Koinoniko Voice")

    def test_voice_save_preserves_unspecified_note_fields_and_can_unmark(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Useful sales AI starts with buyer timing.",
                url="https://x.com/gavin/status/voice102",
                date="2026-06-20",
                author="Gavin",
                topic="Leadpoet",
                post_type="founder_insight",
            )
            store.save_note(
                PostNote(
                    post_id=post.id,
                    worked_reason="Original reason.",
                    hook="specific_observation",
                    structure="contrast",
                    tone="plainspoken",
                    cta="none",
                    formatting="single_paragraph",
                    pattern_name="enemy_then_better_model",
                    imitate=True,
                )
            )
            store.close()

            status, payload = handle_api_request(
                "POST",
                f"/api/posts/{post.id}/voice",
                json.dumps({"voice_example": False, "why_this_worked": "Updated reason."}),
                db_path=db_path,
            )

            self.assertEqual(status, 200)
            voice = payload["post"]["voice"]
            self.assertFalse(voice["is_voice_example"])
            self.assertEqual(voice["why_this_worked"], "Updated reason.")
            self.assertEqual(voice["hook"], "specific_observation")
            self.assertEqual(voice["structure"], "contrast")
            self.assertEqual(voice["tone"], "plainspoken")
            self.assertEqual(voice["cta"], "none")
            self.assertEqual(voice["formatting"], "single_paragraph")
            self.assertEqual(voice["pattern"], "enemy_then_better_model")

    def test_voice_examples_and_suggestions_exclude_selected_examples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            selected = ingest_x_post(
                store,
                text="The product point is that context beats volume.",
                url="https://x.com/gavin/status/voice201",
                date="2026-06-21",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=5000, likes=100, replies=10, bookmarks=30),
                worked_reason="Plain contrast.",
                imitate=True,
            )
            suggested = ingest_x_post(
                store,
                text="The best AI SDR does less guessing.",
                url="https://x.com/gavin/status/voice202",
                date="2026-06-22",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=8000, likes=120, replies=20, bookmarks=60),
            )
            store.close()

            status, examples = handle_api_request("GET", "/api/voice/examples", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in examples["items"]], [selected.id])
            self.assertEqual(examples["items"][0]["why_this_worked"], "Plain contrast.")

            status, suggestions = handle_api_request("GET", "/api/voice/suggestions", db_path=db_path)
            self.assertEqual(status, 200)
            suggestion_ids = {item["id"] for item in suggestions["items"]}
            self.assertIn(suggested.id, suggestion_ids)
            self.assertNotIn(selected.id, suggestion_ids)
            self.assertIn("reason", suggestions["items"][0])

    def test_post_detail_and_dashboard_payload_include_voice_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Founder-led content should make one useful point.",
                url="https://x.com/gavin/status/voice301",
                date="2026-06-23",
                author="Gavin",
                topic="founder content",
                post_type="founder_insight",
                worked_reason="Feels direct and useful.",
                imitate=True,
            )
            store.close()

            status, detail = handle_api_request("GET", f"/api/posts/{post.id}", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertTrue(detail["post"]["voice"]["is_voice_example"])
            self.assertEqual(detail["post"]["voice"]["why_this_worked"], "Feels direct and useful.")

            status, dashboard = handle_api_request("GET", "/api/dashboard", db_path=db_path)
            self.assertEqual(status, 200)
            recent = dashboard["recent_posts"][0]
            self.assertTrue(recent["voice"]["is_voice_example"])

    def test_generation_prompt_labels_rebuilt_guide_as_voice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            ingest_x_post(
                store,
                text="Most sales AI is just volume. Useful sales AI starts with timing.",
                url="https://x.com/gavin/status/voice401",
                date="2026-06-24",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                worked_reason="Sharp contrast.",
                imitate=True,
            )
            ContextManager(store).rebuild_style_guide_from_examples()
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats volume",
                )
            )

            self.assertIn("Koinoniko Voice:", prompt)
            self.assertNotIn("Observed style guide:", prompt)
            store.close()

    def test_account_sync_x_posts_endpoint_imports_defaults_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "accountsync1",
                        "text": "This post was created directly on X.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 20, "reply_count": 2, "impression_count": 1000},
                    }
                ]
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    status, payload = handle_api_request(
                        "POST",
                        "/api/account/sync-x-posts",
                        json.dumps({"limit": 100}),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["imported"], 1)
            self.assertEqual(payload["sync"]["status"], "success")

            store = SQLiteStore(db_path)
            posts = store.list_posts(platform="x", limit=10)
            metrics = store.latest_metrics_by_post_id([posts[0].id])
            metadata = store.get_sync_metadata("x_posts_sync")
            self.assertEqual(posts[0].topic, "Leadpoet")
            self.assertEqual(posts[0].post_type, "founder_insight")
            self.assertNotIn(posts[0].id, store.notes_by_post_id([posts[0].id]))
            self.assertEqual(metrics[posts[0].id].likes, 20)
            self.assertEqual(metadata.status, "success")
            store.close()

    def test_account_sync_x_posts_can_auto_categorize_and_update_default_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            existing = ingest_x_post(
                store,
                text="Sales AI should know why this buyer cares today.",
                url="https://x.com/gavinzaentz/status/accountcat1",
                date="2026-06-24T12:00:00+00:00",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
            )
            store.close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "accountcat1",
                        "text": "Sales AI should know why this buyer cares today.",
                        "created_at": "2026-06-24T12:00:00.000Z",
                    },
                    {
                        "id": "accountcat2",
                        "text": "Subnet incentives on Bittensor need to make miner behavior obvious.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    },
                ]
            )
            provider = SequentialProvider(
                [
                    '{"topic":"sales LLMs","post_type":"founder_insight","confidence":0.91,"reason":"Sales AI post."}',
                    '{"topic":"Bittensor","post_type":"bittensor_update","confidence":0.92,"reason":"Subnet incentives."}',
                ]
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token", "SOCIAL_BRAIN_PROVIDER": "openrouter"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                        status, payload = handle_api_request(
                            "POST",
                            "/api/account/sync-x-posts",
                            json.dumps({"limit": 100, "auto_categorize": True}),
                            db_path=db_path,
                        )

            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["imported"], 1)
            self.assertEqual(payload["result"]["duplicates"], 1)
            self.assertEqual(payload["result"]["recategorized"], 1)
            self.assertEqual(payload["result"]["topics"]["sales LLMs"], 1)
            self.assertEqual(payload["result"]["topics"]["Bittensor"], 1)
            store = SQLiteStore(db_path)
            recategorized = store.get_post(existing.id)
            self.assertIsNotNone(recategorized)
            self.assertEqual(recategorized.topic, "sales LLMs")
            self.assertEqual(recategorized.post_type, "founder_insight")
            imported = store.get_post_by_x_post_id("accountcat2")
            self.assertIsNotNone(imported)
            self.assertEqual(imported.topic, "Bittensor")
            self.assertEqual(imported.post_type, "bittensor_update")
            store.close()

    def test_account_sync_x_metrics_endpoint_appends_metrics_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="Metrics refresh post.",
                url="https://x.com/gavinzaentz/status/metricrefresh1",
                date="2026-06-25T12:00:00+00:00",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
                metrics=EngagementMetrics(likes=1, replies=0, impressions=100),
            )
            store.close()
            fake_client = FakeXApiClient(
                [],
                metrics_by_id={
                    "metricrefresh1": {
                        "like_count": 50,
                        "reply_count": 4,
                        "retweet_count": 3,
                        "impression_count": 2000,
                    }
                },
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    status, payload = handle_api_request(
                        "POST",
                        "/api/account/sync-x-metrics",
                        json.dumps({"limit": 100}),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["result"]["updated"], 1)
            store = SQLiteStore(db_path)
            snapshots = store.metrics_for_post(post.id)
            metadata = store.get_sync_metadata("x_metrics_sync")
            self.assertEqual(len(snapshots), 2)
            self.assertIn(50, [snapshot.likes for snapshot in snapshots])
            self.assertEqual(metadata.summary["updated"], 1)
            store.close()

    def test_account_sync_x_all_runs_posts_then_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "syncall1",
                        "text": "Sync all should import this direct X post.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 3, "impression_count": 90},
                    }
                ],
                metrics_by_id={
                    "syncall1": {"like_count": 9, "reply_count": 1, "impression_count": 300}
                },
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    status, payload = handle_api_request(
                        "POST",
                        "/api/account/sync-x-all",
                        json.dumps({"limit": 100}),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["posts"]["imported"], 1)
            self.assertEqual(payload["metrics"]["updated"], 1)
            store = SQLiteStore(db_path)
            self.assertIsNotNone(store.get_sync_metadata("x_posts_sync"))
            self.assertIsNotNone(store.get_sync_metadata("x_metrics_sync"))
            store.close()

    def test_account_sync_missing_x_bearer_token_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch.dict(os.environ, {"X_BEARER_TOKEN": ""}, clear=False):
                status, payload = handle_api_request(
                    "POST",
                    "/api/account/sync-x-posts",
                    json.dumps({"limit": 100}),
                    db_path=db_path,
                )

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], MISSING_X_READ_CREDENTIALS_MESSAGE)
            store = SQLiteStore(db_path)
            self.assertEqual(store.get_sync_metadata("x_posts_sync").status, "failed")
            store.close()

    def test_daily_sync_task_requires_task_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            with patch.dict(os.environ, {TASK_SECRET_ENV: ""}, clear=False):
                allowed, captured = self.handler_guard(
                    db_path,
                    "/tasks/sync-x-daily",
                    guard="_require_task_secret",
                )
            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 503)
            self.assertEqual(captured["payload"]["error"], TASK_SECRET_CONFIG_MESSAGE)

            with patch.dict(os.environ, {TASK_SECRET_ENV: "daily-secret"}, clear=False):
                allowed, captured = self.handler_guard(
                    db_path,
                    "/tasks/sync-x-daily",
                    guard="_require_task_secret",
                )
            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 401)
            self.assertEqual(captured["payload"]["error"], TASK_SECRET_REQUIRED_MESSAGE)

            with patch.dict(os.environ, {TASK_SECRET_ENV: "daily-secret"}, clear=False):
                self.assertTrue(request_has_task_secret({"Authorization": "Bearer daily-secret"}))

    def test_daily_sync_imports_without_duplicates_and_updates_dashboard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "daily1",
                        "text": "Daily sync should pick up direct X posts.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 7, "reply_count": 1, "impression_count": 500},
                    }
                ],
                metrics_by_id={
                    "daily1": {"like_count": 8, "reply_count": 2, "impression_count": 600}
                },
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token", TASK_SECRET_ENV: "daily-secret"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    first_status, first_payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-daily",
                        b"{}",
                        db_path=db_path,
                    )
                    second_status, second_payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-daily",
                        b"{}",
                        db_path=db_path,
                    )

            self.assertEqual(first_status, 200)
            self.assertEqual(first_payload["new_posts_imported"], 1)
            self.assertEqual(first_payload["metrics_updated"], 1)
            self.assertEqual(second_status, 200)
            self.assertEqual(second_payload["new_posts_imported"], 0)
            self.assertEqual(second_payload["duplicates_skipped"], 1)

            store = SQLiteStore(db_path)
            self.assertEqual(store.count_posts(platform="x"), 1)
            self.assertEqual(store.get_sync_metadata("x_daily_sync").status, "success")
            self.assertEqual(store.get_sync_metadata("x_posts_sync").summary["duplicates"], 1)
            store.close()

            dashboard_status, dashboard = handle_api_request("GET", "/api/dashboard", db_path=db_path)
            self.assertEqual(dashboard_status, 200)
            self.assertEqual(dashboard["recent_posts"][0]["url"], "https://x.com/gavinzaentz/status/daily1")
            self.assertEqual(dashboard["recent_posts"][0]["metrics"]["likes"], 8)

    def test_task_sync_posts_and_metrics_endpoints_can_run_separately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "frequent1",
                        "text": "Frequent sync should keep the dashboard current.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 4, "reply_count": 0, "impression_count": 200},
                    }
                ],
                metrics_by_id={
                    "frequent1": {"like_count": 12, "reply_count": 2, "impression_count": 900}
                },
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    posts_status, posts_payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-posts",
                        json.dumps({"limit": 100}),
                        db_path=db_path,
                    )
                    metrics_status, metrics_payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-metrics",
                        json.dumps({"since_days": 90, "limit": 100}),
                        db_path=db_path,
                    )
                    second_posts_status, second_posts_payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-posts",
                        json.dumps({"limit": 100}),
                        db_path=db_path,
                    )

            self.assertEqual(posts_status, 200)
            self.assertEqual(posts_payload["new_posts_imported"], 1)
            self.assertEqual(metrics_status, 200)
            self.assertEqual(metrics_payload["metrics_updated"], 1)
            self.assertEqual(second_posts_status, 200)
            self.assertEqual(second_posts_payload["duplicates_skipped"], 1)
            self.assertIsNone(fake_client.iter_user_posts_calls[0].get("since_id"))
            self.assertEqual(fake_client.iter_user_posts_calls[1].get("since_id"), "frequent1")
            self.assertEqual(second_posts_payload["sync"]["summary"]["since_id"], "frequent1")
            store = SQLiteStore(db_path)
            latest_metrics = store.latest_metrics_by_post_id([store.list_posts(platform="x", limit=1)[0].id])
            self.assertEqual(next(iter(latest_metrics.values())).likes, 12)
            self.assertEqual(store.get_sync_metadata("x_posts_sync").status, "success")
            self.assertEqual(store.get_sync_metadata("x_metrics_sync").status, "success")
            store.close()

    def test_task_sync_all_runs_posts_then_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "alltask1",
                        "text": "Combined sync imports the post before refreshing metrics.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 1, "reply_count": 0, "impression_count": 50},
                    }
                ],
                metrics_by_id={
                    "alltask1": {"like_count": 6, "reply_count": 1, "impression_count": 250}
                },
            )

            with patch.dict(os.environ, {"X_BEARER_TOKEN": "read-token"}, clear=False):
                with patch("leadpoet_social_brain.web.app.XApiClient.from_env", return_value=fake_client):
                    status, payload = handle_api_request(
                        "POST",
                        "/tasks/sync-x-all",
                        json.dumps({"post_limit": 100, "metrics_limit": 100}),
                        db_path=db_path,
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["new_posts_imported"], 1)
            self.assertEqual(payload["metrics_updated"], 1)
            store = SQLiteStore(db_path)
            self.assertEqual(store.count_posts(platform="x"), 1)
            self.assertEqual(store.get_sync_metadata("x_posts_sync").summary["imported"], 1)
            self.assertEqual(store.get_sync_metadata("x_metrics_sync").summary["updated"], 1)
            store.close()

    def test_task_client_defaults_support_frequent_railway_cron_jobs(self):
        self.assertEqual(TASK_DEFAULTS["sync-x-posts"], {"limit": 25})
        self.assertEqual(TASK_DEFAULTS["sync-x-metrics"]["since_days"], 30)
        self.assertEqual(TASK_DEFAULTS["sync-x-metrics"]["limit"], 250)
        self.assertEqual(TASK_DEFAULTS["refresh-trends"], {"limit": 5})
        with patch(
            "leadpoet_social_brain.web.task_client.post_task",
            return_value={"ok": True, "errors": []},
        ) as post_task:
            with redirect_stdout(io.StringIO()):
                exit_code = task_client_main(
                    [
                        "sync-x-posts",
                        "--url",
                        "https://example.com",
                        "--secret",
                        "task-secret",
                        "--payload",
                        '{"limit":50}',
                    ]
                )
        self.assertEqual(exit_code, 0)
        post_task.assert_called_once_with(
            "https://example.com",
            "sync-x-posts",
            "task-secret",
            {"limit": 50},
        )

    def test_web_account_saves_resets_and_tests_model_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            env = {
                "SOCIAL_BRAIN_PROVIDER": "leadpoet_social_brain.openrouter_provider:create_provider",
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "openai/gpt-4.1-mini",
            }
            with patch.dict(os.environ, env, clear=True):
                status, saved = handle_api_request(
                    "PATCH",
                    "/api/account",
                    json.dumps(
                        {
                            "models": {
                                "default": "anthropic/claude-sonnet-4.5",
                                "strategist": "openai/gpt-4.1",
                                "natural_writer": "anthropic/claude-opus-4.5",
                                "evaluator": "openai/gpt-4.1-mini",
                            }
                        }
                    ),
                    db_path=db_path,
                )
                self.assertEqual(status, 200)
                self.assertEqual(saved["settings"]["models"]["default"], "anthropic/claude-sonnet-4.5")
                self.assertEqual(saved["settings"]["models"]["strategist"], "openai/gpt-4.1")
                self.assertEqual(saved["settings"]["effective_models"]["natural_writer"], "anthropic/claude-opus-4.5")
                self.assertEqual(saved["status"]["llm_model_source"], "account")
                self.assertFalse(saved["status"]["x_read_configured"])
                self.assertFalse(saved["status"]["x_write_configured"])

                status, account = handle_api_request("GET", "/api/account", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(account["settings"]["effective_llm_model"], "anthropic/claude-sonnet-4.5")

            store = SQLiteStore(db_path)
            self.assertEqual(store.get_app_setting("model.default"), "anthropic/claude-sonnet-4.5")
            self.assertEqual(store.get_app_setting("model.strategist"), "openai/gpt-4.1")
            self.assertIsNone(store.get_app_setting("llm_model"))
            store.close()

            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "leadpoet_social_brain.web.app.load_provider_from_env",
                    return_value=RoleAwareProvider(),
                ):
                    status, provider_payload = handle_api_request(
                        "POST",
                        "/api/account/test-provider",
                        b"{}",
                        db_path=db_path,
                    )
            self.assertEqual(status, 200)
            self.assertTrue(provider_payload["ok"])

            with patch.dict(os.environ, env, clear=True):
                status, reset = handle_api_request(
                    "PATCH",
                    "/api/account",
                    json.dumps({"reset_model_settings": True}),
                    db_path=db_path,
                )
            self.assertEqual(status, 200)
            self.assertEqual(reset["settings"]["models"]["default"], "")
            self.assertEqual(reset["settings"]["effective_models"]["default"], "openai/gpt-4.1-mini")

    def test_web_missing_provider_blocks_ai_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Original queued post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                )
            )
            store.close()

            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=None):
                status, payload = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/revise",
                    json.dumps({"instruction": "Make it better.", "variants": 1}),
                    db_path=db_path,
                )

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], MISSING_PROVIDER_MESSAGE)

    def test_web_missing_write_auth_blocks_posting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Approved post ready to publish.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            store.close()

            with patch.dict(
                os.environ,
                {
                    "X_OAUTH2_ACCESS_TOKEN": "",
                    "X_API_KEY": "",
                    "X_API_SECRET": "",
                    "X_ACCESS_TOKEN": "",
                    "X_ACCESS_TOKEN_SECRET": "",
                },
                clear=False,
            ):
                status, payload = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/post-now",
                    b"{}",
                    db_path=db_path,
                )

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], MISSING_X_WRITE_CREDENTIALS_MESSAGE)

    def test_post_queue_item_requires_approval_and_creates_published_post(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            draft = SocialBrainGenerator(store, provider=FakeProvider()).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="timing beats volume",
                ),
                variants=1,
            )[0]
            unapproved = store.save_content_queue_item(
                ContentQueueItem(
                    draft_id=draft.id,
                    text=draft.text,
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="timing beats volume",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    status="needs_review",
                )
            )
            with self.assertRaises(ValueError):
                post_queue_item_x(store, FakeXWriteClient(), item=unapproved)

            approved = store.save_content_queue_item(
                ContentQueueItem(
                    **{
                        **unapproved.__dict__,
                        "status": "approved",
                        "approved_at": utc_now(),
                    }
                )
            )
            result = post_queue_item_x(store, FakeXWriteClient(), item=approved)
            saved_item = store.get_content_queue_item(approved.id)
            post = store.get_post_by_x_post_id("posted1")

            self.assertIsNone(result.error)
            self.assertEqual(saved_item.status, "posted")
            self.assertEqual(saved_item.x_post_id, "posted1")
            self.assertIsNotNone(post)
            self.assertEqual(post.source_draft_id, draft.id)
            store.close()

    def test_x_write_client_fails_clearly_without_write_credentials(self):
        with patch.dict(
            os.environ,
            {
                "X_OAUTH2_ACCESS_TOKEN": "",
                "X_API_KEY": "",
                "X_API_SECRET": "",
                "X_ACCESS_TOKEN": "",
                "X_ACCESS_TOKEN_SECRET": "",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError) as error:
                XWriteClient.from_env()

        self.assertEqual(str(error.exception), MISSING_X_WRITE_CREDENTIALS_MESSAGE)

    def test_publishing_draft_preserves_source_and_analyzes_influences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            ingest_x_post(
                store,
                text="Most sales AI is still mail merge with better adjectives.",
                url="https://x.com/gavin/status/10",
                date="2026-06-20",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=1000, likes=30, bookmarks=12),
                pattern_name="enemy_then_better_model",
                hook="specific_observation",
                imitate=True,
            )
            draft = SocialBrainGenerator(store, provider=FakeProvider()).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="account context beats generic automation",
                ),
                variants=1,
            )[0]

            post = publish_x_draft(
                store,
                draft_id=draft.id,
                url="https://x.com/gavin/status/20",
                date="2026-06-25",
                author="Gavin",
                metrics=EngagementMetrics(impressions=3000, likes=90, replies=8, reposts=12, bookmarks=35),
            )
            saved_post = store.get_post(post.id)
            influences = store.list_post_influences([post.id])
            summary = build_analytics_summary(store)

            self.assertEqual(saved_post.source_draft_id, draft.id)
            self.assertTrue(any(influence.source_draft_id == draft.id for influence in influences))
            self.assertTrue(summary.by_hook)
            self.assertIn("specific_observation", {row.key for row in summary.by_hook})
            store.close()

    def test_analytics_groups_by_post_type_topic_and_pattern(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            post = ingest_x_post(
                store,
                text="The sentence is the easy part. The timing is the work.",
                url="https://x.com/gavin/status/2",
                date="2026-06-25",
                author="Gavin",
                topic="sales LLMs",
                post_type="technical_explanation",
                metrics=EngagementMetrics(impressions=2000, likes=40, replies=5),
                pattern_name="plain_english_technical",
                hook="specific_observation",
                structure="claim_then_list",
                tone="plain_english_technical",
                cta="none",
                formatting="short_paragraphs",
                imitate=True,
            )

            summary = build_analytics_summary(store)

            self.assertIn(post.id, {example.id for example in summary.best_examples})
            self.assertEqual(summary.by_post_type[0].key, "technical_explanation")
            self.assertEqual(summary.by_topic[0].key, "sales LLMs")
            self.assertIn("plain_english_technical", {row.key for row in summary.by_pattern})
            self.assertEqual(summary.by_hook[0].key, "specific_observation")
            self.assertEqual(summary.by_structure[0].key, "claim_then_list")
            self.assertEqual(summary.by_tone[0].key, "plain_english_technical")
            self.assertEqual(summary.by_cta[0].key, "none")
            self.assertEqual(summary.by_formatting[0].key, "short_paragraphs")
            self.assertGreater(summary.by_hook[0].avg_replies, 0)
            store.close()

    def test_analytics_handles_metrics_without_engagement_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            post_without_rate = ingest_x_post(
                store,
                text="X may return likes without impressions, so engagement rate can be unavailable.",
                url="https://x.com/gavin/status/no-rate",
                date="2026-06-25",
                author="Gavin",
                topic="Leadpoet",
                post_type="founder_insight",
                metrics=EngagementMetrics(likes=40, replies=4),
            )
            post_with_rate = ingest_x_post(
                store,
                text="This post has impressions and a calculable engagement rate.",
                url="https://x.com/gavin/status/with-rate",
                date="2026-06-26",
                author="Gavin",
                topic="Leadpoet",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=1000, likes=30, replies=3),
            )

            summary = build_analytics_summary(store)

            self.assertEqual(summary.by_topic[0].key, "Leadpoet")
            self.assertIn(post_without_rate.id, {example.id for example in summary.best_examples})
            self.assertIn(post_with_rate.id, {example.id for example in summary.best_examples})
            store.close()

    def test_analytics_hints_are_human_readable_when_enough_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(2):
                ingest_x_post(
                    store,
                    text=f"Sales AI post {index}. The sentence is easy. The timing is the work.",
                    url=f"https://x.com/gavin/status/30{index}",
                    date="2026-06-25",
                    author="Gavin",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    metrics=EngagementMetrics(impressions=2000, likes=50, replies=5, reposts=4, bookmarks=20),
                    structure="enemy_then_better_model",
                    hook="specific_observation",
                    tone="plain_english_technical",
                    imitate=True,
                )

            summary = build_analytics_summary(store)
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="context beats generic automation",
                )
            )

            self.assertTrue(summary.hints)
            self.assertTrue(any("weak default" in hint or "concrete observation" in hint for hint in summary.hints))
            self.assertFalse(any(hint.startswith("Best hook") or "plain_english_technical" in hint for hint in summary.hints))
            self.assertIn("Useful performance guidance:", prompt)
            self.assertTrue("weak default" in prompt or "concrete observation" in prompt)
            store.close()

    def test_batch_import_csv_and_long_x_examples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            csv_path = Path(tmpdir) / "x_posts.csv"
            long_text = "Long X example. " * 40
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "text",
                        "url",
                        "date",
                        "author",
                        "topic",
                        "post_type",
                        "impressions",
                        "likes",
                        "replies",
                        "reposts",
                        "quotes",
                        "bookmarks",
                        "worked_reason",
                        "hook",
                        "structure",
                        "tone",
                        "cta",
                        "formatting",
                        "pattern",
                        "imitate",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "text": long_text,
                        "url": "https://x.com/gavin/status/30",
                        "date": "2026-06-20",
                        "author": "Gavin",
                        "topic": "sales LLMs",
                        "post_type": "founder_insight",
                        "impressions": "5000",
                        "likes": "120",
                        "replies": "10",
                        "reposts": "20",
                        "quotes": "3",
                        "bookmarks": "44",
                        "worked_reason": "Long but useful example.",
                        "hook": "enemy_hook",
                        "structure": "enemy_then_better_model",
                        "tone": "plainspoken",
                        "cta": "none",
                        "formatting": "short_paragraphs",
                        "pattern": "enemy_then_better_model",
                        "imitate": "true",
                    }
                )
                writer.writerow(
                    {
                        "text": "A second post with fewer columns filled.",
                        "topic": "product",
                        "post_type": "product_update",
                    }
                )
                writer.writerow({"text": "", "topic": "sales LLMs", "post_type": "founder_insight"})

            result = ingest_x_batch_csv(store, csv_path)
            posts = store.list_posts(platform="x", limit=10)
            summary = build_analytics_summary(store)

            self.assertEqual(result.imported, 2)
            self.assertEqual(result.skipped, 1)
            self.assertTrue(any(len(post.text) > 280 for post in posts))
            self.assertEqual(result.topics["sales LLMs"], 1)
            self.assertIn("enemy_hook", {row.key for row in summary.by_hook})
            self.assertIn("short_paragraphs", {row.key for row in summary.by_formatting})
            store.close()

    def test_x_api_post_normalizes_into_import_fields(self):
        normalized = normalize_x_api_post(
            {
                "id": "12345",
                "text": "The useful version of sales AI knows why this buyer cares now.",
                "created_at": "2026-06-25T12:00:00.000Z",
                "public_metrics": {
                    "impression_count": 1000,
                    "like_count": 50,
                    "reply_count": 4,
                    "retweet_count": 8,
                    "quote_count": 2,
                    "bookmark_count": 12,
                },
            },
            username="@gavinzaentz",
            default_topic="Leadpoet",
            default_post_type="founder_insight",
            imitate=True,
        )

        self.assertEqual(normalized.x_post_id, "12345")
        self.assertEqual(normalized.url, "https://x.com/gavinzaentz/status/12345")
        self.assertEqual(normalized.date, "2026-06-25T12:00:00.000Z")
        self.assertEqual(normalized.author, "gavinzaentz")
        self.assertEqual(normalized.topic, "Leadpoet")
        self.assertEqual(normalized.post_type, "founder_insight")
        self.assertTrue(normalized.imitate)
        self.assertEqual(normalized.metrics.impressions, 1000)
        self.assertEqual(normalized.metrics.likes, 50)
        self.assertEqual(normalized.metrics.replies, 4)
        self.assertEqual(normalized.metrics.reposts, 8)
        self.assertEqual(normalized.metrics.quotes, 2)
        self.assertEqual(normalized.metrics.bookmarks, 12)

    def test_llm_categorizer_sets_topic_and_post_type(self):
        normalized = normalize_x_api_post(
            {
                "id": "cat123",
                "text": "A good subnet is an incentive system, not a Discord with emissions.",
                "created_at": "2026-06-25T12:00:00.000Z",
            },
            username="gavinzaentz",
            default_topic="uncategorized",
            default_post_type="founder_insight",
        )
        provider = FakeProvider([
            '{"topic":"Bittensor","post_type":"bittensor_update","confidence":0.92,"reason":"Subnet incentives."}'
        ])

        categorized = XPostLLMCategorizer(provider).categorize({"referenced_tweets": []}, normalized)

        self.assertEqual(categorized.topic, "Bittensor")
        self.assertEqual(categorized.post_type, "bittensor_update")
        self.assertIn("Return only valid JSON", provider.prompts[0])
        self.assertIn("A good subnet is an incentive system", provider.prompts[0])

    def test_category_response_normalizes_aliases(self):
        parsed = parse_x_category_response(
            '{"topic":"sales","post_type":"technical"}',
            default_topic="uncategorized",
            default_post_type="founder_insight",
        )

        self.assertEqual(parsed["topic"], "sales LLMs")
        self.assertEqual(parsed["post_type"], "technical_explanation")

    def test_x_api_sync_prevents_duplicates_by_url_or_x_post_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            ingest_x_post(
                store,
                text="Already imported through a legacy Twitter URL.",
                url="https://twitter.com/gavinzaentz/status/legacy123?s=20",
                date="2026-06-24T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
            )
            client = FakeXApiClient(
                [
                    {
                        "id": "abc123",
                        "text": "Leadpoet should make buyer timing obvious.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                        "public_metrics": {"like_count": 10},
                    },
                    {
                        "id": "legacy123",
                        "text": "This tweet is already in the system.",
                        "created_at": "2026-06-25T13:00:00.000Z",
                    }
                ]
            )

            first = sync_x_user_posts(
                store,
                client,
                username="gavinzaentz",
                limit=10,
                default_topic="Leadpoet",
                default_post_type="founder_insight",
            )
            second = sync_x_user_posts(
                store,
                client,
                username="gavinzaentz",
                limit=10,
                default_topic="Leadpoet",
                default_post_type="founder_insight",
            )

            self.assertEqual(first.imported, 1)
            self.assertEqual(first.duplicates, 1)
            self.assertEqual(second.imported, 0)
            self.assertEqual(second.duplicates, 2)
            self.assertEqual(len(store.list_posts(platform="x", limit=10)), 2)
            store.close()

    def test_x_api_sync_can_use_llm_categorizer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            client = FakeXApiClient(
                [
                    {
                        "id": "salescat123",
                        "text": "The hard part of sales AI is knowing why this buyer cares now.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    }
                ]
            )
            provider = FakeProvider([
                '{"topic":"sales LLMs","post_type":"founder_insight","confidence":0.96,"reason":"Sales AI post."}'
            ])

            result = sync_x_user_posts(
                store,
                client,
                username="gavinzaentz",
                limit=10,
                default_topic="uncategorized",
                default_post_type="founder_insight",
                categorizer=XPostLLMCategorizer(provider).categorize,
            )
            post = store.list_posts(platform="x", limit=1)[0]

            self.assertEqual(result.imported, 1)
            self.assertEqual(result.topics["sales LLMs"], 1)
            self.assertEqual(post.topic, "sales LLMs")
            self.assertEqual(post.post_type, "founder_insight")
            store.close()

    def test_x_api_client_handles_pagination(self):
        calls = []

        def request_json(path, params):
            calls.append((path, dict(params)))
            if path == "/users/by/username/gavinzaentz":
                return {"data": {"id": "42"}}
            if params.get("pagination_token") == "next-page":
                return {
                    "data": [
                        {"id": "3", "text": "Third post"},
                        {"id": "4", "text": "Fourth post"},
                    ],
                    "meta": {},
                }
            return {
                "data": [
                    {"id": "1", "text": "First post"},
                    {"id": "2", "text": "Second post"},
                ],
                "meta": {"next_token": "next-page"},
            }

        client = XApiClient("token", request_json=request_json)

        user_id = client.resolve_user_id("gavinzaentz")
        tweets = list(
            client.iter_user_posts(
                user_id,
                limit=3,
                exclude_replies=True,
                exclude_retweets=True,
            )
        )

        self.assertEqual(user_id, "42")
        self.assertEqual([tweet["id"] for tweet in tweets], ["1", "2", "3"])
        self.assertTrue(any(params.get("pagination_token") == "next-page" for _, params in calls))
        tweet_calls = [params for path, params in calls if path == "/users/42/tweets"]
        self.assertEqual(tweet_calls[0]["exclude"], "replies,retweets")
        self.assertIn("public_metrics", tweet_calls[0]["tweet.fields"])

    def test_sync_x_user_dry_run_does_not_write_posts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            fake_client = FakeXApiClient(
                [
                    {
                        "id": "dry123",
                        "text": "Dry run post",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    }
                ]
            )
            output = io.StringIO()

            with patch("leadpoet_social_brain.cli.XApiClient.from_env", return_value=fake_client):
                with redirect_stdout(output):
                    cli_main(
                        [
                            "--db",
                            str(db_path),
                            "sync-x-user",
                            "--username",
                            "gavinzaentz",
                            "--limit",
                            "10",
                            "--exclude-replies",
                            "--exclude-retweets",
                            "--default-topic",
                            "Leadpoet",
                            "--default-post-type",
                            "founder_insight",
                            "--dry-run",
                        ]
                    )

            store = SQLiteStore(db_path)
            self.assertEqual(store.list_posts(platform="x", limit=10), [])
            store.close()
            self.assertIn("Dry run X user @gavinzaentz", output.getvalue())
            self.assertIn("Would import: 1", output.getvalue())

    def test_x_api_sync_missing_metrics_does_not_break_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            client = FakeXApiClient(
                [
                    {
                        "id": "nometrics123",
                        "text": "Metrics can arrive later.",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    }
                ]
            )

            result = sync_x_user_posts(
                store,
                client,
                username="gavinzaentz",
                limit=10,
                default_topic="Leadpoet",
                default_post_type="founder_insight",
            )
            posts = store.list_posts(platform="x", limit=10)
            metrics = store.latest_metrics_by_post_id([post.id for post in posts])

            self.assertEqual(result.imported, 1)
            self.assertEqual(result.errors, [])
            self.assertEqual(len(posts), 1)
            self.assertEqual(metrics, {})
            store.close()

    def test_x_api_sync_skips_replies_and_retweets_but_keeps_quotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            client = FakeXApiClient(
                [
                    {
                        "id": "original123",
                        "text": "Original post",
                        "created_at": "2026-06-25T12:00:00.000Z",
                    },
                    {
                        "id": "quote123",
                        "text": "Quote tweet",
                        "created_at": "2026-06-25T12:01:00.000Z",
                        "referenced_tweets": [{"type": "quoted", "id": "quoted1"}],
                    },
                    {
                        "id": "reply123",
                        "text": "Reply that should stay out",
                        "created_at": "2026-06-25T12:02:00.000Z",
                        "referenced_tweets": [{"type": "replied_to", "id": "reply-parent"}],
                    },
                    {
                        "id": "retweet123",
                        "text": "Retweet that should stay out",
                        "created_at": "2026-06-25T12:03:00.000Z",
                        "referenced_tweets": [{"type": "retweeted", "id": "retweeted-parent"}],
                    },
                ]
            )

            result = sync_x_user_posts(
                store,
                client,
                username="gavinzaentz",
                limit=10,
                default_topic="Leadpoet",
                default_post_type="founder_insight",
                exclude_replies=True,
                exclude_retweets=True,
            )
            posts = store.list_posts(platform="x", limit=10)
            urls = {post.url for post in posts}

            self.assertEqual(result.fetched, 4)
            self.assertEqual(result.imported, 2)
            self.assertEqual(result.skipped, 2)
            self.assertIn("https://x.com/gavinzaentz/status/original123", urls)
            self.assertIn("https://x.com/gavinzaentz/status/quote123", urls)
            self.assertNotIn("https://x.com/gavinzaentz/status/reply123", urls)
            self.assertNotIn("https://x.com/gavinzaentz/status/retweet123", urls)
            store.close()

    def test_sync_x_metrics_appends_snapshots_and_reports_movers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            post = ingest_x_post(
                store,
                text="Metrics should get better over time.",
                url="https://x.com/gavinzaentz/status/metrics123",
                date="2026-06-25T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
                metrics=EngagementMetrics(
                    impressions=100,
                    likes=5,
                    replies=1,
                    reposts=1,
                    bookmarks=2,
                    measured_at="2026-06-25T13:00:00+00:00",
                ),
            )
            client = FakeXApiClient(
                [],
                metrics_by_id={
                    "metrics123": {
                        "impression_count": 250,
                        "like_count": 20,
                        "reply_count": 3,
                        "retweet_count": 4,
                        "quote_count": 1,
                        "bookmark_count": 8,
                    }
                },
            )

            result = sync_x_metrics(store, client, limit=10)
            history = store.metrics_for_post(post.id, limit=5)

            self.assertEqual(result.checked, 1)
            self.assertEqual(result.updated, 1)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].impressions, 250)
            self.assertEqual(history[1].impressions, 100)
            self.assertEqual(result.top_movers[0].post_id, post.id)
            self.assertEqual(result.top_movers[0].total_engagement_delta, 27)
            store.close()

    def test_sync_x_metrics_skips_missing_x_post_ids_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            ingest_x_post(
                store,
                text="No URL means no X post id.",
                url=None,
                date="2026-06-25T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
            )

            result = sync_x_metrics(store, FakeXApiClient([]), limit=10)

            self.assertEqual(result.checked, 1)
            self.assertEqual(result.updated, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(result.errors, [])
            store.close()

    def test_analytics_uses_latest_metric_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            post = ingest_x_post(
                store,
                text="Latest snapshot should drive analytics.",
                url="https://x.com/gavinzaentz/status/latest123",
                date="2026-06-25T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
                metrics=EngagementMetrics(
                    impressions=100,
                    likes=1,
                    measured_at="2026-06-25T13:00:00+00:00",
                ),
            )
            store.add_metrics(
                post.id,
                EngagementMetrics(
                    impressions=1000,
                    likes=100,
                    measured_at="2026-06-25T14:00:00+00:00",
                ),
            )

            summary = build_analytics_summary(store)

            self.assertEqual(summary.by_topic[0].key, "Leadpoet")
            self.assertEqual(summary.by_topic[0].avg_impressions, 1000)
            self.assertEqual(summary.by_topic[0].avg_engagement_rate, 0.1)
            store.close()

    def test_update_post_notes_preserves_unspecified_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="A strong post.",
                url="https://x.com/gavinzaentz/status/notes123",
                date="2026-06-25T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
                worked_reason="Original reason.",
                hook="enemy_hook",
                tone="plainspoken",
                imitate=True,
            )
            store.close()

            with redirect_stdout(io.StringIO()):
                cli_main(
                    [
                        "--db",
                        str(db_path),
                        "update-post-notes",
                        "--post-id",
                        post.id,
                        "--worked-reason",
                        "Updated reason.",
                        "--topic",
                        "sales LLMs",
                    ]
                )

            store = SQLiteStore(db_path)
            saved = store.get_post(post.id)
            note = store.notes_by_post_id([post.id])[post.id]

            self.assertEqual(saved.topic, "sales LLMs")
            self.assertEqual(saved.post_type, "founder_insight")
            self.assertEqual(note.worked_reason, "Updated reason.")
            self.assertEqual(note.hook, "enemy_hook")
            self.assertEqual(note.tone, "plainspoken")
            self.assertTrue(note.imitate)
            store.close()

    def test_update_post_notes_can_mark_imitate_true_and_rebuild_style_guide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            post = ingest_x_post(
                store,
                text="Most sales AI sounds busy. The useful version knows when to stay quiet.",
                url="https://x.com/gavinzaentz/status/imitate123",
                date="2026-06-25T12:00:00.000Z",
                author="gavinzaentz",
                topic="Leadpoet",
                post_type="founder_insight",
            )
            store.close()

            with redirect_stdout(io.StringIO()):
                cli_main(
                    [
                        "--db",
                        str(db_path),
                        "update-post-notes",
                        "--post-id",
                        post.id,
                        "--worked-reason",
                        "Strong because it makes a sharp contrast and explains the product point in plain language.",
                        "--imitate",
                        "true",
                        "--topic",
                        "sales LLMs",
                        "--post-type",
                        "founder_insight",
                        "--structure",
                        "enemy_then_better_model",
                    ]
                )

            store = SQLiteStore(db_path)
            note = store.notes_by_post_id([post.id])[post.id]
            section = ContextManager(store).rebuild_style_guide_from_examples()

            self.assertTrue(note.imitate)
            self.assertIn("Strong because it makes a sharp contrast", section.content)
            self.assertIn("Name the weak default", section.content)
            store.close()

    def test_trend_items_manual_batch_generation_and_publish_linkage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            ingest_x_post(
                store,
                text="The hard part of sales AI is not writing the sentence. It is knowing whether the sentence should exist.",
                url="https://x.com/gavin/status/401",
                date="2026-06-20",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=3000, likes=100, replies=8, reposts=12, bookmarks=40),
                worked_reason="Plain technical framing with a concrete business point.",
                imitate=True,
            )
            trend = store.save_trend_item(
                TrendItem(
                    title="AI SDR tools shift from volume to buyer context",
                    source_url="https://example.com/ai-sdr-context",
                    source_type="news",
                    topic="sales LLMs",
                    summary="More teams are questioning high-volume AI SDR workflows.",
                    why_it_matters="Buyers are tuning out generic automation and rewarding timely context.",
                    leadpoet_angle="Leadpoet can argue that sales AI needs buyer timing, not just more generated copy.",
                    suggested_hooks="The AI SDR debate is really a context debate.",
                    urgency="high",
                    date="2026-06-25",
                )
            )

            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="AI SDR context beats volume",
                    use_trends=True,
                )
            )
            draft = SocialBrainGenerator(store, provider=FakeProvider()).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="AI SDR context beats volume",
                    use_trends=True,
                ),
                variants=1,
            )[0]
            post = publish_x_draft(
                store,
                draft_id=draft.id,
                url="https://x.com/gavin/status/402",
                date="2026-06-25",
                author="Gavin",
                metrics=EngagementMetrics(impressions=5000, likes=150, replies=12, reposts=22, bookmarks=55),
            )
            influences = store.list_post_influences([post.id], influence_type="trend")
            summary = build_analytics_summary(store)
            trends = store.list_trends(status="used")

            self.assertIn("Timely market context:", prompt)
            self.assertIn("Trend:\nAI SDR tools shift", prompt)
            self.assertEqual(draft.context_snapshot["selected_trends"][0]["id"], trend.id)
            self.assertEqual(influences[0].ref_id, trend.id)
            self.assertIn("sales LLMs", {row.key for row in summary.by_trend_topic})
            self.assertIn("news", {row.key for row in summary.by_trend_source_type})
            self.assertIn("high", {row.key for row in summary.by_trend_urgency})
            self.assertEqual(trends[0].id, trend.id)
            store.close()

    def test_trend_batch_import_works(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            csv_path = Path(tmpdir) / "trends.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "title",
                        "source_url",
                        "source_type",
                        "topic",
                        "summary",
                        "why_it_matters",
                        "leadpoet_angle",
                        "suggested_hooks",
                        "urgency",
                        "date",
                        "status",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "title": "Bittensor subnet launches new sales eval",
                        "source_url": "https://example.com/subnet",
                        "source_type": "Bittensor_update",
                        "topic": "Bittensor",
                        "summary": "A subnet is testing sales-context evaluation.",
                        "why_it_matters": "It may make sales AI quality easier to price.",
                        "leadpoet_angle": "Useful sales AI needs measurable context quality.",
                        "suggested_hooks": "A useful subnet should make one sales signal better.",
                        "urgency": "medium",
                        "date": "2026-06-25",
                        "status": "unused",
                    }
                )

            result = ingest_trends_batch_csv(store, csv_path)
            trends = store.list_trends(topic="Bittensor", status="unused")

            self.assertEqual(result.imported, 1)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(trends[0].source_type, "Bittensor_update")
            store.close()


if __name__ == "__main__":
    unittest.main()
