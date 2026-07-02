from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.assistant_context import (
    ASSISTANT_MISSING_PROVIDER_MESSAGE,
    build_assistant_context,
    build_assistant_prompt,
)
from leadpoet_social_brain.categorization import XPostLLMCategorizer, parse_x_category_response
from leadpoet_social_brain.content_queue import create_post_x, plan_week_x, post_queue_item_x
from leadpoet_social_brain.cli import (
    build_naturalness_audit,
    build_parser as build_cli_parser,
    leadpoet_actual_syntax,
    leadpoet_bad_phrase_counts,
    leadpoet_mention_pattern,
    leadpoet_sentence_position,
    leadpoet_starts_sentence,
    main as cli_main,
)
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContentQueueItem, ContextSection, DraftRequest, EngagementMetrics, Pattern, PostNote, SocialPost, SyncMetadata, TrendItem, utc_now
from leadpoet_social_brain.generation import (
    MISSING_PROVIDER_MESSAGE,
    SocialBrainGenerator,
    clean_generated_text,
    deterministic_leadpoet_definition_repair,
    leadpoet_anchor_line,
    primary_closing_target,
    primary_human_rhythm_target,
    primary_layout_target,
    primary_product_mention_lane,
    primary_product_mention_placement_target,
    primary_product_mention_syntax_target,
    primary_structure_shape,
    selected_closing_targets,
    selected_human_rhythm_targets,
    selected_layout_targets,
    selected_product_mention_lanes,
    selected_product_mention_placement_targets,
    selected_product_mention_syntax_targets,
    selected_structure_shapes,
    workspace_anchor_candidates,
)
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
from leadpoet_social_brain.openrouter_provider import OpenRouterProvider, create_provider
from leadpoet_social_brain.pattern_scout import (
    PATTERN_REFRESH_MISSING_SCRAPINGDOG,
    PatternRefreshResult,
    add_pattern_example,
    fetch_scrapingdog_pattern_candidates,
    ingest_patterns_batch_csv,
    pattern_context_terms,
    pattern_groups,
    refresh_patterns,
)
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
from leadpoet_social_brain.trend_scout import WebTrendGroups, build_trend_scout_profile, refresh_web_trends, web_trend_groups
from leadpoet_social_brain.voice import (
    DEFAULT_AVOID_PHRASES,
    load_avoid_phrases,
    load_positioning_rules,
    save_avoid_phrases,
    save_positioning_rules,
)
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
    request_workspace_id,
    validate_session_cookie_value,
    verify_credentials,
)
from leadpoet_social_brain.web import task_client
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
                        "thesis": "Sales AI needs account judgment, not more volume.",
                        "angle": "timing beats automation volume",
                        "core_point": "Sales AI needs account judgment, not more volume.",
                        "intended_audience": "founders and sales leaders",
                        "reader_problem": "AI outbound looks polished even when targeting is shallow.",
                        "why_this_is_worth_posting": "teams are questioning AI SDR volume",
                        "concrete_detail": "buyer timing",
                        "source_material": "User request and Leadpoet positioning context.",
                        "evidence_boundary": "Do not imply Gavin personally read examples, spoke to customers, or ran a test.",
                        "post_shape": "observation",
                        "post_angle": "timing beats automation volume",
                        "hook_direction": "plain contrast",
                        "tone_direction": "dry, plainspoken, lightly opinionated",
                        "claims_to_make": ["The useful sales AI edge is account judgment."],
                        "claims_to_avoid": ["AI SDRs are useless."],
                        "forbidden_moves": ["unsupported first-person setup", "fake anecdote", "generic AI hype"],
                        "what_to_avoid": "generic AI hype",
                        "success_criteria": "sounds specific and founder-led",
                    }
                )
            ]
        if "Write 3 X draft options for this workspace" in prompt:
            return [
                "Most sales AI still acts like more volume is the product.\n\nThe useful version knows why this account cares today.",
                "The hard part of sales AI is not sending.\n\nIt is knowing which buyer has a real reason to listen.",
                "A sales LLM should not make outbound louder.\n\nIt should make timing obvious.",
            ][:variants]
        if "Choose the best X post for this workspace" in prompt:
            return [
                json.dumps(
                    {
                        "ranked_drafts": [1, 2, 3],
                        "quality_notes": "Specific, plain, and not corporate.",
                        "warnings": [],
                        "suggested_final_version": (
                            "Most sales AI still acts like more volume is the product.\n\n"
                            "The useful Leadpoet question is why this account cares today."
                        ),
                    }
                )
            ]
        return ["Default routed draft about buyer timing."][:variants]


def strategy_json(**overrides):
    payload = {
        "thesis": "Sales AI needs account judgment, not volume.",
        "angle": "buyer timing beats volume",
        "core_point": "Sales AI needs account judgment, not volume.",
        "intended_audience": "founders and sales leaders",
        "reader_problem": "Outbound tools automate before they know timing.",
        "why_this_is_worth_posting": "AI SDR volume is getting attention.",
        "concrete_detail": "buyer timing",
        "source_material": "User request and Leadpoet context.",
        "source_bridge": "Sales AI trend -> need for buyer timing -> Leadpoet intent layer.",
        "evidence_boundary": "No invented customer stories or private metrics.",
        "post_shape": "observation",
        "hook_direction": "plain contrast",
        "tone_direction": "plain founder-led",
        "claims_to_make": ["Leadpoet centers buyer timing."],
        "claims_to_avoid": ["AI SDRs are useless."],
        "forbidden_moves": ["fake anecdote", "generic AI hype"],
        "success_criteria": "specific, grounded, and tied to Leadpoet.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def evaluation_json(final_text: str, *, warnings: list[str] | None = None):
    return json.dumps(
        {
            "ranked_drafts": [1, 2, 3],
            "quality_notes": "Specific, grounded, and not corporate.",
            "warnings": warnings or [],
            "suggested_final_version": final_text,
        }
    )


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


class FailingXWriteClient:
    def post_tweet(self, text: str) -> XPostedTweet:
        raise RuntimeError("X write failed")


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

    def test_workspace_migration_preserves_legacy_data_and_scopes_unique_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE context_sections (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, name)
                );
                INSERT INTO context_sections
                VALUES ('ctx_legacy', 'writer', 'writer_profile', 'Legacy Gavin context', 2.0, 1, '2026-06-27T00:00:00+00:00', '2026-06-27T00:00:00+00:00');

                CREATE TABLE patterns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    example_post_ids TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO patterns
                VALUES ('pat_legacy', 'plain_claim', 'structure', 'Legacy pattern', '[]', 1, '2026-06-27T00:00:00+00:00', '2026-06-27T00:00:00+00:00');

                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO app_settings
                VALUES ('voice.rules', '["Legacy default voice"]', '2026-06-27T00:00:00+00:00');

                CREATE TABLE sync_metadata (
                    key TEXT PRIMARY KEY,
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT
                );
                INSERT INTO sync_metadata
                VALUES ('x_posts_sync', '2026-06-27T00:00:00+00:00', '2026-06-27T00:01:00+00:00', 'success', '{"imported": 1}', NULL);
                """
            )
            conn.commit()
            conn.close()

            default_store = SQLiteStore(db_path)
            alice_store = SQLiteStore(db_path, workspace_id="user-alice")

            default_sections = default_store.list_context_sections(kinds={"writer"})
            self.assertEqual(default_sections[0].content, "Legacy Gavin context")
            self.assertEqual(default_store.get_app_setting("voice.rules"), '["Legacy default voice"]')
            self.assertEqual(default_store.get_sync_metadata("x_posts_sync").summary["imported"], 1)

            alice_store.save_context_section(
                ContextSection(
                    kind="writer",
                    name="writer_profile",
                    content="Alice workspace context",
                    weight=2.0,
                )
            )
            alice_store.save_pattern(
                Pattern(
                    name="plain_claim",
                    category="structure",
                    description="Alice can reuse the same pattern name independently.",
                )
            )
            alice_store.save_app_setting("voice.rules", '["Alice voice"]')
            alice_store.save_sync_metadata(SyncMetadata(key="x_posts_sync", summary={"imported": 2}))

            self.assertEqual(default_store.list_context_sections(kinds={"writer"})[0].content, "Legacy Gavin context")
            self.assertEqual(alice_store.list_context_sections(kinds={"writer"})[0].content, "Alice workspace context")
            self.assertEqual(default_store.list_patterns()[0].description, "Legacy pattern")
            self.assertEqual(alice_store.list_patterns()[0].description, "Alice can reuse the same pattern name independently.")
            self.assertEqual(default_store.get_app_setting("voice.rules"), '["Legacy default voice"]')
            self.assertEqual(alice_store.get_app_setting("voice.rules"), '["Alice voice"]')
            self.assertEqual(default_store.get_sync_metadata("x_posts_sync").summary["imported"], 1)
            self.assertEqual(alice_store.get_sync_metadata("x_posts_sync").summary["imported"], 2)
            default_store.close()
            alice_store.close()

    def test_store_initialization_can_run_concurrently_without_index_races(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"

            def initialize_store(_index: int) -> tuple[str, ...]:
                store = SQLiteStore(db_path)
                try:
                    rows = store.conn.execute("PRAGMA index_info(idx_pattern_examples_lookup)").fetchall()
                    return tuple(row["name"] for row in rows)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=8) as executor:
                columns = list(executor.map(initialize_store, range(24)))

            self.assertTrue(columns)
            self.assertTrue(all(column_set == columns[0] for column_set in columns))
            self.assertEqual(
                columns[0],
                ("workspace_id", "status", "window", "category", "topic", "collected_at"),
            )

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
        styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
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
        self.assertIn("20260702-queue-generate-loading", index_html)
        self.assertIn("skeleton-card", index_html)
        self.assertIn("skeleton-line", styles_css)
        self.assertIn("renderInitialLoadingState", app_js)
        self.assertIn("renderDashboardLoading", app_js)
        self.assertIn("skeletonTableRows", app_js)
        self.assertIn("@keyframes skeleton-shimmer", styles_css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles_css)
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
        self.assertNotIn("Koinoniko Voice", index_html)
        self.assertNotIn("Koinoniko Voice is current.", index_html)
        self.assertNotIn("voiceStatusDescription", index_html + app_js)
        self.assertNotIn("<h2>Voice Status</h2>", index_html)
        self.assertIn("<h2>Voice</h2>", index_html)
        self.assertNotIn("<h2>Current Voice</h2>", index_html)
        self.assertIn("Voice Examples", index_html)
        self.assertIn("Update Voice", index_html)
        self.assertIn("<h2>Context</h2>", index_html)
        self.assertIn("Facts & Context", index_html)
        self.assertIn('id="voiceFactsButton"', index_html)
        self.assertIn('id="voiceFactsModal"', index_html)
        self.assertIn('id="voiceFactsSaveButton"', index_html)
        self.assertIn("/api/voice/facts", app_js)
        self.assertIn("renderVoiceFacts", app_js)
        self.assertIn('<h2 id="voiceRulesTitle">Rules</h2>', index_html)
        self.assertNotIn("Voice Coverage", index_html)
        self.assertIn("Active Writing Rules", index_html)
        self.assertIn("Writing Guardrails", index_html)
        self.assertIn("View Rules", index_html)
        self.assertIn("Save Rules", index_html)
        self.assertIn('id="voiceCoverageGrid"', index_html)
        self.assertIn('id="voiceRulesButton"', index_html)
        self.assertIn('id="voiceRulesModal"', index_html)
        self.assertIn('id="voiceAvoidPhraseList"', index_html)
        self.assertIn('id="voiceAvoidPhraseInput"', index_html)
        self.assertNotIn('<span class="eyebrow">Voice</span>', index_html)
        self.assertNotIn("How Koinoniko should frame Leadpoet in every post.", index_html + app_js)
        self.assertNotIn("How Gavin's writing should feel.", index_html + app_js)
        self.assertNotIn("Final checks before a draft is saved.", index_html + app_js)
        self.assertNotIn("Words and habits Koinoniko should write around.", index_html)
        self.assertNotIn("Positioning, Voice, and review rules Koinoniko uses when writing.", index_html)
        self.assertNotIn("Add 1-2 product update examples so Koinoniko can write stronger launch/product posts.", index_html + app_js)
        self.assertNotIn('id="voiceCoverageRecommendation"', index_html)
        self.assertNotIn("Voice Context", index_html)
        self.assertNotIn("Voice Context posts", index_html)
        self.assertNotIn('class="voice-status-row"', index_html)
        self.assertNotIn('class="voice-status-chip"', index_html)
        self.assertIn('class="voice-panel-header"', index_html)
        self.assertIn('class="modal-header-actions"', index_html)
        self.assertIn('id="voiceContextButton"', index_html)
        self.assertIn('id="voiceContextModal"', index_html)
        self.assertNotIn('id="voiceContextCount"', index_html)
        self.assertEqual(index_html.count('id="updateVoiceButton"'), 1)
        self.assertIn('id="voiceSuggestionsDisclosure"', index_html)
        self.assertIn("<summary>Show suggested examples</summary>", index_html)
        self.assertNotIn('class="panel voice-examples-panel"', index_html)
        self.assertIn('voice-context-panel', index_html)
        self.assertNotIn("<h2>Suggested Voice Examples</h2>", index_html)
        self.assertNotIn("<h2>Queue</h2>", index_html)
        self.assertIn('id="reviewNavBadge"', index_html)
        self.assertIn(".sidebar-collapsed .nav-badge.muted-badge", styles_css)
        self.assertNotIn(".sidebar-collapsed .nav-badge.muted-badge {\n  display: none;", styles_css)
        self.assertIn('data-view="account"', index_html)
        self.assertIn('<span class="nav-label">Account</span>', index_html)
        self.assertIn('id="accountView"', index_html)
        self.assertIn('id="accountModel"', index_html)
        self.assertIn('id="planModal"', index_html)
        self.assertIn('id="createPostModal"', index_html)
        self.assertIn('data-create-post-field="topic"', index_html)
        self.assertIn('data-create-post-field="post_type"', index_html)
        self.assertIn('data-create-post-field="length"', index_html)
        self.assertIn('id="createPostTopicMenu"', index_html)
        self.assertIn('id="createPostTypeMenu"', index_html)
        self.assertIn('id="createPostLengthMenu"', index_html)
        self.assertIn('id="createPostLength"', index_html)
        self.assertIn('<strong>${escapeHtml(option.label)}</strong>', app_js)
        self.assertIn('lengthOptions', app_js)
        self.assertIn('createPostLengthValue', app_js)
        self.assertIn('id="planLength"', index_html)
        self.assertNotIn('id="createPostQuality"', index_html)
        self.assertNotIn('id="planQuality"', index_html)
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
        self.assertIn('id="queueMoreDetailsModal"', index_html)
        self.assertIn("More Details", index_html + app_js)
        self.assertIn('id="queueMoreDetailsNotes"', app_js)
        self.assertNotIn("Revision options will appear here", app_js)
        self.assertNotIn("Draft\", item.draft_id", app_js)
        self.assertIn('id="detailScheduleShortcuts"', app_js)
        self.assertIn("schedule-shortcuts-popover hidden", app_js)
        self.assertIn("openDetailScheduleShortcuts", app_js)
        self.assertIn('isOpenById("detailScheduleShortcuts")', app_js)
        self.assertIn("closeDetailScheduleShortcuts", app_js)
        self.assertIn("queue-revision-box", app_js + styles_css)
        self.assertIn("queue-save-actions", app_js + styles_css)
        self.assertIn('closeModal("queueModal")', app_js)
        self.assertNotIn("Save changes", app_js)
        queue_modal_template = app_js[app_js.index("function renderQueueModal"): app_js.index("function revisionList")]
        self.assertLess(queue_modal_template.index('class="form-grid"'), queue_modal_template.index("AI instruction"))
        self.assertLess(queue_modal_template.index("AI instruction"), queue_modal_template.index('id="revisionList"'))
        self.assertLess(queue_modal_template.index('id="revisionList"'), queue_modal_template.index("More Details"))
        self.assertLess(queue_modal_template.index("More Details"), queue_modal_template.index(">Save</button>"))
        self.assertIn('id="recentPostsTable"', index_html)
        self.assertNotIn("window.prompt", app_js)
        self.assertNotIn("Scheduled date/time", app_js)
        self.assertIn("queueCardPostTitle", app_js)
        self.assertIn("queueCardPrompt", app_js)
        self.assertIn("pendingQueueCard", app_js)
        self.assertIn("pendingReviewCreations", app_js)
        self.assertIn("queue-card-pending", app_js + styles_css)
        self.assertIn("queue-card-arrived", app_js + styles_css)
        create_post_template = app_js[app_js.index("async function createPost"): app_js.index("async function planWeek")]
        self.assertNotIn('showMessage("Generating post for review.")', create_post_template)
        self.assertNotIn("Created post for review.", create_post_template)
        self.assertNotIn("review-queue-card", app_js)
        self.assertIn(">Edit</button>", app_js)
        self.assertIn(">Deny</button>", app_js)
        self.assertIn(">Post now</button>", app_js)
        self.assertIn("cleanString(item.angle)", app_js)
        self.assertIn("Scheduled not set", app_js)
        self.assertNotIn("Skipping...", app_js)
        self.assertIn(".card-open h3", styles_css)
        self.assertIn("text-overflow: ellipsis", styles_css)
        self.assertIn("white-space: nowrap", styles_css)
        self.assertNotIn("Review / Edit", app_js)
        self.assertIn('id="postVoiceFilter"', index_html)
        self.assertIn('id="trendList"', index_html)
        self.assertIn('id="trendDailyButton"', index_html)
        self.assertIn('id="trendWeeklyButton"', index_html)
        self.assertIn('id="patternList"', index_html)
        self.assertIn('id="patternWeeklyButton"', index_html)
        self.assertIn('id="patternMonthlyButton"', index_html)
        self.assertNotIn('id="patternDailyButton"', index_html)
        self.assertIn('selectedPatternWindow: "weekly"', app_js)
        self.assertNotIn('selectedPatternWindow: "daily"', app_js)
        self.assertIn('id="patternModal"', index_html)
        self.assertIn('id="savedTrendList"', index_html)
        self.assertEqual(index_html.count('id="trendList"'), 1)
        self.assertIn('id="assistantLauncher"', index_html)
        self.assertIn('title="Ask Koinoniko"', index_html)
        self.assertIn('class="assistant-launcher-icon"', index_html)
        self.assertIn("width: 56px", styles_css)
        self.assertIn("width: 40px", styles_css)
        self.assertIn('width="24" height="24" fill="none"', index_html)
        self.assertIn('class="assistant-launcher-laurel"', index_html)
        self.assertIn('fill="currentColor"', index_html)
        self.assertIn('id="assistantDrawer"', index_html)
        self.assertIn('id="assistantStarterPrompts"', index_html)
        self.assertIn('id="assistantMessages"', index_html)
        self.assertIn('id="assistantInput"', index_html)
        self.assertIn('id="assistantExpandButton"', index_html)
        self.assertIn('aria-controls="assistantDrawer"', index_html)
        self.assertNotIn("assistant-pill-button assistant-launcher", index_html)
        self.assertIn("assistant-expand-button", index_html)
        self.assertIn("assistant-expand-icon-open", index_html)
        self.assertIn("assistant-expand-icon-close", index_html)
        self.assertNotIn(".assistant-pill-button", styles_css)
        self.assertIn("Compress Ask Koinoniko", app_js)
        self.assertIn(".assistant-drawer.expanded .assistant-expand-icon-close", styles_css)
        self.assertIn("Ask Koinoniko", index_html)
        self.assertIn("What should I post about today?", app_js)
        self.assertIn("/api/assistant/chat", app_js)
        self.assertIn("/api/assistant/starter-prompts", app_js)
        self.assertIn("shouldShowAssistantStarters", app_js)
        self.assertIn("toggleAssistantExpanded", app_js)
        self.assertIn("assistantRichTextHtml", app_js)
        self.assertIn("assistantInlineHtml", app_js)
        self.assertIn("assistant-thinking", app_js)
        self.assertIn(".assistant-messages.has-chat::before", styles_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", styles_css)
        self.assertIn('drawer.classList.toggle("expanded"', app_js)
        self.assertNotIn('data-view="assistant"', index_html)
        self.assertIn("Use the structure, not the wording.", app_js)
        self.assertIn("usePatternInPost", app_js)
        self.assertIn("savePattern", app_js)
        self.assertIn("unsavePattern", app_js)
        self.assertIn("Pattern removed from saved.", app_js)
        self.assertIn("patternCardSummary", app_js)
        self.assertIn("renderDashboardPatterns", app_js)
        self.assertIn("renderDashboardPatternsPreservingScroll", app_js)
        self.assertIn("updateDashboardPattern", app_js)
        self.assertIn("pattern-save-button", app_js + styles_css)
        self.assertIn("/api/patterns/", app_js)
        self.assertIn('id="createPostTrend"', index_html)
        self.assertIn('id="createPostPattern"', index_html)
        self.assertNotIn('id="createPostUseTrends"', index_html)
        self.assertNotIn('id="createPostUsePatterns"', index_html)
        self.assertIn('id="planUsePatterns"', index_html)
        self.assertNotIn('data-view="patterns"', index_html)
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
        self.assertIn("Sync Trends", index_html)
        self.assertNotIn('id="syncPostsButton"', index_html)
        self.assertNotIn('id="syncMetricsButton"', index_html)
        self.assertNotIn('id="dashboardSyncLine"', index_html)
        self.assertNotIn("Last X sync", index_html + app_js)
        self.assertNotIn('class="top-actions"', index_html)
        self.assertIn('id="accountSyncPostsButton"', index_html)
        self.assertIn('id="accountSyncMetricsButton"', index_html)
        self.assertIn('id="accountSyncAllButton"', index_html)
        self.assertIn('id="accountRefreshTrendsButton"', index_html)
        self.assertIn('id="accountSyncGrid"', index_html)
        self.assertIn('id="accountDetailDrawer"', index_html)
        self.assertIn('id="accountDetailBackdrop"', index_html)
        self.assertIn('class="account-detail-modal hidden"', index_html)
        self.assertIn('class="account-detail-card"', index_html)
        self.assertIn('id="accountSyncMenuButton" type="button" aria-haspopup="true" aria-expanded="false">Sync</button>', index_html)
        self.assertNotIn(">Sync now</button>", index_html)
        self.assertIn('id="accountConnectionsSummary"', index_html)
        self.assertIn('id="accountModelSummary"', index_html)
        self.assertIn('id="accountSyncSummary"', index_html)
        self.assertIn('id="accountUserSummary"', index_html)
        self.assertIn('class="account-settings-list"', index_html)
        self.assertNotIn('class="account-layout"', index_html)
        self.assertNotIn('id="accountConnectionsPill"', index_html + app_js)
        self.assertNotIn('account-detail-drawer', index_html + styles_css)
        self.assertIn(".account-detail-actions .sync-menu", styles_css)
        self.assertIn("bottom: calc(100% + 8px)", styles_css)
        self.assertLess(index_html.index('id="accountSyncPanel"'), index_html.index('id="accountSyncMenuButton"'))
        self.assertLess(index_html.index('id="accountSyncGrid"'), index_html.index('id="accountSyncMenuButton"'))
        self.assertIn("openAccountDetailDrawer", app_js)
        self.assertIn("renderAccountDetailDrawer", app_js)
        self.assertNotIn('<span class="eyebrow">Settings</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Data</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Advanced</span>', index_html)
        self.assertNotIn('<span class="eyebrow">Security</span>', index_html)
        self.assertNotIn("Services Koinoniko uses to generate, sync, publish, and refresh context.", index_html)
        self.assertNotIn("Pull posts created on X, refresh public metrics, and update trend context.", index_html)
        self.assertNotIn("Choose which models power Standard and Best Version generation.", index_html)
        self.assertNotIn("Single-user access for the hosted Koinoniko workspace.", index_html)
        self.assertNotIn('id="refreshButton"', index_html)
        self.assertIn('id="accountStrategistModel"', index_html)
        self.assertIn('id="accountNaturalWriterModel"', index_html)
        self.assertIn('id="accountEvaluatorModel"', index_html)
        self.assertNotIn('id="accountModelMeta"', index_html)
        self.assertNotIn("Model names depend on your provider", index_html)
        self.assertNotIn("Writer:", app_js)
        self.assertNotIn("Evaluator:", app_js)
        self.assertNotIn("Change password from the CLI", index_html + app_js)
        self.assertNotIn("set-web-login</code>", index_html + app_js)
        self.assertNotIn('id="resetAccountModelsButton"', index_html)
        self.assertNotIn('id="testProviderButton"', index_html)
        self.assertNotIn("Reset to Env Defaults", index_html)
        self.assertNotIn("Test Provider", index_html)
        self.assertNotIn("Improve with AI", app_js)
        self.assertNotIn("Generate Best Version", app_js)
        self.assertIn("submitRevisionInstructionFromKey", app_js)
        self.assertIn("Ask for a rewrite...", app_js)
        self.assertNotIn("Make More Natural", app_js)
        self.assertNotIn("Make More Founder-Led", app_js)
        self.assertNotIn(">Move to review</button>", app_js)
        self.assertNotIn(">Approve</button>", app_js)
        self.assertNotIn(">Skip</button>", app_js)
        self.assertIn('state.view === "reviewer"', app_js)
        self.assertIn("clearMessage", app_js)
        self.assertIn('"Accept": "application/json"', app_js)
        self.assertIn('response.headers.get("Content-Type")', app_js)
        self.assertIn("Server returned HTML instead of JSON", app_js)
        self.assertIn("/api/content/create-post", app_js)
        self.assertIn("openQueueDetailById", app_js)
        self.assertIn('setView("reviewer");\n  let payload;', app_js)
        self.assertIn("addPendingReviewCreation", app_js)
        self.assertIn("showCreatedQueueItem(payload.item)", app_js)
        self.assertNotIn("if (createdQueueId) await openQueueDetailById(createdQueueId);", app_js)
        self.assertNotIn("No posts to review.", app_js)
        self.assertNotIn("No approved posts scheduled yet.", app_js)
        self.assertIn("saveAccount", app_js)
        self.assertIn("/api/account/sync-x-posts", app_js)
        self.assertIn("/api/account/sync-x-metrics", app_js)
        self.assertIn("/api/account/sync-x-all", app_js)
        self.assertIn("/api/voice/status", app_js)
        self.assertIn("/api/voice/update", app_js)
        self.assertIn("/api/voice/examples", app_js)
        self.assertIn("/api/voice/current", app_js)
        self.assertIn("/api/voice/suggestions", app_js)
        self.assertIn("/api/voice/coverage", app_js)
        self.assertIn("/api/voice/rules", app_js)
        self.assertIn("/api/voice/avoid-phrases", app_js)
        self.assertIn("/api/voice/examples/", app_js)
        self.assertIn("Suggest why this worked", app_js)
        self.assertIn("renderVoiceCoverage", app_js)
        self.assertIn("renderAvoidPhrases", app_js)
        self.assertIn("saveVoiceRules", app_js)
        self.assertIn("/api/taxonomy/topics", app_js)
        self.assertIn("/api/taxonomy/post-types", app_js)
        self.assertIn("/api/posts/", app_js)
        self.assertIn("Mark as Voice Example", app_js)
        self.assertIn("Why this worked", app_js)
        self.assertIn("Why keep this as a voice example?", app_js)
        self.assertIn("postVoiceSaveStatus", app_js)
        self.assertIn("savePostVoiceFromModal", app_js)
        self.assertIn("Save Voice Details", app_js)
        self.assertIn("voice-detail-editor", app_js)
        self.assertIn(".voice-detail-field textarea", styles_css)
        self.assertNotIn("voice-example-fields", app_js + styles_css)
        self.assertNotIn("voiceDetailInput", app_js)
        self.assertIn("compact-meta-row", app_js)
        self.assertIn("compact-metrics-row", app_js)
        self.assertIn("postFieldControl", app_js)
        self.assertIn("postTypeOptions", app_js)
        self.assertIn("setupCreatePostFieldCombo", app_js)
        self.assertIn("chooseCreatePostFieldOption", app_js)
        self.assertIn("createPostFieldOptionRows", app_js)
        self.assertIn("closeTransientDropdowns", app_js)
        self.assertIn("handleGlobalEscape", app_js)
        self.assertNotIn("best_version", app_js)
        self.assertNotIn("postVoiceTopic", app_js)
        self.assertNotIn("postVoicePostType", app_js)
        self.assertNotIn("postVoiceHook", app_js)
        self.assertNotIn("savePostVoice(event)", app_js)
        self.assertIn("auto_categorize", app_js)
        self.assertIn("Recategorized", app_js)
        self.assertIn("/api/trends/refresh", app_js)
        self.assertIn("connectionRow", app_js)
        self.assertIn("status-badge", app_js)
        self.assertIn("openTrendDetails", app_js)
        self.assertIn("Source details", app_js)
        self.assertIn("escapeHtml(topicLabel(item.topic || \"\"))", app_js)
        self.assertIn("trendCardSummary", app_js)
        self.assertIn("trendCardDate", app_js)
        self.assertIn('class="trend-summary"', app_js)
        self.assertIn('class="trend-date"', app_js)
        self.assertIn(".trend-row .trend-summary", styles_css)
        self.assertIn(".dashboard-top-grid {\n  align-items: stretch;", styles_css)
        self.assertIn("overflow-y: auto;", styles_css)
        self.assertIn("overscroll-behavior: contain;", styles_css)
        self.assertIn("scrollbar-gutter: stable;", styles_css)
        self.assertIn("align-content: start;", styles_css)
        self.assertIn("grid-template-rows: auto auto minmax(34px, auto) auto;", styles_css)
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
        self.assertIn("<h2>Review</h2>", index_html)
        self.assertIn("<h2>Scheduled</h2>", index_html)
        self.assertNotIn("Scheduled Content", index_html)
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
        self.assertIn("Write like the saved Voice profile", skill.content)
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
            self.assertIn("Use saved Facts & Context as the source of truth", prompt)
            self.assertIn("Do not make another tool, agent, or case study supply the workspace's core mechanism", prompt)
            self.assertIn("explain the exact mechanism that transfers to saved Facts & Context", prompt)
            self.assertIn("Natural Voice Calibration:", prompt)
            self.assertIn("Sound like one specific founder noticing a real thing", prompt)
            self.assertIn("Human Rhythm Variation:", prompt)
            self.assertIn("Primary human rhythm target for this request:", prompt)
            self.assertIn("Structure Variation:", prompt)
            self.assertIn("Primary structure for this request:", prompt)
            self.assertIn("Primary layout target for this request:", prompt)
            self.assertIn("Primary closing target for this request:", prompt)
            self.assertIn("Dynamic Product Mention:", prompt)
            self.assertIn("Primary product mention lane for this request:", prompt)
            self.assertIn("Primary product mention placement target for this request:", prompt)
            self.assertIn("Product mention lanes for this request:", prompt)
            self.assertIn("Product mention placement targets for this request:", prompt)
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

            self.assertIn("Study these selected Voice Examples first:", prompt)
            self.assertIn("Example:\nMost sales AI is still mail merge", prompt)
            self.assertIn("Why it worked:\nIt named a weak default", prompt)
            self.assertIn("Performance:\n1000 impressions", prompt)
            self.assertIn("Facts & Context:", prompt)
            self.assertIn("Voice profile:", prompt)
            self.assertNotIn("Labels:", prompt)
            self.assertLess(prompt.index("Study these selected Voice Examples first:"), prompt.index("Writing and review instructions:"))
            store.close()

    def test_structure_variation_rotates_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        structures_a = [name for name, _ in selected_structure_shapes(request_a, [trend_a], count=4)]
        structures_b = [name for name, _ in selected_structure_shapes(request_b, [trend_b], count=4)]

        self.assertEqual(len(structures_a), 4)
        self.assertEqual(len(set(structures_a)), 4)
        self.assertNotEqual(structures_a[0], structures_b[0])
        self.assertEqual(primary_structure_shape(request_a, [trend_a])[0], structures_a[0])
        self.assertEqual(primary_structure_shape(request_b, [trend_b])[0], structures_b[0])

    def test_layout_targets_rotate_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        layouts_a = [name for name, _ in selected_layout_targets(request_a, [trend_a], count=4)]
        layouts_b = [name for name, _ in selected_layout_targets(request_b, [trend_b], count=4)]

        self.assertEqual(len(layouts_a), 4)
        self.assertEqual(len(set(layouts_a)), 4)
        self.assertNotEqual(layouts_a[0], layouts_b[0])
        self.assertEqual(primary_layout_target(request_a, [trend_a])[0], layouts_a[0])
        self.assertEqual(primary_layout_target(request_b, [trend_b])[0], layouts_b[0])

    def test_closing_targets_rotate_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        closings_a = [name for name, _ in selected_closing_targets(request_a, [trend_a], count=4)]
        closings_b = [name for name, _ in selected_closing_targets(request_b, [trend_b], count=4)]

        self.assertEqual(len(closings_a), 4)
        self.assertEqual(len(set(closings_a)), 4)
        self.assertNotEqual(closings_a[0], closings_b[0])
        self.assertEqual(primary_closing_target(request_a, [trend_a])[0], closings_a[0])
        self.assertEqual(primary_closing_target(request_b, [trend_b])[0], closings_b[0])

    def test_human_rhythm_targets_rotate_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        rhythms_a = [name for name, _ in selected_human_rhythm_targets(request_a, [trend_a], count=4)]
        rhythms_b = [name for name, _ in selected_human_rhythm_targets(request_b, [trend_b], count=4)]

        self.assertEqual(len(rhythms_a), 4)
        self.assertEqual(len(set(rhythms_a)), 4)
        self.assertNotEqual(rhythms_a[0], rhythms_b[0])
        self.assertEqual(primary_human_rhythm_target(request_a, [trend_a])[0], rhythms_a[0])
        self.assertEqual(primary_human_rhythm_target(request_b, [trend_b])[0], rhythms_b[0])

    def test_product_mention_lanes_rotate_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        lanes_a = [name for name, _ in selected_product_mention_lanes(request_a, [trend_a], count=4)]
        lanes_b = [name for name, _ in selected_product_mention_lanes(request_b, [trend_b], count=4)]

        self.assertEqual(len(lanes_a), 4)
        self.assertEqual(len(set(lanes_a)), 4)
        self.assertNotEqual(lanes_a[0], lanes_b[0])
        self.assertEqual(primary_product_mention_lane(request_a, [trend_a])[0], lanes_a[0])
        self.assertEqual(primary_product_mention_lane(request_b, [trend_b])[0], lanes_b[0])

    def test_product_mention_placement_targets_rotate_across_trends(self):
        request_a = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_a"],
        )
        request_b = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="buyer timing beats generic data",
            trend_ids=["trend_b"],
        )
        trend_a = TrendItem(
            id="trend_a",
            title="ZoomInfo context layer",
            source_type="web",
            topic="sales AI",
            summary="Context layer",
            why_it_matters="It proves context matters.",
            leadpoet_angle="Buyer timing beats generic data.",
            date=utc_now(),
        )
        trend_b = TrendItem(
            id="trend_b",
            title="Close launches AI agent",
            source_type="web",
            topic="sales AI",
            summary="AI agent",
            why_it_matters="It proves agents need better timing.",
            leadpoet_angle="Account context should decide action.",
            date=utc_now(),
        )

        placements_a = [name for name, _ in selected_product_mention_placement_targets(request_a, [trend_a], count=4)]
        placements_b = [name for name, _ in selected_product_mention_placement_targets(request_b, [trend_b], count=4)]

        self.assertEqual(len(placements_a), 4)
        self.assertEqual(len(set(placements_a)), 4)
        self.assertNotEqual(placements_a[0], placements_b[0])
        self.assertEqual(primary_product_mention_placement_target(request_a, [trend_a])[0], placements_a[0])
        self.assertEqual(primary_product_mention_placement_target(request_b, [trend_b])[0], placements_b[0])

    def test_product_mention_syntax_targets_rotate_across_trends(self):
        primary_syntaxes = set()
        for index in range(12):
            trend = TrendItem(
                id=f"syntax_trend_{index}",
                title=f"Syntax trend {index}",
                source_type="web",
                topic="sales AI",
                summary="Context layer",
                why_it_matters="It proves context matters.",
                leadpoet_angle="Buyer timing beats generic data.",
                date=utc_now(),
            )
            request = DraftRequest(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                angle="buyer timing beats generic data",
                trend_ids=[trend.id],
            )

            syntaxes = [name for name, _ in selected_product_mention_syntax_targets(request, [trend], count=4)]

            self.assertEqual(len(syntaxes), 4)
            self.assertEqual(len(set(syntaxes)), 4)
            self.assertEqual(primary_product_mention_syntax_target(request, [trend])[0], syntaxes[0])
            primary_syntaxes.add(syntaxes[0])

        self.assertGreaterEqual(len(primary_syntaxes), 4)

    def test_workspace_anchor_candidates_respect_product_placement(self):
        text = "Buyer timing should choose the account.\n\nCopy comes later."
        anchor = "The account-timing question comes before louder outbound. Leadpoet owns that part."

        middle = workspace_anchor_candidates(text, anchor, "middle turn")[0]
        product_free_close = workspace_anchor_candidates(text, anchor, "product-free close")[0]
        late = workspace_anchor_candidates(text, anchor, "late consequence")[0]

        self.assertEqual(middle, f"Buyer timing should choose the account.\n\n{anchor}\n\nCopy comes later.")
        self.assertEqual(product_free_close, middle)
        self.assertEqual(late, f"{text}\n\n{anchor}")
        self.assertNotIn("Leadpoet", product_free_close.split("\n\n")[-1])

    def test_leadpoet_anchor_lines_avoid_formulaic_product_subjects(self):
        lanes = [
            "mechanism-first",
            "consequence-first",
            "quiet embedded name",
            "no-name wedge",
            "operator scene",
            "boundary line",
            "signal language",
            "ownership without definition",
            "implicit product belief",
        ]

        lines = {lane: leadpoet_anchor_line(lane) for lane in lanes}
        syntax_lines = {
            syntax: leadpoet_anchor_line("mechanism-first", product_syntax=syntax)
            for syntax in [
                "product as object",
                "prepositional tuck-in",
                "possessive modifier",
                "name omitted",
                "handoff point",
                "question-adjacent",
                "earned late noun",
            ]
        }

        self.assertEqual(len(set(lines.values())), len(lanes))
        self.assertEqual(len(set(syntax_lines.values())), len(syntax_lines))
        for label, line in {**lines, **syntax_lines}.items():
            with self.subTest(label=label):
                if label != "name omitted":
                    self.assertIn("Leadpoet", line)
                self.assertFalse(any(leadpoet_bad_phrase_counts(line).values()), line)
                self.assertFalse(leadpoet_starts_sentence(line), line)
                self.assertNotRegex(line, r"\bLeadpoet\s+(?:owns|starts|can|needs?|should|has\s+to|makes?|keeps?|turns?|focuses)\b")

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

            self.assertEqual(len(drafts), 1)
            self.assertTrue(all(len(draft.text) <= 280 for draft in drafts))
            self.assertTrue(all("—" not in draft.text for draft in drafts))
            self.assertTrue(all(draft.explanation.startswith("Influenced by") for draft in drafts))
            self.assertEqual(len(provider.prompts), 3)
            self.assertIn("Produce the structured Post Intent", provider.prompts[0])
            self.assertIn("Writing and review instructions:", provider.prompts[1])
            self.assertIn("Draft Option Variation Map:", provider.prompts[1])
            self.assertIn("Use these as distinct assignments, not labels to print in the post.", provider.prompts[1])
            self.assertIn("Final evaluator should check", provider.prompts[2])
            saved_draft = store.get_generated_draft(drafts[0].id)
            self.assertIsNotNone(saved_draft)
            self.assertEqual(saved_draft.text, drafts[0].text)
            self.assertEqual(saved_draft.context_snapshot["generation_mode"], "provider")
            self.assertIn("FakeProvider", saved_draft.context_snapshot["provider_name"])
            self.assertIn("quality_notes", saved_draft.context_snapshot)
            self.assertIn("selected_patterns", saved_draft.context_snapshot)
            self.assertIn("selected_context_sections", saved_draft.context_snapshot)
            self.assertIn("primary_structure_option", saved_draft.context_snapshot)
            self.assertIn("primary_product_mention_lane", saved_draft.context_snapshot)
            self.assertIn("primary_product_mention_placement_target", saved_draft.context_snapshot)
            self.assertIn("primary_product_mention_syntax_target", saved_draft.context_snapshot)
            self.assertIn("primary_layout_target", saved_draft.context_snapshot)
            self.assertIn("primary_closing_target", saved_draft.context_snapshot)
            self.assertIn("primary_human_rhythm_target", saved_draft.context_snapshot)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["structure_variation_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["product_mention_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["product_mention_placement_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["product_mention_syntax_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["layout_target_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["closing_target_options"]), 1)
            self.assertGreaterEqual(len(saved_draft.context_snapshot["human_rhythm_options"]), 1)
            self.assertEqual(
                saved_draft.context_snapshot["primary_structure_option"]["name"],
                saved_draft.context_snapshot["structure_variation_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_product_mention_lane"]["name"],
                saved_draft.context_snapshot["product_mention_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_product_mention_placement_target"]["name"],
                saved_draft.context_snapshot["product_mention_placement_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_product_mention_syntax_target"]["name"],
                saved_draft.context_snapshot["product_mention_syntax_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_layout_target"]["name"],
                saved_draft.context_snapshot["layout_target_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_closing_target"]["name"],
                saved_draft.context_snapshot["closing_target_options"][0]["name"],
            )
            self.assertEqual(
                saved_draft.context_snapshot["primary_human_rhythm_target"]["name"],
                saved_draft.context_snapshot["human_rhythm_options"][0]["name"],
            )
            option_lines = [
                line.strip()
                for line in provider.prompts[1].splitlines()
                if line.strip().startswith("- Option ")
            ]
            self.assertEqual(len(option_lines), 3)
            self.assertIn(
                f"structure={saved_draft.context_snapshot['structure_variation_options'][0]['name']}",
                option_lines[0],
            )
            self.assertIn(
                f"structure={saved_draft.context_snapshot['structure_variation_options'][1]['name']}",
                option_lines[1],
            )
            self.assertIn(
                f"product mention lane={saved_draft.context_snapshot['product_mention_options'][0]['name']}",
                option_lines[0],
            )
            self.assertIn(
                f"product mention lane={saved_draft.context_snapshot['product_mention_options'][1]['name']}",
                option_lines[1],
            )
            self.assertIn(
                f"product placement={saved_draft.context_snapshot['product_mention_placement_options'][0]['name']}",
                option_lines[0],
            )
            self.assertIn(
                f"product syntax={saved_draft.context_snapshot['product_mention_syntax_options'][0]['name']}",
                option_lines[0],
            )
            self.assertIn("skills", saved_draft.context_snapshot)
            skill_paths = {skill["path"] for skill in saved_draft.context_snapshot["skills"]}
            self.assertIn("skills/global_voice.md", skill_paths)
            self.assertIn("skills/platforms/x.md", skill_paths)
            self.assertIn("skills/post_types/founder_insight.md", skill_paths)
            self.assertTrue(all(skill.get("sha256") for skill in saved_draft.context_snapshot["skills"]))
            store.close()

    def test_generator_includes_recent_output_anti_sameness_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            store.save_content_queue_item(
                ContentQueueItem(
                    text=(
                        "Static records answer who exists.\n\n"
                        "The useful Leadpoet question is why this account now."
                    ),
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-07-03T10:00:00+00:00",
                )
            )
            provider = FakeProvider()

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="context beats generic automation",
                ),
                variants=1,
            )[0]

            for prompt in provider.prompts[:3]:
                self.assertIn("Recent Output Anti-Sameness:", prompt)
                self.assertIn("The useful Leadpoet question is why this account now.", prompt)
                self.assertIn("Avoid repeating those first-line shapes", prompt)
            rows = draft.context_snapshot["recent_output_anti_sameness"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "queue:needs_review")
            self.assertEqual(rows[0]["leadpoet_sentence"], "The useful Leadpoet question is why this account now.")
            store.close()

    def test_generation_retries_when_writer_options_are_unusable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(),
                    "",
                    "",
                    "",
                    "Leadpoet is useful when sales AI knows which account is moving now.",
                    evaluation_json("Leadpoet is useful when sales AI knows which account is moving now."),
                    "For Leadpoet, useful sales AI starts with knowing which account is moving now.",
                ]
            )

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats volume",
                )
            )

            self.assertEqual(
                drafts[0].text,
                "That only matters if the Leadpoet line makes it actionable: useful sales AI starts with knowing which account is moving now.",
            )
            self.assertNotIn("For Leadpoet", drafts[0].text)
            self.assertEqual(len(provider.prompts), 6)
            self.assertIn("previous draft options were not usable", provider.prompts[2])
            self.assertIn("avoid formulaic 'For Leadpoet...' product lead-in", provider.prompts[-1])
            store.close()

    def test_generation_repairs_banned_not_just_phrase_before_saving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(),
                    "Leadpoet ranks accounts by live signals.",
                    "Leadpoet shows buyer timing.",
                    "Leadpoet makes intent usable.",
                    evaluation_json(
                        "Leadpoet is not just a title filter. It is a timing system.",
                        warnings=["avoid stock framing"],
                    ),
                    "Leadpoet ranks accounts by live signals instead of title filters.",
                ]
            )

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="Leadpoet",
                    post_type="product_update",
                    angle="live signals beat title filters",
                )
            )

            self.assertEqual(drafts[0].text, "Leadpoet ranks accounts by live signals instead of title filters.")
            self.assertNotIn("not just", drafts[0].text.lower())
            self.assertIn('Do not include the exact words "not just".', provider.prompts[-1])
            saved_draft = store.get_generated_draft(drafts[0].id)
            self.assertEqual(saved_draft.text, drafts[0].text)
            store.close()

    def test_generation_repairs_refusal_style_final_into_publishable_post(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(),
                    "Leadpoet turns buyer timing into the starting point.",
                    "Leadpoet makes account context useful before outreach starts.",
                    "Leadpoet is the intent layer before the sequence layer.",
                    evaluation_json(
                        "Cannot proceed. No product update supplied. Provide: shipped feature name.",
                        warnings=[
                            "product_update_missing_concrete_change",
                            "all_drafts_unpublishable",
                            "violates_publishable_output_requirement",
                        ],
                    ),
                    "Leadpoet product belief: the useful sales AI layer starts with buyer timing, not sequence volume.",
                ]
            )

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="Leadpoet",
                    post_type="product_update",
                    angle="No hype, no launch theater.",
                )
            )

            self.assertNotIn("cannot proceed", drafts[0].text.lower())
            self.assertNotIn("provide:", drafts[0].text.lower())
            self.assertIn("Leadpoet", drafts[0].text)
            self.assertIn("Return a publishable post.", provider.prompts[-1])
            warnings = drafts[0].context_snapshot["evaluation"]["warnings"]
            self.assertIn("product_update_missing_concrete_change", warnings)
            self.assertNotIn("all_drafts_unpublishable", warnings)
            self.assertNotIn("violates_publishable_output_requirement", warnings)
            store.close()

    def test_generation_deterministically_anchors_when_provider_ignores_anchor_repair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(),
                    "Every sales AI says it replaces reps. Then asks you who to email.",
                    "Sales AI still needs a human to point it at the right account.",
                    "The agent can write. It still needs judgment.",
                    evaluation_json("Every sales AI says it replaces reps.\n\nThen asks you who to email."),
                    "Every sales AI says it replaces reps.\n\nThen asks you who to email.",
                    "Every sales AI says it replaces reps.\n\nThen asks you who to email.",
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="Sales LLMs",
                    post_type="light_funny",
                    angle="sales AI still needs judgment",
                    length_mode="short",
                )
            )[0]

            self.assertIn("Leadpoet", draft.text)
            self.assertLessEqual(len(draft.text), 160)
            warnings = draft.context_snapshot["evaluation"]["warnings"]
            self.assertNotIn("anchor the point to Leadpoet as the source of the core wedge", warnings)
            store.close()

    def test_generation_uses_saved_workspace_rules_without_leadpoet_leakage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            save_positioning_rules(
                store,
                [
                    "AcmeOS is the core account intelligence layer: first-party CRM context, renewal timing, and buyer intent.",
                    "Competitor agents are market proof that account timing matters, not the source of AcmeOS's wedge.",
                ],
            )
            provider = SequentialProvider(
                [
                    strategy_json(
                        source_material="User request and AcmeOS positioning context.",
                        source_bridge="AI SDR trend -> account timing problem -> AcmeOS account intelligence layer.",
                        claims_to_make=["AcmeOS centers account timing."],
                        success_criteria="specific, grounded, and tied to AcmeOS.",
                    ),
                    "Every AI SDR can write a decent email. It still guesses who is ready to hear it.",
                    "Outbound automation got fast before it got informed.",
                    "The useful layer is account timing, not sequence volume.",
                    evaluation_json(
                        "Every AI SDR can write a decent email.\n\nIt still guesses who is ready to hear it."
                    ),
                    "Every AI SDR can write a decent email.\n\nIt still guesses who is ready to hear it.",
                    "Every AI SDR can write a decent email.\n\nIt still guesses who is ready to hear it.",
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="Sales LLMs",
                    post_type="founder_insight",
                    angle="sales AI still needs account timing",
                    length_mode="standard",
                )
            )[0]

            active_rules = "\n".join(draft.context_snapshot["active_writing_rules"]["positioning_rules"])
            self.assertIn("AcmeOS is the core account intelligence layer", active_rules)
            self.assertNotIn("Leadpoet", active_rules)
            self.assertIn("AcmeOS", draft.text)
            self.assertNotIn("Leadpoet", draft.text)
            warnings = draft.context_snapshot["evaluation"]["warnings"]
            self.assertNotIn("anchor the point to AcmeOS as the source of the core wedge", warnings)
            store.close()

    def test_generation_is_scoped_to_active_workspace_voice_rules_and_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            default_store = SQLiteStore(db_path)
            ingest_x_post(
                default_store,
                text="Leadpoet makes buyer timing visible before outreach starts.",
                url="https://x.com/gavin/status/default-voice",
                date="2026-06-27T12:00:00+00:00",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                worked_reason="Leadpoet-specific timing contrast.",
                imitate=True,
            )
            save_positioning_rules(
                default_store,
                ["Leadpoet is the intent layer for buyer timing and proprietary account context."],
            )

            acme_store = SQLiteStore(db_path, workspace_id="acme")
            ingest_x_post(
                acme_store,
                text="AcmeOS starts from CRM renewal risk instead of generic outbound volume.",
                url="https://x.com/alice/status/acme-voice",
                date="2026-06-27T13:00:00+00:00",
                author="Alice",
                topic="renewals",
                post_type="founder_insight",
                worked_reason="AcmeOS-specific renewal timing contrast.",
                imitate=True,
            )
            save_positioning_rules(
                acme_store,
                [
                    "AcmeOS is the renewal intelligence layer: first-party CRM context, renewal risk, and expansion timing.",
                    "Competitor agents are market proof that timing matters, not the source of AcmeOS's wedge.",
                ],
            )
            provider = SequentialProvider(
                [
                    strategy_json(
                        source_material="User request and AcmeOS renewal context.",
                        source_bridge="AI renewal workflow -> account timing problem -> AcmeOS renewal intelligence layer.",
                        claims_to_make=["AcmeOS centers renewal timing."],
                        success_criteria="specific, grounded, and tied to AcmeOS.",
                    ),
                    "AcmeOS turns renewal risk into the first thing revenue teams see.",
                    "Renewal workflows break when the account context shows up after the sequence.",
                    "The better CRM agent starts with renewal timing, not generated follow-up.",
                    evaluation_json(
                        "AcmeOS turns renewal risk into the first thing revenue teams see."
                    ),
                ]
            )

            draft = SocialBrainGenerator(acme_store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="renewals",
                    post_type="founder_insight",
                    angle="renewal timing beats generated follow-up",
                    length_mode="standard",
                )
            )[0]

            self.assertEqual(draft.workspace_id, "acme")
            self.assertEqual(draft.context_snapshot["workspace_id"], "acme")
            active_rules = "\n".join(draft.context_snapshot["active_writing_rules"]["positioning_rules"])
            self.assertIn("AcmeOS is the renewal intelligence layer", active_rules)
            self.assertNotIn("Leadpoet", active_rules)
            selected_examples = draft.context_snapshot["selected_examples"]
            self.assertEqual([example["author"] for example in selected_examples], ["Alice"])
            self.assertIn("AcmeOS", draft.text)
            self.assertNotIn("Leadpoet", draft.text)
            self.assertEqual(len(default_store.list_posts(platform="x")), 1)
            self.assertEqual(len(acme_store.list_posts(platform="x")), 1)
            self.assertEqual(default_store.list_posts(platform="x")[0].author, "Gavin")
            self.assertEqual(acme_store.list_posts(platform="x")[0].author, "Alice")
            default_store.close()
            acme_store.close()

    def test_new_non_default_workspace_does_not_inherit_gavin_or_leadpoet_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3", workspace_id="user-alice")

            rules_text = "\n".join(load_positioning_rules(store))
            profile = build_trend_scout_profile(store)
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(
                    platform="x",
                    topic="customer onboarding",
                    post_type="founder_insight",
                    angle="why onboarding context matters",
                )
            )

            self.assertNotIn("Leadpoet", rules_text)
            self.assertEqual(profile.user_name, "the writer")
            self.assertEqual(profile.workspace_name, "this workspace")
            self.assertNotIn("Gavin", prompt)
            self.assertNotIn("Leadpoet", prompt)
            store.close()

    def test_generation_clears_stale_positioning_warnings_when_final_text_is_anchored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(),
                    "Sales AI needs timing before copy.",
                    "The message is downstream of the account signal.",
                    "Most tools write before they know who is ready.",
                    evaluation_json(
                        "Sales AI needs timing before copy.\n\nFor Leadpoet, the intent layer comes before outreach.",
                        warnings=[
                            "positioning_gap: no draft names Leadpoet explicitly as the intent layer owner",
                            "All drafts stop at generic 'intent layer' concept without claiming Leadpoet wedge",
                        ],
                    ),
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="Sales LLMs",
                    post_type="founder_insight",
                    angle="sales AI needs buyer timing before copy",
                    length_mode="standard",
                )
            )[0]

            self.assertIn("Leadpoet", draft.text)
            warnings = draft.context_snapshot["evaluation"]["warnings"]
            self.assertEqual(warnings, [])
            store.close()

    def test_generation_uses_default_model_for_all_roles_when_roles_are_blank(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            store.save_app_setting("model.default", "openrouter/default")
            provider = RoleAwareProvider()

            drafts = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight"),
                variants=1,
            )

            self.assertEqual([call["model"] for call in provider.calls], ["openrouter/default"] * 3)
            self.assertEqual(drafts[0].context_snapshot["quality_mode"], "quality_review")
            self.assertEqual(drafts[0].context_snapshot["default_model"], "openrouter/default")
            store.close()

    def test_generation_calls_role_models_and_saves_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            save_avoid_phrases(store, ["game changer", "generic launch language"])
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
                )
            )

            self.assertEqual(len(drafts), 1)
            self.assertEqual(
                [call["model"] for call in provider.calls],
                ["openrouter/strategy", "openrouter/writer", "openrouter/evaluator"],
            )
            snapshot = store.get_generated_draft(drafts[0].id).context_snapshot
            self.assertEqual(snapshot["quality_mode"], "quality_review")
            self.assertEqual(snapshot["strategist_model"], "openrouter/strategy")
            self.assertEqual(snapshot["natural_writer_model"], "openrouter/writer")
            self.assertEqual(snapshot["evaluator_model"], "openrouter/evaluator")
            self.assertIn("strategy_summary", snapshot)
            self.assertEqual(snapshot["strategy_summary"]["thesis"], "Sales AI needs account judgment, not more volume.")
            self.assertEqual(snapshot["strategy_summary"]["concrete_detail"], "buyer timing")
            self.assertIn("Do not imply Gavin personally read examples", snapshot["strategy_summary"]["evidence_boundary"])
            self.assertIn("quality_notes", snapshot)
            self.assertIn("Produce the structured Post Intent", provider.calls[0]["prompt"])
            self.assertIn("source_bridge", provider.calls[0]["prompt"])
            self.assertIn("shallow topical pegs", provider.calls[0]["prompt"])
            self.assertIn("Hidden Post Intent from the strategist", provider.calls[1]["prompt"])
            self.assertIn("Natural Voice Calibration:", provider.calls[1]["prompt"])
            self.assertIn("unsupported first-person setups", provider.calls[1]["prompt"])
            self.assertIn("make the bridge explicit", provider.calls[1]["prompt"])
            self.assertIn("evidence safety", provider.calls[2]["prompt"])
            self.assertIn("natural voice", provider.calls[2]["prompt"])
            self.assertIn("post-worthiness", provider.calls[2]["prompt"])
            self.assertIn("generic swap test", provider.calls[2]["prompt"])
            self.assertIn("Final evaluator should check", provider.calls[2]["prompt"])
            self.assertIn("- game changer", provider.calls[2]["prompt"])
            self.assertIn("Most sales AI", drafts[0].text)
            self.assertIn("Leadpoet", drafts[0].text)
            store.close()

    def test_generation_length_modes_reach_prompts_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = RoleAwareProvider()

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing needs more than email copy",
                    length_mode="long",
                )
            )[0]

            snapshot = store.get_generated_draft(draft.id).context_snapshot
            self.assertEqual(snapshot["length_mode"], "long")
            self.assertEqual(snapshot["length_label"], "Long")
            self.assertEqual(snapshot["max_chars"], 1200)
            self.assertIn("- Length form: Long", provider.calls[0]["prompt"])
            self.assertIn("- Character limit: 1200", provider.calls[1]["prompt"])
            self.assertIn("match the requested length form", provider.calls[2]["prompt"])
            store.close()

    def test_generation_repairs_ai_polish_across_prompt_and_trend_shapes(self):
        scenarios = [
            {
                "name": "trend_response",
                "request": DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats generated email volume",
                    use_trends=True,
                    length_mode="standard",
                ),
                "trend": TrendItem(
                    title="AI SDR teams shift from volume to timing",
                    source_type="news",
                    topic="sales LLMs",
                    summary="Sales teams are questioning high-volume outbound tools.",
                    why_it_matters="It supports the idea that timing matters more than generated copy.",
                    leadpoet_angle="Leadpoet should connect the trend to buyer timing and account context.",
                    urgency="high",
                    date=utc_now(),
                ),
                "bad_final": (
                    "As AI continues to reshape outbound, the future of sales AI is buyer timing at scale.\n\n"
                    "Leadpoet helps teams unlock it."
                ),
                "repair": (
                    "Most sales AI still starts with the email.\n\n"
                    "Inside Leadpoet, the starting point is whether this account has a reason to care now."
                ),
                "expected_violations": ["future of", "as X continues", "scale language"],
                "disallowed": ["as ai continues", "the future of", "at scale", "unlock"],
            },
            {
                "name": "product_update",
                "request": DraftRequest(
                    platform="x",
                    topic="Leadpoet",
                    post_type="product_update",
                    angle="make account context show up before outreach",
                    length_mode="standard",
                ),
                "bad_final": "Here's the thing: Leadpoet is a game changer for seamless account intelligence.",
                "repair": "The useful Leadpoet signal has to make account context show up before the sequence starts.",
                "expected_violations": [
                    "stock setup",
                    "avoid phrase: game changer",
                    "corporate/AI-polish",
                    "standalone 'Leadpoet is...' product definition",
                ],
                "disallowed": ["here's the thing", "game changer", "seamless", "leadpoet is"],
            },
            {
                "name": "short_founder_insight",
                "request": DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="timing beats volume",
                    length_mode="short",
                ),
                "bad_final": "It's not about more outbound. It's about better timing.\n\nLeadpoet makes that possible.",
                "repair": (
                    "More outbound is the easy part.\n\n"
                    "Timing gets useful inside Leadpoet when an account has a real reason to listen."
                ),
                "expected_violations": ["it's not about X"],
                "disallowed": ["it's not about", "it's about", "leadpoet is"],
            },
            {
                "name": "quote_tweet_with_fake_setup",
                "request": DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="quote_tweet",
                    angle="use the quoted post as proof that account timing is the real constraint",
                    length_mode="standard",
                ),
                "bad_final": (
                    "I spent the morning reading every AI SDR thread.\n\n"
                    "The takeaway is Leadpoet makes buyer timing the real workflow."
                ),
                "repair": (
                    "The AI SDR debate keeps landing in the same place.\n\n"
                    "The hard part is knowing why this account should hear from you now. That question gets specific inside Leadpoet."
                ),
                "expected_violations": ["unsupported first-person", "thought-leadership"],
                "disallowed": ["i spent the morning", "the takeaway"],
            },
        ]

        for scenario in scenarios:
            with self.subTest(name=scenario["name"]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    store = self.make_store(tmpdir)
                    store.seed_defaults()
                    trend = scenario.get("trend")
                    if trend:
                        saved_trend = store.save_trend_item(trend)
                        request = replace(scenario["request"], trend_ids=[saved_trend.id])
                    else:
                        request = scenario["request"]
                    provider = SequentialProvider(
                        [
                            strategy_json(),
                            "Leadpoet starts with buyer timing before generated copy.",
                            "The useful sales AI layer knows why this account is ready now.",
                            "Account context matters before the sequence starts.",
                            evaluation_json(scenario["bad_final"]),
                            scenario["repair"],
                        ]
                    )

                    draft = SocialBrainGenerator(store, provider=provider).generate(request)[0]

                    self.assertEqual(draft.text, scenario["repair"])
                    self.assertLessEqual(len(draft.text), draft.context_snapshot["max_chars"])
                    for disallowed in scenario["disallowed"]:
                        self.assertNotIn(disallowed, draft.text.lower())
                    self.assertIn("Natural Voice Calibration:", provider.prompts[1])
                    self.assertIn("Violations to fix:", provider.prompts[-1])
                    for expected in scenario["expected_violations"]:
                        self.assertIn(expected, provider.prompts[-1])
            store.close()

    def test_generation_repairs_cookie_cutter_leadpoet_definition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="verified data versus buyer timing",
                        source_bridge="ZoomInfo proves context matters, but not proprietary buyer timing.",
                    ),
                    "Verified data tells an agent who exists.\n\nLeadpoet is building the intent layer for who is worth action now.",
                    "ZoomInfo proves context is moving into GTM.\n\nLeadpoet is the timing layer.",
                    "The hard part is not the contact record. It is knowing why this account should hear from you now.",
                    evaluation_json(
                        "Verified data tells an agent who exists.\n\nLeadpoet is building the intent layer for buyer context and timing signals.",
                    ),
                    "Verified data tells an agent who exists.\n\nFor Leadpoet, the harder question is why this account is worth action now.",
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="ZoomInfo context layer proves intent matters.",
                )
            )[0]

            self.assertEqual(
                draft.text,
                "Verified data tells an agent who exists.\n\n"
                "The harder question is why this account is worth action now. "
                "Keep the Leadpoet mention close to account timing, not in a definition line.",
            )
            self.assertNotIn("Leadpoet is building", draft.text)
            self.assertNotIn("For Leadpoet", draft.text)
            self.assertTrue(
                any("avoid default 'Leadpoet is building...' product definition" in prompt for prompt in provider.prompts)
            )
            self.assertIn("avoid formulaic 'For Leadpoet...' product lead-in", provider.prompts[-1])
            store.close()

    def test_generation_repairs_formulaic_for_leadpoet_leadin(self):
        bad_text = "Verified data tells an agent who exists.\n\nFor Leadpoet, the harder question is why this account is worth action now."
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="verified data versus buyer timing",
                        source_bridge="ZoomInfo proves context matters, but not proprietary buyer timing.",
                    ),
                    "Verified data tells an agent who exists.\n\nLeadpoet should make buyer timing visible.",
                    "ZoomInfo proves context is moving into GTM.\n\nThe useful account signal is timing.",
                    "The hard part is knowing why this account should hear from you now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="ZoomInfo context layer proves intent matters.",
                )
            )[0]

            self.assertEqual(
                draft.text,
                "Verified data tells an agent who exists.\n\n"
                "The harder question is why this account is worth action now. "
                "Keep the Leadpoet mention close to account timing, not in a definition line.",
            )
            self.assertNotIn("For Leadpoet", draft.text)
            self.assertIn("avoid formulaic 'For Leadpoet...' product lead-in", provider.prompts[-1])
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_repairs_formulaic_leadpoet_modal_leadin(self):
        bad_text = "Static records answer who exists.\n\nLeadpoet should make buyer timing visible."
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="buyer timing",
                        source_bridge="Saved context points to buyer timing.",
                    ),
                    "Static records answer who exists.\n\nThe account signal should explain why now.",
                    "The CRM can be correct and still late.\n\nTiming has to choose the account.",
                    "More names do not fix judgment.\n\nThe useful signal is the reason now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="buyer timing beats generic data",
                )
            )[0]

            self.assertEqual(
                draft.text,
                "Static records answer who exists.\n\n"
                "The account-timing question gets concrete inside Leadpoet when it can make buyer timing visible.",
            )
            self.assertNotIn("Leadpoet should", draft.text)
            self.assertIn("avoid formulaic Leadpoet modal lead-in", provider.prompts[-1])
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_repairs_formulaic_leadpoet_subject_verb_leadin(self):
        bad_text = "Static records answer who exists.\n\nLeadpoet owns the account-timing question."
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="buyer timing",
                        source_bridge="Saved context points to buyer timing.",
                    ),
                    "Static records answer who exists.\n\nThe account signal should explain why now.",
                    "The CRM can be correct and still late.\n\nTiming has to choose the account.",
                    "More names do not fix judgment.\n\nThe useful signal is the reason now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="buyer timing beats generic data",
                )
            )[0]

            self.assertEqual(
                draft.text,
                "Static records answer who exists.\n\n"
                "Inside Leadpoet, ask it plainly: can this keep the account-timing question concrete?",
            )
            self.assertNotIn("Leadpoet owns", draft.text)
            self.assertFalse(any(leadpoet_bad_phrase_counts(draft.text).values()), draft.text)
            self.assertIn("avoid formulaic Leadpoet subject-verb lead-in", provider.prompts[-1])
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_repairs_formulaic_where_leadpoet_leadin(self):
        bad_text = "More names do not fix judgment.\n\nThat is where Leadpoet focuses: timing before copy."
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="buyer timing",
                        source_bridge="Saved context points to buyer timing.",
                    ),
                    "Static records answer who exists.\n\nThe account signal should explain why now.",
                    "The CRM can be correct and still late.\n\nTiming has to choose the account.",
                    "More names do not fix judgment.\n\nThe useful signal is the reason now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="buyer timing beats generic data",
                )
            )[0]

            self.assertEqual(
                draft.text,
                "More names do not fix judgment.\n\nTiming before copy is the part Leadpoet keeps visible.",
            )
            self.assertNotIn("That is where Leadpoet", draft.text)
            self.assertIn("avoid formulaic 'that is where Leadpoet...' product lead-in", provider.prompts[-1])
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_repairs_formulaic_the_leadpoet_noun_is_definition(self):
        bad_text = "Buyer timing comes first.\n\nThe Leadpoet product belief is simple: timing should pick the account before copy."
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="buyer timing",
                        source_bridge="Saved context points to buyer timing.",
                    ),
                    "Buyer timing comes first.\n\nThe account signal should explain why now.",
                    "The CRM can be correct and still late.\n\nTiming has to choose the account.",
                    "More names do not fix judgment.\n\nThe useful signal is the reason now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="buyer timing beats generic data",
                )
            )[0]

            self.assertIn("Leadpoet", draft.text)
            self.assertNotIn("The Leadpoet product belief is", draft.text)
            self.assertFalse(any(leadpoet_bad_phrase_counts(draft.text).values()), draft.text)
            self.assertIn("avoid formulaic 'The Leadpoet ... is...' product definition", provider.prompts[-1])
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_keeps_trend_specific_lanes_through_evaluation_and_repair(self):
        def primary_line(prompt: str, prefix: str) -> str:
            return next(line for line in prompt.splitlines() if line.startswith(prefix))

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            trend = store.save_trend_item(
                TrendItem(
                    id="trend_lane_consistency",
                    title="ZoomInfo context layer",
                    source_type="web",
                    topic="sales AI",
                    summary="Context layer",
                    why_it_matters="It proves context matters.",
                    leadpoet_angle="Buyer timing beats generic data.",
                    date=utc_now(),
                )
            )
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="verified data versus buyer timing",
                        source_bridge="ZoomInfo proves context matters, but not proprietary buyer timing.",
                    ),
                    "Verified data tells an agent who exists.\n\nLeadpoet is building the intent layer for who is worth action now.",
                    "ZoomInfo proves context is moving into GTM.\n\nLeadpoet is the timing layer.",
                    "The hard part is not the contact record. It is knowing why this account should hear from you now.",
                    evaluation_json(
                        "Verified data tells an agent who exists.\n\nLeadpoet is building the intent layer for buyer context and timing signals.",
                    ),
                    "Verified data tells an agent who exists.\n\nThe Leadpoet signal should explain why now.",
                ]
            )

            SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="ZoomInfo context layer proves intent matters.",
                    use_trends=True,
                    trend_ids=[trend.id],
                )
            )

            writer_prompt = provider.prompts[1]
            evaluator_prompt = provider.prompts[2]
            repair_prompt = provider.prompts[-1]
            for prefix in [
                "Primary structure for this request:",
                "Primary layout target for this request:",
                "Primary closing target for this request:",
                "Primary human rhythm target for this request:",
                "Primary product mention lane for this request:",
                "Primary product mention placement target for this request:",
                "Primary product mention syntax target for this request:",
            ]:
                self.assertEqual(primary_line(writer_prompt, prefix), primary_line(evaluator_prompt, prefix))
                self.assertEqual(primary_line(writer_prompt, prefix), primary_line(repair_prompt, prefix))
            store.close()

    def test_generation_locally_cleans_stubborn_leadpoet_definition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            bad_text = (
                "Verified data tells an agent who exists.\n\n"
                "Leadpoet is building the intent layer for buyer context and timing signals."
            )
            provider = SequentialProvider(
                [
                    strategy_json(
                        thesis="Buyer timing needs account context.",
                        concrete_detail="verified data versus buyer timing",
                        source_bridge="ZoomInfo proves context matters, but not proprietary buyer timing.",
                    ),
                    bad_text,
                    "ZoomInfo proves context is moving into GTM.\n\nLeadpoet is the timing layer.",
                    "The hard part is not the contact record. It is knowing why this account should hear from you now.",
                    evaluation_json(bad_text),
                    bad_text,
                    bad_text,
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales AI",
                    post_type="founder_insight",
                    angle="ZoomInfo context layer proves intent matters.",
                )
            )[0]

            self.assertIn("Verified data tells an agent who exists.", draft.text)
            self.assertIn("Leadpoet", draft.text)
            self.assertIn("timing should choose the account", draft.text.lower())
            self.assertNotIn("Leadpoet is", draft.text)
            self.assertNotIn("Leadpoet is building", draft.text)
            self.assertNotIn("For Leadpoet", draft.text)
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_deterministic_leadpoet_definition_replacements_vary_by_lane(self):
        outputs_by_lane = {}
        for index in range(60):
            trend = TrendItem(
                id=f"trend_{index}",
                title=f"Trend {index}",
                source_type="web",
                topic="sales AI",
                summary="Context layer",
                why_it_matters="It proves context matters.",
                leadpoet_angle="Buyer timing beats generic data.",
                date=utc_now(),
            )
            request = DraftRequest(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                angle="buyer timing beats generic data",
                trend_ids=[trend.id],
            )
            lane = primary_product_mention_lane(request, [trend])[0]
            outputs_by_lane.setdefault(
                lane,
                deterministic_leadpoet_definition_repair(
                    "Leadpoet is building the intent layer for buyer timing.",
                    request=request,
                    trends=[trend],
                    max_chars=280,
                ),
            )

        self.assertEqual(len(outputs_by_lane), 9)
        self.assertGreaterEqual(len(set(outputs_by_lane.values())), 9)
        combined = "\n".join(outputs_by_lane.values())
        for banned in ["Leadpoet is", "Leadpoet is building", "For Leadpoet", "Leadpoet should"]:
            self.assertNotIn(banned.lower(), combined.lower())
        self.assertIn("Put Leadpoet inside that account-change check", combined)
        self.assertIn("before Leadpoet earns the line", combined)
        self.assertIn("Inside Leadpoet, that belief beats another volume claim", combined)

    def test_deterministic_leadpoet_subject_verb_repairs_vary_by_syntax_target(self):
        outputs_by_syntax = {}
        for index in range(180):
            trend = TrendItem(
                id=f"syntax_repair_trend_{index}",
                title=f"Syntax repair trend {index}",
                source_type="web",
                topic="sales AI",
                summary="Context layer",
                why_it_matters="It proves context matters.",
                leadpoet_angle="Buyer timing beats generic data.",
                date=utc_now(),
            )
            request = DraftRequest(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                angle="buyer timing beats generic data",
                trend_ids=[trend.id],
            )
            syntax = primary_product_mention_syntax_target(request, [trend])[0]
            lane = primary_product_mention_lane(request, [trend])[0]
            if lane == "no-name wedge":
                continue
            outputs_by_syntax.setdefault(
                syntax,
                deterministic_leadpoet_definition_repair(
                    "Leadpoet makes timing useful when an account has a reason to listen.",
                    request=request,
                    trends=[trend],
                    max_chars=280,
                ),
            )

        self.assertGreaterEqual(len(outputs_by_syntax), 5)
        self.assertGreaterEqual(len(set(outputs_by_syntax.values())), 5)
        combined = "\n".join(outputs_by_syntax.values()).lower()
        self.assertNotRegex(combined, r"\bleadpoet\s+(?:owns?|makes?|starts?|keeps?|turns?|focuses)\b")
        self.assertTrue(all(not any(leadpoet_bad_phrase_counts(text).values()) for text in outputs_by_syntax.values()))

    def test_missing_anchor_fallback_varies_leadpoet_line_by_trend(self):
        class NoAnchorProvider:
            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [
                        strategy_json(
                            thesis="Buyer timing needs account context.",
                            concrete_detail="buyer timing",
                            source_bridge="The trend points to buyer timing.",
                        )
                    ]
                if "Write 3 X draft options" in prompt:
                    return ["Buyer timing is the real sales AI advantage. More email is not strategy."][:variants]
                if "Choose the best X post" in prompt:
                    return [evaluation_json("Buyer timing is the real sales AI advantage. More email is not strategy.")]
                if "Repair this X post" in prompt:
                    return ["Buyer timing is the real sales AI advantage. More email is not strategy."]
                return ["Buyer timing is the real sales AI advantage."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            trend_a = store.save_trend_item(
                TrendItem(
                    id="trend_a",
                    title="ZoomInfo context layer",
                    source_type="web",
                    topic="sales AI",
                    summary="Context layer",
                    why_it_matters="It proves context matters.",
                    leadpoet_angle="Buyer timing beats generic data.",
                    date=utc_now(),
                )
            )
            trend_b = store.save_trend_item(
                TrendItem(
                    id="trend_b",
                    title="Close launches AI agent",
                    source_type="web",
                    topic="sales AI",
                    summary="AI agent",
                    why_it_matters="It proves agents need better timing.",
                    leadpoet_angle="Account context should decide action.",
                    date=utc_now(),
                )
            )
            generator = SocialBrainGenerator(store, provider=NoAnchorProvider())
            request_a = DraftRequest(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                angle="buyer timing beats generic data",
                use_trends=True,
                trend_ids=[trend_a.id],
            )
            request_b = replace(request_a, trend_ids=[trend_b.id])

            draft_a = generator.generate(request_a, save=False)[0]
            draft_b = generator.generate(request_b, save=False)[0]

            self.assertIn("Leadpoet", draft_a.text)
            self.assertIn("Leadpoet", draft_b.text)
            self.assertNotEqual(draft_a.text, draft_b.text)
            self.assertNotIn("Leadpoet is", draft_a.text)
            self.assertNotIn("Leadpoet is", draft_b.text)
            self.assertNotIn("proprietary buyer context and timing signals are where useful sales AI starts", draft_a.text)
            self.assertNotIn("proprietary buyer context and timing signals are where useful sales AI starts", draft_b.text)
            self.assertNotEqual(
                draft_a.context_snapshot["primary_product_mention_lane"]["name"],
                draft_b.context_snapshot["primary_product_mention_lane"]["name"],
            )
            self.assertNotEqual(
                draft_a.context_snapshot["primary_product_mention_syntax_target"]["name"],
                draft_b.context_snapshot["primary_product_mention_syntax_target"]["name"],
            )
            store.close()

    def test_no_name_product_lane_can_omit_leadpoet_anchor(self):
        class NoNameProvider:
            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [
                        strategy_json(
                            thesis="Buyer timing should choose the account before copy.",
                            concrete_detail="buyer timing",
                            source_bridge="The trend points to buyer timing.",
                        )
                    ]
                if "Write 3 X draft options" in prompt:
                    return ["Buyer timing should choose the account before copy writes the email."][:variants]
                if "Choose the best X post" in prompt:
                    return [evaluation_json("Buyer timing should choose the account before copy writes the email.")]
                return ["Buyer timing should choose the account before copy writes the email."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            trend = store.save_trend_item(
                TrendItem(
                    id="no_name_trend_2",
                    title="Buyer timing becomes the account selection layer",
                    source_type="web",
                    topic="sales AI",
                    summary="Context matters before generated copy.",
                    why_it_matters="It proves account timing matters.",
                    leadpoet_angle="Buyer timing beats generic data.",
                    date=utc_now(),
                )
            )
            request = DraftRequest(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                angle="buyer timing beats generic data",
                use_trends=True,
                trend_ids=[trend.id],
            )
            self.assertEqual(primary_product_mention_lane(request, [trend])[0], "no-name wedge")

            draft = SocialBrainGenerator(store, provider=NoNameProvider()).generate(request, save=False)[0]

            self.assertEqual(draft.text, "Buyer timing should choose the account before copy writes the email.")
            self.assertNotIn("Leadpoet", draft.text)
            self.assertEqual(draft.context_snapshot["evaluation"]["warnings"], [])
            store.close()

    def test_generation_repairs_cookie_cutter_leadpoet_mentions_across_current_trend_batch(self):
        class CookieCutterTrendProvider:
            def __init__(self):
                self.repair_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [
                        strategy_json(
                            thesis="Sales AI needs buyer timing, not static records.",
                            concrete_detail="buyer timing",
                            source_bridge="The source proves context matters; saved context turns that into buyer timing.",
                            success_criteria="Avoid cookie-cutter product definitions.",
                        )
                    ]
                if "Write 3 X draft options" in prompt:
                    return [
                        "The market keeps moving from records to context.\n\nLeadpoet is building the intent layer for buyer timing.",
                        "Verified data tells a rep who exists.\n\nLeadpoet is the layer for who is worth action now.",
                        "The category wants context.\n\nLeadpoet's bet is proprietary buyer timing.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    return [
                        evaluation_json(
                            "The market keeps moving from records to context.\n\n"
                            "Leadpoet is building the intent layer for buyer timing and account qualification."
                        )
                    ]
                if "Repair this X post" in prompt:
                    self.repair_count += 1
                    repairs = [
                        "Static records answer who exists. The why-now signal has to become usable inside Leadpoet.",
                        "Buyer timing is where the Leadpoet signal starts.\n\nThe email is downstream.",
                        "A rep does not need another prettier list.\n\nThe account-change moment is Leadpoet's lane.\n\nThat is when copy becomes useful.",
                        "- Signal\n- Context\n- A reason now\n\nThe account should be worth action before the sequence starts.",
                        "The handoff from signal to sequence is where Leadpoet enters.\n\nThe contact record is only the start.",
                        "What changes when context is real?\n\nCopy comes later.\n\nInside Leadpoet, the account reason comes before the first sentence.",
                        "The CRM record can be perfect.\n\nTiming changes the account choice before Leadpoet earns the mention.\n\nStill too late is still too late.",
                    ]
                    return [repairs[(self.repair_count - 1) % len(repairs)]]
                return ["Buyer timing beats static lists."]

        trend_titles = [
            "Stop Losing Pipeline to Slow Follow-Up: Meet Qualified",
            "Salesforce Agentforce Prospecting Agent Now Powered by ZoomInfo's GTM.AI for AI Sales Prospecting",
            "The Global Associates Closes the Process Gap in B2B Lead Generation with ISO Certification and AI",
            "Aircall Acquires Piper AI to Expand Revenue Intelligence and Sales Automation Capabilities",
            "Close Launches Chloe, an AI Sales Agent Built Directly Into the CRM",
            "Right Now, We Run 4+ AI SDR Agents. Here’s Why.",
            "Salesforce Puts an AI Marketing Team in Every Marketer’s Hands",
            "Salesforce Acquires Contentful, Rolls Out New AI Agents for Marketers",
            "Bombora: Interview CEO Mark Connon About The B2B Intent Data Company",
            "From Lead Gen to Revenue Marketing",
            "The Revenue Handoff Problem: Why CX Signals Still Don't Flow Cleanly Into RevTech Stacks",
            "ZoomInfo, Recognized as One to Watch in Snowflake's Modern Marketing Data Stack Report",
            "One Stalled Deal, One Automation, 10 New Prospects: Lightfield CEO Keith Peiris Demos the AI-Native GTM Loop Live",
            "AI in CRM: Benefits, Use Cases, and Tools",
            "Salesforce Makes Agentforce Commerce Generally Available Ahead of Peak Season",
            "As AI Agents Transform Commerce, Salesforce Unleashes Its Biggest Agentforce Commerce Release Yet",
            "Why LinkedIn engagement is becoming one of the most valuable sales signals",
            "The four layers of winning GTM infrastructure",
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = CookieCutterTrendProvider()
            trends = [
                store.save_trend_item(
                    TrendItem(
                        title=title,
                        source_type="web",
                        topic="sales AI",
                        summary="Current GTM trend fixture for generation audit.",
                        why_it_matters="It shows the market moving toward context and timing.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )
                for title in trend_titles
            ]

            drafts = []
            for index in range(20):
                trend = trends[index % len(trends)]
                drafts.append(
                    SocialBrainGenerator(store, provider=provider).generate(
                        DraftRequest(
                            platform="x",
                            topic=trend.topic,
                            post_type="founder_insight",
                            angle=trend.leadpoet_angle,
                            use_trends=True,
                            trend_ids=[trend.id],
                            length_mode="standard",
                        ),
                        variants=1,
                        save=False,
                    )[0]
                )

            texts = [draft.text for draft in drafts]
            lowered_text = "\n\n".join(texts).lower()
            self.assertEqual(len(drafts), 20)
            self.assertGreaterEqual(provider.repair_count, 20)
            self.assertNotIn("leadpoet is building", lowered_text)
            self.assertNotIn("leadpoet is the", lowered_text)
            self.assertNotIn("leadpoet's bet", lowered_text)
            self.assertGreaterEqual(len({text.split("\n\n", 1)[0] for text in texts}), 5)
            self.assertGreaterEqual(
                len({draft.context_snapshot["structure_variation_options"][0]["name"] for draft in drafts}),
                5,
            )
            self.assertGreaterEqual(
                len({draft.context_snapshot["product_mention_options"][0]["name"] for draft in drafts}),
                5,
            )
            self.assertGreaterEqual(
                len({draft.context_snapshot["product_mention_placement_options"][0]["name"] for draft in drafts}),
                4,
            )
            self.assertGreaterEqual(
                len({draft.context_snapshot["product_mention_syntax_options"][0]["name"] for draft in drafts}),
                4,
            )
            self.assertGreaterEqual(
                len({draft.context_snapshot["human_rhythm_options"][0]["name"] for draft in drafts}),
                4,
            )
            store.close()

    def test_audit_naturalness_x_generates_twenty_trend_posts_with_varied_shapes(self):
        class CookieCutterTrendProvider:
            def __init__(self):
                self.repair_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [
                        strategy_json(
                            thesis="Sales AI needs buyer timing, not static records.",
                            concrete_detail="buyer timing",
                            source_bridge="The source proves context matters; saved context turns that into buyer timing.",
                            success_criteria="Avoid cookie-cutter product definitions.",
                        )
                    ]
                if "Write 3 X draft options" in prompt:
                    return [
                        "The market keeps moving from records to context.\n\nLeadpoet is building the intent layer for buyer timing.",
                        "Verified data tells a rep who exists.\n\nLeadpoet is the layer for who is worth action now.",
                        "The category wants context.\n\nLeadpoet's bet is proprietary buyer timing.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    return [
                        evaluation_json(
                            "The market keeps moving from records to context.\n\n"
                            "Leadpoet is building the intent layer for buyer timing and account qualification."
                        )
                    ]
                if "Repair this X post" in prompt:
                    self.repair_count += 1
                    repairs = [
                        "Static records answer who exists. The why-now signal has to become usable inside Leadpoet.",
                        "Buyer timing is where the Leadpoet signal starts.\n\nThe email is downstream.",
                        "A rep does not need another prettier list.\n\nThe account-change moment is Leadpoet's lane.\n\nThat is when copy becomes useful.",
                        "- Signal\n- Context\n- A reason now\n\nThe account should be worth action before the sequence starts.",
                        "The handoff from signal to sequence is where Leadpoet enters.\n\nThe contact record is only the start.",
                        "What changes when context is real?\n\nCopy comes later.\n\nInside Leadpoet, the account reason comes before the first sentence.",
                        "The CRM record can be perfect.\n\nTiming changes the account choice before Leadpoet earns the mention.\n\nStill too late is still too late.",
                    ]
                    return [repairs[(self.repair_count - 1) % len(repairs)]]
                return ["Buyer timing beats static lists."]

        trend_titles = [
            "Stop Losing Pipeline to Slow Follow-Up: Meet Qualified",
            "Salesforce Agentforce Prospecting Agent Now Powered by ZoomInfo's GTM.AI for AI Sales Prospecting",
            "The Global Associates Closes the Process Gap in B2B Lead Generation with ISO Certification and AI",
            "Aircall Acquires Piper AI to Expand Revenue Intelligence and Sales Automation Capabilities",
            "Close Launches Chloe, an AI Sales Agent Built Directly Into the CRM",
            "Right Now, We Run 4+ AI SDR Agents. Here's Why.",
            "Salesforce Puts an AI Marketing Team in Every Marketer's Hands",
            "Salesforce Acquires Contentful, Rolls Out New AI Agents for Marketers",
            "Bombora: Interview CEO Mark Connon About The B2B Intent Data Company",
            "From Lead Gen to Revenue Marketing",
            "The Revenue Handoff Problem: Why CX Signals Still Don't Flow Cleanly Into RevTech Stacks",
            "ZoomInfo, Recognized as One to Watch in Snowflake's Modern Marketing Data Stack Report",
            "One Stalled Deal, One Automation, 10 New Prospects: Lightfield CEO Keith Peiris Demos the AI-Native GTM Loop Live",
            "AI in CRM: Benefits, Use Cases, and Tools",
            "Salesforce Makes Agentforce Commerce Generally Available Ahead of Peak Season",
            "As AI Agents Transform Commerce, Salesforce Unleashes Its Biggest Agentforce Commerce Release Yet",
            "Why LinkedIn engagement is becoming one of the most valuable sales signals",
            "The four layers of winning GTM infrastructure",
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = CookieCutterTrendProvider()
            for title in trend_titles:
                store.save_trend_item(
                    TrendItem(
                        title=title,
                        source_type="web",
                        topic="sales AI",
                        summary="Current GTM trend fixture for generation audit.",
                        why_it_matters="It shows the market moving toward context and timing.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=provider),
                count=20,
                trend_limit=20,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["post_count"], 20)
            self.assertEqual(payload["trend_count"], len(trend_titles))
            self.assertEqual(payload["covered_trend_count"], len(trend_titles))
            self.assertEqual(payload["uncovered_trend_count"], 0)
            self.assertGreaterEqual(provider.repair_count, 20)
            self.assertTrue(all(count == 0 for count in payload["bad_phrase_counts"].values()))
            self.assertEqual(payload["warnings"], [])
            self.assertGreaterEqual(payload["unique_first_lines"], 5)
            self.assertGreaterEqual(payload["unique_last_lines"], 5)
            self.assertGreaterEqual(len(payload["primary_structure_option_counts"]), 5)
            self.assertGreaterEqual(len(payload["primary_product_mention_lane_counts"]), 5)
            self.assertGreaterEqual(len(payload["primary_product_mention_placement_target_counts"]), 4)
            self.assertGreaterEqual(len(payload["primary_product_mention_syntax_target_counts"]), 4)
            self.assertGreaterEqual(len(payload["primary_layout_target_counts"]), 4)
            self.assertGreaterEqual(len(payload["primary_closing_target_counts"]), 4)
            self.assertGreaterEqual(len(payload["primary_human_rhythm_target_counts"]), 4)
            self.assertGreaterEqual(len(payload["first_structure_option_counts"]), 5)
            self.assertGreaterEqual(len(payload["first_product_mention_lane_counts"]), 5)
            self.assertGreaterEqual(len(payload["leadpoet_mention_pattern_counts"]), 5)
            self.assertGreaterEqual(len(payload["leadpoet_actual_syntax_counts"]), 3)
            self.assertGreaterEqual(len(payload["leadpoet_paragraph_position_counts"]), 3)
            self.assertGreaterEqual(len(payload["leadpoet_sentence_position_counts"]), 3)
            self.assertLess(payload["leadpoet_final_line_mentions"], 10)
            self.assertLess(payload["leadpoet_sentence_start_mentions"], 10)
            self.assertGreaterEqual(len(payload["layout_fingerprint_counts"]), 4)
            self.assertLess(max(payload["leadpoet_mention_pattern_counts"].values()), 10)
            self.assertNotIn("leadpoet_is_building", payload["leadpoet_shapes"])
            self.assertNotIn("leadpoet_is_built", payload["leadpoet_shapes"])
            self.assertNotIn("leadpoet_is", payload["leadpoet_shapes"])
            store.close()

    def test_audit_naturalness_x_feeds_prior_batch_outputs_into_prompts(self):
        class BatchMemoryProvider:
            def __init__(self):
                self.prompts: list[str] = []
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                self.prompts.append(prompt)
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nThe useful Leadpoet question is why this account now.",
                        "The CRM record can be right.\n\nTiming changes the account before it reaches Leadpoet.",
                        "More names do not fix judgment.\n\nThe account-change moment belongs before copy.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    finals = [
                        "Static records answer who exists.\n\nThe useful Leadpoet question is why this account now.",
                        "The CRM record can be right.\n\nTiming changes the account before it reaches Leadpoet.",
                    ]
                    text = finals[self.final_count % len(finals)]
                    self.final_count += 1
                    return [evaluation_json(text)]
                return ["Timing changes the account before it reaches Leadpoet."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(2):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )
            provider = BatchMemoryProvider()

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=provider),
                count=2,
                trend_limit=2,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            strategy_prompts = [prompt for prompt in provider.prompts if "Do not write the post yet" in prompt]
            self.assertEqual(payload["post_count"], 2)
            self.assertEqual(len(strategy_prompts), 2)
            self.assertNotIn("source=batch", strategy_prompts[0])
            self.assertIn("source=batch", strategy_prompts[1])
            self.assertIn("The useful Leadpoet question is why this account now.", strategy_prompts[1])
            store.close()

    def test_audit_naturalness_x_warns_on_repeated_closing_line(self):
        class RepeatedCloserProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists. The Leadpoet signal makes timing visible.\n\nThe useful question is why now.",
                        "The CRM can be correct. The Leadpoet signal makes timing visible.\n\nThe useful question is why now.",
                        "More names do not fix judgment. The Leadpoet signal makes timing visible.\n\nThe useful question is why now.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    line = first_lines[self.final_count % len(first_lines)]
                    self.final_count += 1
                    return [evaluation_json(f"{line} The Leadpoet signal makes timing visible.\n\nThe useful question is why now.")]
                return ["Static records answer who exists. The Leadpoet signal makes timing visible.\n\nThe useful question is why now."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=RepeatedCloserProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["unique_first_lines"], 5)
            self.assertEqual(payload["unique_last_lines"], 1)
            self.assertTrue(
                any("Closing lines are not varied enough" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_audit_naturalness_x_warns_on_repeated_stock_phrase(self):
        class RepeatedPhraseProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists. Buyer timing should choose the account before copy.\n\nLeadpoet can make signal one visible.",
                        "The CRM can be correct. Buyer timing should choose the account before copy.\n\nLeadpoet can make signal two visible.",
                        "More names do not fix judgment. Buyer timing should choose the account before copy.\n\nLeadpoet can make signal three visible.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    endings = ["one", "two", "three", "four", "five"]
                    index = self.final_count % len(first_lines)
                    self.final_count += 1
                    return [
                        evaluation_json(
                            f"{first_lines[index]} Buyer timing should choose the account before copy.\n\n"
                            f"The Leadpoet signal can make signal {endings[index]} visible."
                        )
                    ]
                return ["Static records answer who exists. The Leadpoet signal can make timing visible."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=RepeatedPhraseProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["repeated_phrase_counts"]["buyer timing should choose"], 5)
            self.assertTrue(
                any("Repeated stock phrase appeared across generated posts" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_audit_naturalness_x_warns_when_leadpoet_stays_in_final_line(self):
        class FinalLineProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nThe useful question stays inside Leadpoet.",
                        "The CRM can be correct.\n\nThe useful question stays inside Leadpoet.",
                        "More names do not fix judgment.\n\nThe useful question stays inside Leadpoet.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    endings = [
                        "The useful question stays inside Leadpoet.",
                        "The Leadpoet edge stays there.",
                        "The Leadpoet edge remains there.",
                        "That constraint stays visible inside Leadpoet.",
                        "The timing reason gets specific inside Leadpoet.",
                    ]
                    index = self.final_count % len(first_lines)
                    self.final_count += 1
                    return [evaluation_json(f"{first_lines[index]}\n\nBuyer timing should choose the account. {endings[index]}")]
                return ["Buyer timing should choose the account. The useful question stays inside Leadpoet."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=FinalLineProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["leadpoet_final_line_mentions"], 5)
            self.assertEqual(payload["leadpoet_paragraph_position_counts"]["last"], 5)
            self.assertTrue(any("Leadpoet appears in the final line too often" in warning for warning in payload["warnings"]))
            store.close()

    def test_audit_naturalness_x_warns_when_leadpoet_starts_product_sentence(self):
        class SentenceStartProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists. Leadpoet, in that handoff, belongs near the account reason.",
                        "The CRM can be correct. Leadpoet, in that handoff, belongs near the account reason.",
                        "More names do not fix judgment. Leadpoet, in that handoff, belongs near the account reason.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    index = self.final_count % len(first_lines)
                    self.final_count += 1
                    return [evaluation_json(f"{first_lines[index]} Leadpoet, in that handoff, belongs near the account reason.")]
                return ["Static records answer who exists. Leadpoet, in that handoff, belongs near the account reason."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=SentenceStartProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["leadpoet_sentence_start_mentions"], 5)
            self.assertTrue(
                any("Leadpoet starts the product sentence too often" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_leadpoet_mention_pattern_classifies_common_dynamic_verbs(self):
        cases = {
            "Leadpoet should explain why now.": "leadpoet_should",
            "Leadpoet needs to make timing visible.": "leadpoet_needs",
            "Leadpoet owns the account-timing question.": "leadpoet_owns",
            "Leadpoet starts before the sequence.": "leadpoet_starts",
            "Leadpoet can show the account reason.": "leadpoet_can",
            "This is where Leadpoet focuses: timing before copy.": "that_is_where_leadpoet",
            "That’s where Leadpoet focuses: timing before copy.": "that_is_where_leadpoet",
            "That is the Leadpoet point.": "that_is_the_leadpoet_closer",
            "The Leadpoet signal is buyer timing.": "the_leadpoet_noun_is",
            "Leadpoet belongs close to account timing.": "leadpoet_belongs",
            "Leadpoet only matters when timing is visible.": "leadpoet_only_matters",
            "Leadpoet must make timing useful.": "leadpoet_must",
            "The account reason gets specific inside Leadpoet.": "inside_leadpoet",
            "The handoff is where Leadpoet enters.": "where_leadpoet_enters",
            "Timing changes before Leadpoet earns the mention.": "before_leadpoet_earns",
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(leadpoet_mention_pattern(text), expected)

    def test_leadpoet_actual_syntax_classifies_product_grammar(self):
        cases = {
            "Leadpoet owns the account-timing question.": "product_subject_verb",
            "The timing reason gets specific inside Leadpoet before copy starts.": "prepositional_tuck_in",
            "The Leadpoet signal should stay close to the account change.": "product_modifier",
            "Put Leadpoet inside the account-change check.": "product_as_object",
            "Buyer timing changes before Leadpoet earns the mention.": "clause_embedded",
            "The useful Leadpoet question is why this account now.": "product_modifier",
            "Buyer timing should choose the account before copy.": "no_leadpoet",
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(leadpoet_actual_syntax(text), expected)

    def test_leadpoet_bad_phrase_counts_include_active_formulaic_rules(self):
        text = "\n\n".join(
            [
                "Leadpoet is building the intent layer.",
                "For Leadpoet, timing comes before copy.",
                "Leadpoet should explain why now.",
                "This is where Leadpoet focuses: timing before copy.",
                "That’s where Leadpoet focuses on account context.",
                "That is the Leadpoet point.",
                "The Leadpoet signal is buyer timing.",
                "Leadpoet belongs close to account timing.",
                "Leadpoet owns the timing question.",
                "Leadpoet's job is account timing.",
                "The layer worth building starts before sequence copy.",
            ]
        )

        counts = leadpoet_bad_phrase_counts(text)

        self.assertEqual(counts["leadpoet is building"], 1)
        self.assertEqual(counts["leadpoet is"], 1)
        self.assertEqual(counts["for leadpoet"], 1)
        self.assertEqual(counts["leadpoet modal lead-in"], 1)
        self.assertEqual(counts["that/this is where leadpoet"], 2)
        self.assertEqual(counts["that is the leadpoet closer"], 1)
        self.assertEqual(counts["the leadpoet noun-is definition"], 1)
        self.assertEqual(counts["leadpoet fallback lead-in"], 1)
        self.assertEqual(counts["leadpoet subject-verb lead-in"], 1)
        self.assertEqual(counts["leadpoet bet/job/role"], 1)
        self.assertEqual(counts["the layer worth building"], 1)

    def test_leadpoet_sentence_position_tracks_actual_sentence_slot(self):
        cases = {
            "Leadpoet owns the timing question. Copy comes later.": ("first_sentence", True),
            "Buyer timing comes first. The useful Leadpoet question is why now.": ("last_sentence", False),
            "Buyer timing comes first. Leadpoet owns that part. Copy comes later.": ("middle_sentence", True),
            "Buyer timing is where the Leadpoet signal starts.": ("single_sentence", False),
            "Buyer timing comes first.": ("none", False),
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(leadpoet_sentence_position(text), expected[0])
                self.assertEqual(leadpoet_starts_sentence(text), expected[1])

    def test_audit_naturalness_x_warns_on_repeated_generated_layout(self):
        class RepeatedLayoutProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nThe timing question stays inside Leadpoet.",
                        "The CRM record can be correct.\n\nThe timing question stays inside Leadpoet.",
                        "Account lists can still be late.\n\nThe timing question stays inside Leadpoet.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    texts = [
                        "Static records answer who exists.\n\nThe timing question stays inside Leadpoet.",
                        "The CRM record can be correct.\n\nThe timing question stays inside Leadpoet.",
                        "Account lists can still be late.\n\nThe timing question stays inside Leadpoet.",
                        "Generic data ages quickly.\n\nThe timing question stays inside Leadpoet.",
                        "Buyer context changes prioritization.\n\nThe timing question stays inside Leadpoet.",
                    ]
                    text = texts[self.final_count % len(texts)]
                    self.final_count += 1
                    return [evaluation_json(text)]
                return ["Static records answer who exists.\n\nThe timing question stays inside Leadpoet."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=RepeatedLayoutProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(len(payload["layout_fingerprint_counts"]), 1)
            self.assertTrue(
                any("Generated post layouts are not varied enough" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_audit_naturalness_x_warns_on_repeated_leadpoet_mention_pattern(self):
        class RepetitiveMentionProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nThe Leadpoet signal points to timing.",
                        "The CRM can be correct and still late.\n\nThe Leadpoet signal points to timing.",
                        "More names do not fix judgment.\n\nThe Leadpoet signal points to timing.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct and still late.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    line = first_lines[self.final_count % len(first_lines)]
                    self.final_count += 1
                    return [evaluation_json(f"{line}\n\nThe Leadpoet signal points to timing.")]
                return ["The Leadpoet signal points to timing."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=RepetitiveMentionProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["leadpoet_mention_pattern_counts"]["leadpoet_signal"], 5)
            self.assertTrue(
                any("Leadpoet mention phrasing is too concentrated" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_audit_naturalness_x_warns_on_repeated_leadpoet_actual_syntax(self):
        class RepetitiveSyntaxProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nThe Leadpoet signal points to timing.",
                        "The CRM can be correct and still late.\n\nThe useful Leadpoet question is why now.",
                        "More names do not fix judgment.\n\nThe Leadpoet edge stays there.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct and still late.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    endings = [
                        "The Leadpoet signal points to timing.",
                        "The useful Leadpoet question is why now.",
                        "The Leadpoet edge stays there.",
                        "The Leadpoet constraint stays concrete.",
                        "The Leadpoet side asks why this account now.",
                    ]
                    index = self.final_count % len(first_lines)
                    self.final_count += 1
                    return [evaluation_json(f"{first_lines[index]}\n\n{endings[index]}")]
                return ["The Leadpoet signal points to timing."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=RepetitiveSyntaxProvider()),
                count=5,
                trend_limit=5,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["leadpoet_actual_syntax_counts"]["product_modifier"], 5)
            self.assertTrue(
                any("Leadpoet mention syntax" in warning for warning in payload["warnings"])
            )
            store.close()

    def test_audit_naturalness_x_can_fail_on_warning(self):
        class RepetitiveMentionProvider:
            def __init__(self):
                self.final_count = 0

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Static records answer who exists.\n\nLeadpoet owns the timing question.",
                        "The CRM can be correct and still late.\n\nLeadpoet owns the timing question.",
                        "More names do not fix judgment.\n\nLeadpoet owns the timing question.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    first_lines = [
                        "Static records answer who exists.",
                        "The CRM can be correct and still late.",
                        "More names do not fix judgment.",
                        "Context only matters when it changes action.",
                        "A sales agent needs timing before copy.",
                    ]
                    line = first_lines[self.final_count % len(first_lines)]
                    self.final_count += 1
                    return [evaluation_json(f"{line}\n\nLeadpoet owns the timing question.")]
                return ["Leadpoet owns the timing question."]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            for index in range(5):
                store.save_trend_item(
                    TrendItem(
                        title=f"Trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A trend about GTM context.",
                        why_it_matters="It shows timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )
            store.close()

            with patch("leadpoet_social_brain.cli.load_configured_provider", return_value=RepetitiveMentionProvider()):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli_main(
                        [
                            "--db",
                            str(db_path),
                            "audit-naturalness-x",
                            "--count",
                            "5",
                            "--trend-limit",
                            "5",
                            "--fail-on-warning",
                        ]
                    )

            self.assertEqual(exit_code, 1)
            self.assertIn("Warnings:", output.getvalue())
            self.assertIn("Leadpoet mention", output.getvalue())

    def test_audit_naturalness_x_warns_when_not_all_loaded_trends_are_covered(self):
        class CoverageProvider:
            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Do not write the post yet" in prompt:
                    return [strategy_json()]
                if "Write 3 X draft options" in prompt:
                    return [
                        "Buyer timing is still the useful sales AI question.\n\nLeadpoet has to make the reason visible.",
                        "A correct account record can still be late.\n\nThe Leadpoet signal has to explain why now.",
                        "More names do not fix prioritization.\n\nTiming before copy is the part Leadpoet keeps visible.",
                    ][:variants]
                if "Choose the best X post" in prompt:
                    return [evaluation_json("Buyer timing is still the useful sales AI question.\n\nLeadpoet has to make the reason visible.")]
                return ["Leadpoet has to make timing visible."]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            for index in range(7):
                store.save_trend_item(
                    TrendItem(
                        title=f"Current trend {index}",
                        source_type="web",
                        topic="sales AI",
                        summary="A current GTM trend.",
                        why_it_matters="It proves timing matters.",
                        leadpoet_angle="Buyer timing needs proprietary account context.",
                        date=utc_now(),
                    )
                )

            payload = build_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=CoverageProvider()),
                count=5,
                trend_limit=0,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertTrue(payload["loaded_all_matching_trends"])
            self.assertEqual(payload["trend_count"], 7)
            self.assertEqual(payload["covered_trend_count"], 5)
            self.assertEqual(payload["uncovered_trend_count"], 2)
            self.assertTrue(any("were not covered" in warning for warning in payload["warnings"]))
            store.close()

    def test_generation_allows_supported_first_person_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            provider = SequentialProvider(
                [
                    strategy_json(
                        source_material="User supplied a first-person research setup.",
                        evidence_boundary="The writer can mention reading AI SDR posts because the request supplied it.",
                    ),
                    "I spent the morning reading AI SDR posts. The useful ones kept coming back to buyer timing.",
                    "The better sales AI argument starts with why this account cares now.",
                    "Leadpoet is for timing, not louder outbound.",
                    evaluation_json(
                        "I spent the morning reading AI SDR posts.\n\n"
                        "The useful ones kept coming back to the same question: why this account, now?\n\n"
                        "That question gets specific inside Leadpoet."
                    ),
                ]
            )

            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="I spent the morning reading AI SDR posts",
                    extra_context="I spent the morning reading AI SDR posts and noticed they all run into buyer timing.",
                    length_mode="standard",
                )
            )[0]

            self.assertIn("I spent the morning", draft.text)
            self.assertEqual(len(provider.prompts), 3)
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

            self.assertIn("Voice Profile", section.content)
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

    def test_plan_week_x_surfaces_quality_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            store.save_trend_item(
                TrendItem(
                    title="Thin AI agent trend",
                    source_type="news",
                    topic="sales LLMs",
                    summary="A company is adding AI agents to operations.",
                    why_it_matters="Could connect to sales context if the bridge is real.",
                    leadpoet_angle="Tie the signal back to account context.",
                    date=utc_now(),
                )
            )
            provider = SequentialProvider(
                [
                    json.dumps(
                        {
                            "thesis": "Account context matters.",
                            "why_this_is_worth_posting": "weak topical bridge",
                            "concrete_detail": "account context",
                            "source_bridge": "weak",
                            "evidence_boundary": "Do not invent proof.",
                            "success_criteria": "Requires a real bridge.",
                        }
                    ),
                    "Company has AI agents. Sales has the same problem.",
                    "The part to watch is context.",
                    "More sending is easy.",
                    json.dumps(
                        {
                            "ranked_drafts": [1, 2, 3],
                            "quality_notes": "No real source bridge.",
                            "warnings": ["not_post_worthy"],
                            "suggested_final_version": "Leadpoet is not another way to send more.\n\nIt is a better reason to know this account is worth attention now.",
                        }
                    ),
                ]
            )

            result = plan_week_x(
                store,
                SocialBrainGenerator(store, provider=provider),
                week_start="2026-06-29",
                posts=1,
                use_trends=True,
                trend_window_days=30,
                topic_mix="sales LLMs:1",
                cadence="daily_mornings",
                default_post_type="founder_insight",
                variants_per_slot=1,
            )

            self.assertTrue(any("Slot 1: Quality warning" in warning for warning in result.warnings))
            self.assertIn("Quality warnings:", result.queued[0].reviewer_notes)
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
            self.assertNotIn("Quality mode:", result.item.reviewer_notes)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].id, result.item.id)
            self.assertEqual(saved_draft.context_snapshot["quality_mode"], "quality_review")
            self.assertIn("strategy_summary", saved_draft.context_snapshot)
            self.assertIn("User instruction: Write about why sales AI needs buyer timing", provider.prompts[0])
            self.assertIn("Create one complete standalone X post for review.", provider.prompts[0])
            self.assertNotIn("Content Reviewer", "\n".join(provider.prompts))
            store.close()

    def test_generated_text_dash_cleanup_keeps_sentence_flow(self):
        cleaned = clean_generated_text(
            "That layer — proprietary intent and account context — is what Leadpoet owns.",
            max_chars=280,
        )

        self.assertEqual(cleaned, "That layer: proprietary intent and account context is what Leadpoet owns.")
        self.assertNotIn("—", cleaned)
        self.assertNotIn("That layer. proprietary", cleaned)

    def test_create_post_x_normalizes_user_facing_post_type_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=FakeProvider(["Leadpoet makes buyer timing obvious."])),
                instruction="Write a light funny post about sales AI needing better judgment.",
                topic="Sales LLMs",
                post_type="funny_post",
            )

            saved_draft = store.get_generated_draft(result.item.draft_id)

            self.assertEqual(result.item.post_type, "light_funny")
            self.assertEqual(saved_draft.request.post_type, "light_funny")
            self.assertEqual(saved_draft.context_snapshot["request"]["post_type"], "light_funny")
            store.close()

    def test_create_post_x_uses_selected_trend_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            selected = store.save_trend_item(
                TrendItem(
                    title="Selected buyer timing trend",
                    source_type="web_daily",
                    topic="Leadpoet",
                    summary="Teams are choosing sales AI with better account timing.",
                    why_it_matters="It supports the Leadpoet timing thesis.",
                    leadpoet_angle="Buyer timing is the wedge.",
                    date=utc_now(),
                )
            )
            store.save_trend_item(
                TrendItem(
                    title="Unselected volume trend",
                    source_type="web_daily",
                    topic="Leadpoet",
                    summary="A different trend should not be pulled in.",
                    why_it_matters="Less relevant.",
                    leadpoet_angle="Ignore this.",
                    date=utc_now(),
                )
            )

            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=FakeProvider(["Buyer timing is the real sales AI edge."])),
                instruction="Use the selected trend.",
                topic="Leadpoet",
                post_type="founder_insight",
                trend_ids=[selected.id],
            )
            saved_draft = store.get_generated_draft(result.item.draft_id)

            self.assertEqual(result.item.trend_ids, [selected.id])
            self.assertEqual(saved_draft.context_snapshot["selected_trends"][0]["id"], selected.id)
            store.close()

    def test_create_post_x_warns_and_continues_without_trends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=FakeProvider(["Leadpoet makes buyer timing easier to act on."])),
                instruction="Make the buyer timing point.",
                topic="Leadpoet",
                post_type="founder_insight",
                use_trends=True,
            )

            self.assertEqual(result.warnings, ["No relevant unused trends found. Created an evergreen post instead."])
            self.assertEqual(result.item.status, "needs_review")
            self.assertEqual(result.item.trend_ids, [])
            store.close()

    def test_create_post_x_surfaces_quality_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            strategy = json.dumps(
                {
                    "thesis": "Buyer timing beats generic volume.",
                    "angle": "weak topical peg",
                    "why_this_is_worth_posting": "thin trend bridge",
                    "concrete_detail": "buyer timing",
                    "source_material": "User request",
                    "source_bridge": "weak",
                    "evidence_boundary": "Do not invent proof.",
                    "post_shape": "observation",
                    "forbidden_moves": ["shallow topical peg"],
                    "success_criteria": "Requires a real source bridge.",
                }
            )
            evaluation = json.dumps(
                {
                    "ranked_drafts": [1, 2, 3],
                    "quality_notes": "The source bridge is too loose.",
                    "warnings": ["not_post_worthy", "source bridge is weak"],
                    "suggested_final_version": "Leadpoet is not another volume lever.\n\nIt gives a better reason to choose this account this week.",
                }
            )
            provider = SequentialProvider(
                [
                    strategy,
                    "Ericsson has AI agents. Sales has the same problem.",
                    "The part to watch is context.",
                    "More sending is easy. Timing is hard.",
                    evaluation,
                ]
            )

            result = create_post_x(
                store,
                SocialBrainGenerator(store, provider=provider),
                instruction="Use this trend angle.",
                topic="sales LLMs",
                post_type="founder_insight",
            )

            self.assertTrue(any("no draft had a strong source bridge" in warning for warning in result.warnings))
            self.assertTrue(any("source bridge is weak" in warning for warning in result.warnings))
            self.assertIn("Quality warnings:", result.item.reviewer_notes)
            self.assertIn("source bridge is weak", result.item.reviewer_notes)
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

    def test_assistant_endpoint_requires_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.save_web_auth_user(DEFAULT_AUTH_USERNAME, "test-password")
            store.close()

            allowed, captured = self.handler_guard(db_path, "/api/assistant/chat")

            self.assertFalse(allowed)
            self.assertEqual(captured["status"], 401)
            self.assertEqual(captured["payload"]["error"], "Authentication required.")

    def test_web_api_uses_authenticated_users_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            auth_store = SQLiteStore(db_path)
            default_user = auth_store.save_web_auth_user(DEFAULT_AUTH_USERNAME, "default-password")
            alice_user = auth_store.save_web_auth_user("alice", "alice-password")
            default_token = create_session_cookie_value(auth_store, default_user.username)
            alice_token = create_session_cookie_value(auth_store, alice_user.username)
            auth_store.close()

            default_store = SQLiteStore(db_path, workspace_id=default_user.workspace_id)
            default_store.save_post(
                SocialPost(
                    platform="x",
                    text="Leadpoet post for the default workspace.",
                    url="https://x.com/gavin/status/web-default",
                    published_at="2026-06-27T12:00:00+00:00",
                    author="Gavin",
                    topic="Leadpoet",
                    post_type="founder_insight",
                )
            )
            default_store.close()

            alice_store = SQLiteStore(db_path, workspace_id=alice_user.workspace_id)
            alice_store.save_post(
                SocialPost(
                    platform="x",
                    text="AcmeOS post for Alice's workspace.",
                    url="https://x.com/alice/status/web-alice",
                    published_at="2026-06-27T13:00:00+00:00",
                    author="Alice",
                    topic="AcmeOS",
                    post_type="founder_insight",
                )
            )
            alice_store.close()

            default_workspace = request_workspace_id(
                {"Cookie": f"{SESSION_COOKIE}={default_token}"},
                db_path,
            )
            alice_workspace = request_workspace_id(
                {"Cookie": f"{SESSION_COOKIE}={alice_token}"},
                db_path,
            )
            self.assertEqual(default_workspace, "default")
            self.assertEqual(alice_workspace, "user-alice")

            default_status, default_payload = handle_api_request(
                "GET",
                "/api/dashboard",
                db_path=db_path,
                workspace_id=default_workspace,
            )
            alice_status, alice_payload = handle_api_request(
                "GET",
                "/api/dashboard",
                db_path=db_path,
                workspace_id=alice_workspace,
            )

            self.assertEqual(default_status, 200)
            self.assertEqual(alice_status, 200)
            self.assertEqual(default_payload["status"]["workspace_id"], "default")
            self.assertEqual(alice_payload["status"]["workspace_id"], "user-alice")
            self.assertEqual([post["author"] for post in default_payload["recent_posts"]], ["Gavin"])
            self.assertEqual([post["author"] for post in alice_payload["recent_posts"]], ["Alice"])
            self.assertNotIn("AcmeOS", default_payload["recent_posts"][0]["text"])
            self.assertNotIn("Leadpoet", alice_payload["recent_posts"][0]["text"])

    def test_assistant_starter_prompts_and_missing_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            prompts_status, prompts = handle_api_request("GET", "/api/assistant/starter-prompts", db_path=db_path)
            self.assertEqual(prompts_status, 200)
            self.assertIn("What patterns are working best?", prompts["items"])

            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=None):
                status, payload = handle_api_request(
                    "POST",
                    "/api/assistant/chat",
                    json.dumps({"message": "What should I post about today?"}),
                    db_path=db_path,
                )

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], ASSISTANT_MISSING_PROVIDER_MESSAGE)

    def test_assistant_context_includes_posts_metrics_and_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            post = store.save_post(
                SocialPost(
                    platform="x",
                    text="Sales AI only gets useful when it knows why this buyer cares today.",
                    url="https://x.com/gavin/status/assistant101",
                    published_at="2026-06-22T12:00:00+00:00",
                    author="Gavin",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    status="published",
                )
            )
            store.add_metrics(
                post.id,
                EngagementMetrics(impressions=1000, likes=50, replies=20, reposts=10, bookmarks=30),
            )
            store.save_content_queue_item(
                ContentQueueItem(
                    text="Scheduled post that might be too generic.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00+00:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )

            voice_context = build_assistant_context(store, "What are the best posts to use as Voice Examples?")
            queue_context = build_assistant_context(store, "Which scheduled posts feel weakest?")

            self.assertEqual(voice_context["voice"]["candidate_examples"][0]["id"], post.id)
            self.assertEqual(voice_context["voice"]["candidate_examples"][0]["metrics"]["impressions"], 1000)
            self.assertIn("engagement rate", voice_context["voice"]["candidate_examples"][0]["key_metric"])
            self.assertEqual(queue_context["queue"]["scheduled"][0]["summary"], "Scheduled post that might be too generic.")
            store.close()

    def test_assistant_chat_is_read_only_and_marks_suggested_actions_for_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = store.save_post(
                SocialPost(
                    platform="x",
                    text="A strong founder post about buyer timing and sales AI.",
                    url="https://x.com/gavin/status/assistant201",
                    published_at="2026-06-22T12:00:00+00:00",
                    author="Gavin",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    status="published",
                )
            )
            store.add_metrics(post.id, EngagementMetrics(impressions=2000, likes=90, replies=25, bookmarks=50))
            store.close()

            provider = FakeProvider(["Use the June 22 buyer timing post as a Voice Example. It has strong engagement and teaches plain founder contrast."])
            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=provider):
                status, payload = handle_api_request(
                    "POST",
                    "/api/assistant/chat",
                    json.dumps({"message": "What are the best posts to use as Voice Examples?"}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertIn("Voice Example", payload["answer"])
            self.assertTrue(payload["suggested_actions"])
            self.assertTrue(all(action["requires_confirmation"] for action in payload["suggested_actions"]))
            self.assertEqual(payload["suggested_actions"][0]["payload"]["post_id"], post.id)
            self.assertIn("assistant201", provider.prompts[0])

            store = SQLiteStore(db_path)
            self.assertEqual(store.notes_by_post_id([post.id]), {})
            store.close()

    def test_assistant_context_omits_secrets_and_reports_missing_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            secret = "super-secret-token-value"
            store.save_sync_metadata(
                SyncMetadata(
                    key="x_posts_sync",
                    status="failed",
                    error_message=secret,
                    summary={"token": secret},
                )
            )
            with patch.dict(os.environ, {"X_BEARER_TOKEN": secret, "OPENROUTER_API_KEY": secret}, clear=False):
                context = build_assistant_context(store, "What are the best posts to use as Voice Examples?")
                prompt = build_assistant_prompt("What are the best posts to use as Voice Examples?", context)

            self.assertNotIn(secret, json.dumps(context))
            self.assertNotIn(secret, prompt)
            self.assertIn("No X posts are stored yet.", context["data_gaps"])
            self.assertIn("No current Voice summary has been built yet.", context["data_gaps"])
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
            save_avoid_phrases(store, ["game changer"])
            trend = store.save_trend_item(
                TrendItem(
                    title="ZoomInfo context layer",
                    source_type="web",
                    topic="sales LLMs",
                    summary="Context layer",
                    why_it_matters="It proves context matters.",
                    leadpoet_angle="Buyer timing beats generic data.",
                    date=utc_now(),
                )
            )
            item = store.save_content_queue_item(
                ContentQueueItem(
                    text="Original queued post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    trend_ids=[trend.id],
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
            self.assertIn("Avoid Phrases:", provider.prompts[0])
            self.assertIn("- game changer", provider.prompts[0])
            for prefix in [
                "Primary structure for this request:",
                "Primary layout target for this request:",
                "Primary closing target for this request:",
                "Primary human rhythm target for this request:",
                "Primary product mention lane for this request:",
                "Primary product mention placement target for this request:",
            ]:
                writer_line = next(line for line in provider.prompts[1].splitlines() if line.startswith(prefix))
                evaluator_line = next(line for line in provider.prompts[2].splitlines() if line.startswith(prefix))
                self.assertEqual(writer_line, evaluator_line)

            store = SQLiteStore(db_path)
            unchanged = store.get_content_queue_item(item.id)
            self.assertEqual(unchanged.text, "Original queued post.")
            self.assertEqual(store.list_queued_post_versions(item.id)[0].text, payload["versions"][0]["text"])
            store.close()

    def test_web_revision_repairs_overlong_output_instead_of_truncating(self):
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

            overlong = (
                "The AI agent that booked 614 meetings for SaaStr proved one thing:\n\n"
                "Outbound only works when you know the buyer is actually moving.\n\n"
                "Real-time CRM lookups. Routing logic. Months of training on their own data. "
                "That's what it took to find timing.\n\n"
                "Most teams don't have that. So they build outreach around volume, generic timing, "
                "and a workflow that still depends on someone manually noticing when the buyer is ready to listen."
            )
            repaired = (
                "The SaaStr AI agent proved the real point: outbound only works when buyer timing is real. "
                "Leadpoet is built around that timing, not more volume."
            )
            strategy = json.dumps(
                {
                    "thesis": "Buyer timing beats volume.",
                    "concrete_detail": "SaaStr booked 614 meetings.",
                    "source_bridge": "Use the case study as market proof for timing.",
                    "evidence_boundary": "Do not invent proof.",
                }
            )
            evaluation = json.dumps(
                {
                    "ranked_drafts": [1, 2, 3],
                    "quality_notes": "Strong but too long.",
                    "warnings": [],
                    "suggested_final_version": overlong,
                }
            )
            provider = SequentialProvider(
                [
                    strategy,
                    overlong,
                    "Leadpoet makes buyer timing obvious.",
                    "More volume is not the wedge.",
                    "Leadpoet repairs the long option around buyer timing.",
                    evaluation,
                    repaired,
                ]
            )

            with patch("leadpoet_social_brain.web.app.load_provider_from_env", return_value=provider):
                status, payload = handle_api_request(
                    "POST",
                    f"/api/queue/{item.id}/revise",
                    json.dumps({"instruction": "Make it sharper.", "variants": 1}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["versions"][0]["text"], repaired)
            self.assertLessEqual(len(payload["versions"][0]["text"]), 280)
            self.assertIn("Character limit: 280", provider.prompts[1])
            self.assertIn("Shorten this queued X revision option to 280 characters or fewer.", provider.prompts[2])
            self.assertIn("Shorten this queued X revision to 280 characters or fewer.", provider.prompts[4])
            self.assertNotIn("Most teams don't have that. So they build outreach around volume", payload["versions"][0]["text"])

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

    def test_web_quality_revision_does_not_auto_approve(self):
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
            trend = store.save_trend_item(
                TrendItem(
                    title="Buyer timing becomes the buying criterion",
                    source_type="web_daily",
                    topic="Leadpoet",
                    summary="Teams are evaluating sales AI by account timing.",
                    why_it_matters="It supports the Leadpoet wedge.",
                    leadpoet_angle="Buyer timing beats generated email volume.",
                    date=utc_now(),
                )
            )
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
                            "use_trends": True,
                            "trend_ids": [trend.id],
                        }
                    ),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "needs_review")
            self.assertIn("buyer timing", payload["generated_text"].lower())
            self.assertIn("sales ai advantage", payload["generated_text"].lower())
            self.assertNotIn(
                "For Leadpoet, proprietary buyer context and timing signals are where useful sales AI starts.",
                payload["generated_text"],
            )
            self.assertNotIn("For Leadpoet", payload["generated_text"])
            self.assertNotIn("Leadpoet is", payload["generated_text"])
            self.assertEqual(payload["item"]["status"], "needs_review")
            self.assertEqual(payload["item"]["scheduled_at"], "2026-06-30T10:00")
            self.assertEqual(payload["item"]["quality_mode"], "quality_review")
            self.assertIn("buyer timing", payload["item"]["focus"].lower())
            self.assertEqual(payload["queue_id"], payload["item"]["id"])

            store = SQLiteStore(db_path)
            saved = store.get_content_queue_item(payload["queue_id"])
            self.assertEqual(saved.status, "needs_review")
            self.assertEqual(saved.trend_ids, [trend.id])
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
            pattern = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/dashboardpattern",
                source_text="A good AI post starts with the weak default.",
                topic="sales LLMs",
                category="sales",
                pattern_name="Weak default teardown",
                structure="weak default, sharper replacement",
                why_it_worked="It gives the reader a better model quickly.",
                window="weekly",
            )
            store.mark_viral_pattern_status(pattern.id, "saved")
            monthly_pattern = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/monthlypattern",
                source_text="A monthly pattern should capture a larger repeated market mechanic.",
                topic="gtm",
                category="gtm",
                pattern_name="Monthly market mechanic",
                structure="big market signal, repeated behavior, reusable rule",
                window="monthly",
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
            self.assertEqual(payload["pattern_groups"]["weekly"][0]["id"], pattern.id)
            self.assertEqual(payload["pattern_groups"]["monthly"][0]["id"], monthly_pattern.id)
            self.assertNotIn("daily", payload["pattern_groups"])
            self.assertEqual(payload["saved_patterns"][0]["id"], pattern.id)
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
            self.assertIn("company/product", topic_values)
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

    def test_web_trend_scout_uses_saved_workspace_context_in_digest_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            save_positioning_rules(
                store,
                [
                    "AcmeOS is the core account intelligence layer: first-party CRM context, renewal timing, and buyer intent.",
                    "Competitor agents are proof that account timing matters, not the source of AcmeOS's wedge.",
                ],
            )
            store.save_context_section(
                ContextSection(
                    kind="product",
                    name="acme_product",
                    content="AcmeOS helps revenue teams spot renewal timing and expansion intent before AI outreach.",
                )
            )
            provider = FakeProvider(["{}"])

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"{timespan} sales AI account signals trend",
                        "url": f"https://example.com/acme/{timespan}",
                        "summary": "AI sales agents are using CRM account signals before outreach.",
                        "seendate": "20260626120000",
                        "sourceCommonName": "Example News",
                    }
                ]

            refresh_web_trends(
                store,
                fetch_articles=fake_fetch,
                limit=1,
                provider=provider,
                fetch_source_page=lambda _: "<html><title>Account signals</title><p>Sales AI tools are using CRM account signals and renewal timing before outreach.</p></html>",
            )

            prompt = provider.prompts[0]
            self.assertIn("Gavin at AcmeOS", prompt)
            self.assertIn("AcmeOS is the core account intelligence layer", prompt)
            self.assertIn("renewal timing and expansion intent", prompt)
            self.assertIn("never make another tool, agent, or case study supply AcmeOS's saved wedge", prompt)
            self.assertIn("Avoid wording like \"AcmeOS can leverage insights from", prompt)
            self.assertNotIn("Gavin at Leadpoet", prompt)
            store.close()

    def test_web_trend_fallback_angle_uses_saved_workspace_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            save_positioning_rules(
                store,
                [
                    "AcmeOS is the core account intelligence layer for renewal timing.",
                ],
            )

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"{timespan} sales AI account signals trend",
                        "url": f"https://example.com/acme-fallback/{timespan}",
                        "summary": "AI sales tools are adding account signals to CRM workflows.",
                        "seendate": "20260626120000",
                        "sourceCommonName": "Example News",
                    }
                ]

            refreshed = refresh_web_trends(store, fetch_articles=fake_fetch, limit=1, enrich=False)

            self.assertIn("AcmeOS", refreshed.daily[0].leadpoet_angle)
            self.assertNotIn("Leadpoet", refreshed.daily[0].leadpoet_angle)
            store.close()

    def test_web_trend_groups_backfill_older_unused_daily_trends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            now = datetime(2026, 6, 26, 12, 30, tzinfo=timezone.utc)
            recent = store.save_trend_item(
                TrendItem(
                    title="Fresh daily sales AI signal",
                    source_type="web_daily",
                    topic="sales AI",
                    summary="Fresh signal.",
                    why_it_matters="Useful today.",
                    leadpoet_angle="Tie it to buyer timing.",
                    urgency="high",
                    date=now.isoformat(),
                )
            )
            older = [
                store.save_trend_item(
                    TrendItem(
                        title=f"Older daily sales AI signal {index}",
                        source_type="web_daily",
                        topic="sales AI",
                        summary="Older but unused signal.",
                        why_it_matters="Still useful context.",
                        leadpoet_angle="Tie it to buyer timing.",
                        urgency="medium",
                        date=(now - timedelta(days=3 + index)).isoformat(),
                    )
                )
                for index in range(4)
            ]
            store.save_trend_item(
                TrendItem(
                    title="Weekly signal should stay in weekly",
                    source_type="web_weekly",
                    topic="sales AI",
                    summary="Weekly signal.",
                    why_it_matters="Useful weekly.",
                    leadpoet_angle="Tie it to buyer timing.",
                    date=(now - timedelta(days=3)).isoformat(),
                )
            )

            grouped = web_trend_groups(store, now=now)

            self.assertEqual([trend.id for trend in grouped.daily], [recent.id, *[trend.id for trend in older]])
            self.assertEqual(len(grouped.daily), 5)
            self.assertTrue(all(trend.source_type == "web_daily" for trend in grouped.daily))
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

    def test_pattern_scout_manual_batch_and_api_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            manual = add_pattern_example(
                store,
                source_url="https://x.com/founder/status/pattern1",
                source_author="Founder",
                source_text="The old way was volume. The new way is timing.",
                topic="sales LLMs",
                category="sales",
                pattern_name="Old way, new way",
                hook_style="sharp contrast",
                structure="old belief, new model",
                pacing="short",
                tone="plainspoken",
                formatting="two short paragraphs",
                why_it_worked="It names a stale default and replaces it quickly.",
                adaptation_rule="Use the structure, not the wording.",
                likes=120,
                replies=9,
                window="daily",
            )
            csv_path = Path(tmpdir) / "patterns.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_url",
                        "source_text",
                        "source_summary",
                        "topic",
                        "category",
                        "pattern_name",
                        "hook_style",
                        "structure",
                        "pacing",
                        "tone",
                        "formatting",
                        "why_it_worked",
                        "adaptation_rule",
                        "engagement_notes",
                        "likes",
                        "window",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "source_url": "https://x.com/founder/status/pattern2",
                        "source_text": "A useful product update shows the before state first.",
                        "source_summary": "Product update structure",
                        "topic": "product",
                        "category": "product",
                        "pattern_name": "Before state first",
                        "hook_style": "specific before state",
                        "structure": "before state, product change, practical result",
                        "pacing": "compact",
                        "tone": "useful",
                        "formatting": "short lines",
                        "why_it_worked": "It makes the product point concrete.",
                        "adaptation_rule": "Use the structure, not the wording.",
                        "engagement_notes": "High bookmark signal",
                        "likes": "88",
                        "window": "weekly",
                    }
                )

            batch = ingest_patterns_batch_csv(store, csv_path)
            store.close()

            status, listed = handle_api_request("GET", "/api/patterns?window=daily", db_path=db_path)
            detail_status, detail = handle_api_request("GET", f"/api/patterns/{manual.id}", db_path=db_path)
            save_status, saved = handle_api_request("POST", f"/api/patterns/{manual.id}/save", b"{}", db_path=db_path)
            saved_list_status, saved_list = handle_api_request("GET", "/api/patterns?status=saved", db_path=db_path)
            unsave_status, unsaved = handle_api_request("POST", f"/api/patterns/{manual.id}/unsave", b"{}", db_path=db_path)
            unsaved_list_status, unsaved_list = handle_api_request("GET", "/api/patterns?status=saved", db_path=db_path)
            archive_status, archived = handle_api_request("POST", f"/api/patterns/{manual.id}/archive", b"{}", db_path=db_path)

            self.assertEqual(batch.imported, 1)
            self.assertEqual(batch.categories["product"], 1)
            self.assertEqual(status, 200)
            self.assertEqual(listed["items"][0]["id"], manual.id)
            self.assertEqual(detail_status, 200)
            self.assertEqual(detail["pattern"]["metrics"]["likes"], 120)
            self.assertEqual(save_status, 200)
            self.assertEqual(saved["pattern"]["status"], "saved")
            self.assertEqual(saved_list_status, 200)
            self.assertEqual(saved_list["items"][0]["id"], manual.id)
            self.assertEqual(unsave_status, 200)
            self.assertEqual(unsaved["pattern"]["status"], "active")
            self.assertEqual(unsaved_list_status, 200)
            self.assertEqual(unsaved_list["items"], [])
            self.assertEqual(archive_status, 200)
            self.assertEqual(archived["pattern"]["status"], "archived")

    def test_pattern_refresh_requires_scrapingdog_without_fetcher(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeProvider([
                '{"pattern_name":"Contrast","category":"sales","structure":"old way then new way"}'
            ])

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "", "SCRAPING_DOG_API_KEY": ""}, clear=False):
                with self.assertRaises(ValueError) as error:
                    refresh_patterns(store, provider, limit=1)

            self.assertEqual(str(error.exception), PATTERN_REFRESH_MISSING_SCRAPINGDOG)
            store.close()

    def test_pattern_context_terms_prioritize_saved_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.save_context_section(
                ContextSection(
                    kind="positioning",
                    name="core",
                    content="Leadpoet is focused on RevOps, CRM account signals, and data enrichment.",
                    weight=2.0,
                )
            )

            terms = pattern_context_terms(store, limit=5)

            self.assertIn("account signals", terms[:3])
            self.assertIn("RevOps", terms)
            self.assertIn("data enrichment", terms)
            store.close()

    def test_pattern_fetch_uses_context_queries_and_filters_irrelevant_news(self):
        responses = [
            [
                {
                    "title": "Kling AI (@Kling_ai) / Posts / X",
                    "link": "https://x.com/Kling_ai",
                    "source": "X",
                    "snippet": "Kling AI (@Kling_ai) / Posts / X",
                },
                {
                    "title": "Budget fight mentions AI procurement",
                    "link": "https://example.com/politics",
                    "source": "Example News",
                    "snippet": "Governor and senate leaders negotiated a political budget deal.",
                },
                {
                    "title": "AI SDR teams add buyer intent signals",
                    "link": "https://example.com/ai-sdr-intent",
                    "source": "GTM News",
                    "snippet": "Outbound teams are using account signals and buyer timing to prioritize pipeline.",
                },
            ],
            [
                {
                    "title": "Consumer AI photo app launches new filters",
                    "link": "https://example.com/photo-app",
                    "source": "Consumer News",
                    "snippet": "The generic AI update focuses on a consumer photo app.",
                },
                {
                    "title": "Lead generation vendors invest in data enrichment",
                    "link": "https://example.com/lead-gen-enrichment",
                    "source": "GTM News",
                    "snippet": "Sales teams want account context before outreach and better intent data.",
                },
            ],
            [
                {
                    "title": "RevOps teams connect CRM data to sales automation",
                    "link": "https://example.com/revops-crm-automation",
                    "source": "RevOps Weekly",
                    "snippet": "Pipeline teams are using CRM signals to tune outbound workflow timing.",
                }
            ],
        ]
        calls: list[dict[str, str]] = []

        def fake_fetch_json(url: str, params: dict[str, str], *, timeout: int):
            calls.append(params)
            index = min(len(calls), len(responses)) - 1
            return {"news_results": responses[index]}

        with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
            with patch("leadpoet_social_brain.pattern_scout.fetch_json", side_effect=fake_fetch_json):
                candidates = fetch_scrapingdog_pattern_candidates(
                    limit=3,
                    window="weekly",
                    context_terms=["data enrichment", "CRM"],
                )

        self.assertEqual(len(candidates), 3)
        self.assertGreaterEqual(len(calls), 3)
        self.assertIn('"data enrichment"', calls[0]["query"])
        self.assertTrue(all("x.com/Kling_ai" not in str(candidate.get("source_url")) for candidate in candidates))
        self.assertEqual(
            [candidate["source_summary"] for candidate in candidates],
            [
                "AI SDR teams add buyer intent signals",
                "Lead generation vendors invest in data enrichment",
                "RevOps teams connect CRM data to sales automation",
            ],
        )

    def test_pattern_fetch_continues_after_one_scrapingdog_query_fails(self):
        def fake_fetch_json(url: str, params: dict[str, str], *, timeout: int):
            if "data enrichment" in params["query"]:
                raise RuntimeError("HTTP 400 Bad Request")
            return {
                "news_results": [
                    {
                        "title": "AI SDR platforms add buyer intent signals",
                        "link": "https://example.com/ai-sdr-intent",
                        "source": "GTM News",
                        "snippet": "Outbound teams are using account signals and buyer timing to prioritize pipeline.",
                    }
                ]
            }

        with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
            with patch("leadpoet_social_brain.pattern_scout.fetch_json", side_effect=fake_fetch_json):
                candidates = fetch_scrapingdog_pattern_candidates(
                    limit=1,
                    window="weekly",
                    context_terms=["data enrichment"],
                )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_summary"], "AI SDR platforms add buyer intent signals")

    def test_pattern_refresh_stores_provider_analysis_with_mocked_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeProvider([
                json.dumps(
                    {
                        "pattern_name": "Specific teardown",
                        "source_summary": "A post tears down a generic AI claim.",
                        "hook_style": "names the weak default",
                        "structure": "weak claim, concrete counterexample, useful rule",
                        "pacing": "fast",
                        "tone": "direct",
                        "formatting": "three short lines",
                        "why_it_worked": "It makes a familiar AI mistake feel obvious.",
                        "adaptation_rule": "Use the structure, not the wording.",
                        "category": "sales",
                        "topic": "sales LLMs",
                        "engagement_signal": "High replies",
                    }
                )
            ])

            result = refresh_patterns(
                store,
                provider,
                limit=1,
                window="daily",
                fetch_candidates=lambda limit: [
                    {
                        "source_url": "https://x.com/operator/status/pattern3",
                        "source_author": "Operator",
                        "source_text": "Most AI SDR takes are just volume with better adjectives.",
                        "likes": 200,
                        "replies": 18,
                    }
                ],
            )
            patterns = store.list_viral_patterns(status="active", window="daily")

            self.assertEqual(result.fetched, 1)
            self.assertEqual(result.stored, 1)
            self.assertEqual(patterns[0].pattern_name, "Specific teardown")
            self.assertEqual(patterns[0].category, "sales")
            self.assertEqual(patterns[0].replies, 18)
            self.assertIn("Extract the mechanic only", provider.prompts[0])
            store.close()

    def test_pattern_refresh_normalizes_analysis_lists_and_enforces_adaptation_rule(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeProvider([
                json.dumps(
                    {
                        "pattern_name": "Integration proof",
                        "source_summary": "A relevant GTM integration announcement.",
                        "structure": ["announce integration", "show user benefit"],
                        "why_it_worked": ["clear partner credibility", "specific workflow value"],
                        "adaptation_rule": "Use this flow for relevant product proof.",
                        "category": "gtm",
                    }
                )
            ])

            refresh_patterns(
                store,
                provider,
                limit=1,
                window="monthly",
                fetch_candidates=lambda limit: [
                    {
                        "source_url": "https://example.com/gtm-integration",
                        "source_author": "Example News",
                        "source_text": "A GTM platform connected account signals to AI sales workflows for RevOps teams.",
                    }
                ],
            )
            pattern = store.list_viral_patterns(status="active", window="monthly")[0]

            self.assertEqual(pattern.structure, "announce integration; show user benefit")
            self.assertEqual(pattern.why_it_worked, "clear partner credibility; specific workflow value")
            self.assertIn("Use the structure, not the wording.", pattern.adaptation_rule)
            store.close()

    def test_pattern_refresh_keeps_weekly_and_monthly_sources_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = SequentialProvider([
                json.dumps(
                    {
                        "pattern_name": "Weekly teardown",
                        "source_summary": "Weekly version.",
                        "structure": "weekly structure",
                        "why_it_worked": "fresh signal",
                        "adaptation_rule": "Use the structure, not the wording.",
                        "category": "sales",
                    }
                ),
                json.dumps(
                    {
                        "pattern_name": "Monthly teardown",
                        "source_summary": "Monthly version.",
                        "structure": "monthly structure",
                        "why_it_worked": "recurring signal",
                        "adaptation_rule": "Use the structure, not the wording.",
                        "category": "sales",
                    }
                ),
            ])
            candidate = {
                "source_url": "https://x.com/operator/status/same-source",
                "source_text": "The same source can be analyzed into different cadence windows.",
            }

            refresh_patterns(store, provider, limit=1, window="weekly", fetch_candidates=lambda limit: [candidate])
            refresh_patterns(store, provider, limit=1, window="monthly", fetch_candidates=lambda limit: [candidate])

            weekly = store.list_viral_patterns(status="active", window="weekly")
            monthly = store.list_viral_patterns(status="active", window="monthly")
            self.assertEqual(len(weekly), 1)
            self.assertEqual(len(monthly), 1)
            self.assertNotEqual(weekly[0].id, monthly[0].id)
            self.assertEqual(weekly[0].pattern_name, "Weekly teardown")
            self.assertEqual(monthly[0].pattern_name, "Monthly teardown")
            store.close()

    def test_pattern_refresh_skips_x_profile_titles_without_post_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeProvider([
                json.dumps(
                    {
                        "pattern_name": "Observed pattern",
                        "adaptation_rule": "Use the structure, not the wording.",
                        "category": "other",
                    }
                )
            ])

            result = refresh_patterns(
                store,
                provider,
                limit=1,
                window="daily",
                fetch_candidates=lambda limit: [
                    {
                        "source_url": "https://x.com/howardlindzon",
                        "source_author": "X",
                        "source_text": "Howard Lindzon (@howardlindzon) / Posts / X",
                    }
                ],
            )

            self.assertEqual(result.fetched, 1)
            self.assertEqual(result.stored, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(provider.prompts, [])
            self.assertEqual(store.list_viral_patterns(status="active", window="daily"), [])
            store.close()

    def test_pattern_groups_hide_placeholder_patterns_without_analysis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            for index in range(6):
                add_pattern_example(
                    store,
                    source_url=f"https://x.com/howardlindzon/{index}",
                    source_author="X",
                    source_text="Howard Lindzon (@howardlindzon) / Posts / X",
                    source_summary="Howard Lindzon (@howardlindzon) / Posts / X",
                    collected_at=f"2026-06-27T12:0{index}:00+00:00",
                    window="weekly",
                )
            specific = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/pattern3",
                source_author="Operator",
                source_text="Most AI SDR takes are just volume with better adjectives and not enough buyer context.",
                pattern_name="Specific teardown",
                structure="weak claim, concrete counterexample, useful rule",
                why_it_worked="It makes a familiar AI mistake feel obvious.",
                collected_at="2026-06-20T12:00:00+00:00",
                window="weekly",
            )
            monthly = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/pattern4",
                source_author="Operator",
                source_text="A market pattern repeats when buyers quote a category shift with evidence.",
                pattern_name="Monthly category shift",
                structure="category shift, evidence, reusable takeaway",
                why_it_worked="It connects a repeated market signal to a clear lesson.",
                collected_at="2026-06-21T12:00:00+00:00",
                window="monthly",
            )

            groups = pattern_groups(store)

            self.assertEqual([pattern.id for pattern in groups["weekly"]], [specific.id])
            self.assertEqual([pattern.id for pattern in groups["monthly"]], [monthly.id])
            store.close()

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
                errors=["weekly Scrapingdog Google News failed: HTTP 429 Too Many Requests"],
                sources=["Scrapingdog Google Trends"],
            )

            with patch("leadpoet_social_brain.web.app.refresh_web_trends", return_value=groups) as refresh:
                status, payload = handle_api_request(
                    "POST",
                    "/tasks/refresh-trends",
                    json.dumps(
                        {
                            "limit": 5,
                            "no_enrich": True,
                            "scrapingdog_timeout_seconds": 12,
                            "max_news_queries_per_window": 2,
                            "deadline_seconds": 85,
                        }
                    ),
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
            refresh.assert_called_once()
            refresh_kwargs = refresh.call_args.kwargs
            self.assertFalse(refresh_kwargs["enrich"])
            self.assertEqual(refresh_kwargs["scrapingdog_timeout_seconds"], 12)
            self.assertEqual(refresh_kwargs["max_news_queries_per_window"], 2)
            self.assertEqual(refresh_kwargs["deadline_seconds"], 85)

    def test_web_dashboard_caps_recent_posts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            for index in range(251):
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
            self.assertEqual(len(payload["recent_posts"]), 250)

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
            self.assertIn("Pattern Scout", payload["connections"]["scrapingdog"]["description"])
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
            self.assertIn("Voice updated, but Voice Example details were not filled", updated["message"])
            self.assertEqual(updated["examples_used"], 1)
            self.assertTrue(updated["current"]["exists"])
            self.assertIn("Reusable writing principles", {section["title"] for section in updated["current"]["sections"]})
            self.assertEqual(updated["status"]["status"], "ready")

            status, current = handle_api_request("GET", "/api/voice/current", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertTrue(current["exists"])
            self.assertEqual(current["title"], "Voice Profile")

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

    def test_voice_facts_context_can_be_saved_and_used_in_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            store.close()

            status, initial = handle_api_request("GET", "/api/voice/facts", db_path=db_path)
            self.assertEqual(status, 200)
            initial_keys = {(item["kind"], item["name"]) for item in initial["items"]}
            self.assertIn(("writer", "writer_profile"), initial_keys)
            self.assertIn(("product", "company_product"), initial_keys)

            status, saved = handle_api_request(
                "PATCH",
                "/api/voice/facts",
                json.dumps(
                    {
                        "items": [
                            {
                                "kind": "writer",
                                "name": "writer_profile",
                                "content": "Alex writes from direct founder/operator experience.",
                            },
                            {
                                "kind": "product",
                                "name": "company_product",
                                "content": "Acme helps sales teams use buyer timing signals.",
                            },
                        ]
                    }
                ),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved["summary"], "2 fact sections saved")

            store = SQLiteStore(db_path)
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight")
            )
            self.assertIn("Facts & Context:", prompt)
            self.assertIn("Alex writes from direct founder/operator experience.", prompt)
            self.assertIn("Acme helps sales teams use buyer timing signals.", prompt)
            self.assertNotIn("Gavin/Leadpoet", prompt)
            store.close()

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

    def test_voice_suggest_note_requires_provider_and_does_not_save_automatically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            post = ingest_x_post(
                store,
                text="The dirty secret of intent data is that most vendors are selling yesterday's curiosity as today's buying signal.",
                url="https://x.com/gavin/status/voice-note-101",
                date="2026-06-24",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=3000, likes=100, replies=8, reposts=12, bookmarks=40),
                imitate=True,
            )
            store.close()

            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=None):
                status, missing = handle_api_request(
                    "POST",
                    f"/api/voice/examples/{post.id}/suggest-note",
                    json.dumps({"post_id": post.id}),
                    db_path=db_path,
                )
            self.assertEqual(status, 400)
            self.assertEqual(missing["error"], "No LLM provider configured. Set a provider before suggesting notes.")

            provider = FakeProvider([
                "Strong because it reframes intent data as a timing problem in plain language, with a concrete enemy and no forced CTA."
            ])
            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=provider):
                status, payload = handle_api_request(
                    "POST",
                    f"/api/voice/examples/{post.id}/suggest-note",
                    json.dumps({"post_id": post.id}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(
                payload["suggested_note"],
                "Strong because it reframes intent data as a timing problem in plain language, with a concrete enemy and no forced CTA.",
            )
            self.assertIn("dirty secret of intent data", provider.prompts[0])
            self.assertIn("- bookmarks: 40", provider.prompts[0])
            self.assertIn("Current Voice:", provider.prompts[0])
            self.assertNotIn("Current Koinoniko Voice:", provider.prompts[0])
            self.assertNotIn("Koinoniko Voice Example", provider.prompts[0])

            store = SQLiteStore(db_path)
            note = store.notes_by_post_id([post.id])[post.id]
            self.assertIsNone(note.worked_reason)
            store.close()

            status, saved = handle_api_request(
                "POST",
                f"/api/posts/{post.id}/voice",
                json.dumps({"voice_example": True, "why_this_worked": payload["suggested_note"]}),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved["post"]["voice"]["why_this_worked"], payload["suggested_note"])

    def test_voice_update_autofills_missing_voice_example_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            older = ingest_x_post(
                store,
                text="Two sales teams want to grow pipeline with outbound. One buys more leads. The other learns buyer timing.",
                url="https://x.com/gavin/status/voice-autofill-older",
                date="2026-06-23",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=2200, likes=80, replies=5, reposts=10, bookmarks=24),
                imitate=True,
            )
            newer = ingest_x_post(
                store,
                text="Intent data should tell you why this account cares today, not that someone clicked an article last month.",
                url="https://x.com/gavin/status/voice-autofill-newer",
                date="2026-06-24",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                metrics=EngagementMetrics(impressions=3000, likes=100, replies=8, reposts=12, bookmarks=40),
                imitate=True,
            )
            store.save_note(
                PostNote(
                    post_id=newer.id,
                    worked_reason="Original human note.",
                    hook="specific observation",
                    imitate=True,
                )
            )
            store.close()

            provider = SequentialProvider(
                [
                    json.dumps(
                        {
                            "why_this_worked": "Should not replace the human note.",
                            "hook": "should not replace hook",
                            "structure": "contrast between old signal and buyer timing",
                            "tone": "plainspoken and skeptical",
                            "cta": "none",
                            "formatting": "short two-line contrast",
                            "pattern": "old signal versus real timing",
                        }
                    ),
                    json.dumps(
                        {
                            "why_this_worked": "Strong because it turns outbound volume into a buyer timing problem in plain Koinoniko voice...",
                            "hook": "two-team contrast",
                            "structure": "contrast, implication, sharper model...",
                            "tone": "direct and founder-led",
                            "cta": "none",
                            "formatting": "short lines",
                            "pattern": "weak default versus better model",
                        }
                    ),
                ]
            )
            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=provider):
                status, payload = handle_api_request("POST", "/api/voice/update", b"{}", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(payload["auto_filled"]["updated"], 2)
            self.assertEqual(len(provider.prompts), 2)
            self.assertIn("Return only valid JSON", provider.prompts[0])
            self.assertIn("why_this_worked", provider.prompts[1])
            self.assertIn("Current Voice:", provider.prompts[0])
            self.assertNotIn("Current Koinoniko Voice:", provider.prompts[0])
            self.assertNotIn("Fill missing Voice Example details for Koinoniko", provider.prompts[0])

            store = SQLiteStore(db_path)
            notes = store.notes_by_post_id([older.id, newer.id])
            self.assertEqual(notes[newer.id].worked_reason, "Original human note.")
            self.assertEqual(notes[newer.id].hook, "specific observation")
            self.assertEqual(notes[newer.id].structure, "contrast between old signal and buyer timing")
            self.assertEqual(notes[older.id].worked_reason, "Strong because it turns outbound volume into a buyer timing problem in plain this voice.")
            self.assertEqual(notes[older.id].hook, "two-team contrast")
            self.assertEqual(notes[older.id].structure, "contrast, implication, sharper model.")
            self.assertEqual(notes[older.id].pattern_name, "weak default versus better model")
            current = store.list_context_sections(kinds={"style_guide"})[0]
            self.assertIn("Original human note.", current.content)
            self.assertIn("Strong because it turns outbound volume", current.content)
            store.close()

    def test_voice_coverage_counts_and_recommendations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()

            status, empty = handle_api_request("GET", "/api/voice/coverage", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(empty["topics"], [])
            self.assertEqual(empty["recommendation"], "Add a few Voice Examples to see coverage.")

            store = SQLiteStore(db_path)
            for index in range(3):
                ingest_x_post(
                    store,
                    text=f"Sales AI post {index}",
                    url=f"https://x.com/gavin/status/coverage{index}",
                    date="2026-06-24",
                    author="Gavin",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    worked_reason="Plain, direct contrast.",
                    imitate=True,
                )
            store.close()

            status, coverage = handle_api_request("GET", "/api/voice/coverage", db_path=db_path)
            self.assertEqual(status, 200)
            topic_counts = {item["name"]: item["count"] for item in coverage["topics"]}
            type_counts = {item["name"]: item["count"] for item in coverage["post_types"]}
            self.assertEqual(topic_counts["sales LLMs"], 3)
            self.assertEqual(topic_counts["product"], 0)
            self.assertEqual(type_counts["founder_insight"], 3)
            self.assertEqual(type_counts["product_update"], 0)
            self.assertIn("product update examples", coverage["recommendation"])

    def test_default_positioning_rules_do_not_seed_formulaic_leadpoet_subjects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()

            rules = load_positioning_rules(store)
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight")
            )
            rules_text = "\n".join(rules)

            self.assertTrue(any("Keep the Leadpoet wedge concrete" in rule for rule in rules))
            self.assertIn("buyer timing and proprietary context inside Leadpoet", rules_text)
            self.assertIn("not the mechanism to copy", rules_text)
            self.assertNotIn("Leadpoet owns the intent wedge", rules_text)
            self.assertNotRegex(rules_text, r"\bLeadpoet\s+(?:owns|is|should|can|needs?|has\s+to|starts)\b")
            self.assertNotIn("Leadpoet is the core intent layer", rules_text)
            self.assertIn("Keep the Leadpoet wedge concrete", prompt)
            self.assertNotIn("Leadpoet owns the intent wedge", prompt)
            self.assertNotIn("Leadpoet is the core intent layer", prompt)
            store.close()

    def test_legacy_default_positioning_rules_rewrite_on_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            save_positioning_rules(
                store,
                [
                    "Leadpoet owns the intent wedge: proprietary buyer context, timing signals, and account-level intent.",
                    "Other tools, agents, and case studies are market proof that intent matters, not the source of Leadpoet's unique wedge.",
                    "When referencing another product, validate the market shift, then pull the point back to Leadpoet owning buyer timing and proprietary context.",
                    "Custom rule: Leadpoet owns buyer timing when the user explicitly says it that way.",
                ],
            )

            rules = load_positioning_rules(store)
            rules_text = "\n".join(rules)

            self.assertIn("Keep the Leadpoet wedge concrete", rules_text)
            self.assertIn("buyer timing and proprietary context inside Leadpoet", rules_text)
            self.assertNotIn("Leadpoet owns the intent wedge", rules_text)
            self.assertNotIn("Leadpoet owning buyer timing", rules_text)
            self.assertIn("Custom rule: Leadpoet owns buyer timing", rules_text)
            store.close()

    def test_voice_rules_endpoint_groups_positioning_voice_filters_and_guardrails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            save_avoid_phrases(store, ["game changer", "not just X, but Y"])
            store.close()

            status, payload = handle_api_request("GET", "/api/voice/rules", db_path=db_path)
            self.assertEqual(status, 200)
            groups = {group["title"]: group for group in payload["groups"]}
            self.assertIn("Positioning Rules", groups)
            self.assertIn("Voice Rules", groups)
            self.assertIn("Review Filters", groups)
            self.assertEqual(payload["summary"], "Review filters and writing guardrails")
            self.assertIn("game changer", payload["avoid_phrases"])
            self.assertGreaterEqual(payload["rules_count"], 10)
            self.assertFalse(any("description" in group for group in payload["groups"]))
            self.assertTrue(
                any("Facts & Context" in item for item in groups["Positioning Rules"]["items"])
            )
            self.assertTrue(
                any("workspace's core mechanism" in item for item in groups["Review Filters"]["items"])
            )
            self.assertTrue(
                any("worth posting now" in item for item in groups["Review Filters"]["items"])
            )

            status, saved = handle_api_request(
                "PATCH",
                "/api/voice/rules",
                json.dumps(
                    {
                        "positioning_rules": [
                            "Leadpoet owns buyer timing and proprietary account intent.",
                            "Competitor stories are proof points, not the product wedge.",
                        ],
                        "voice_rules": [
                            "Write like Gavin saw the problem himself.",
                            "Use one concrete mechanism before the conclusion.",
                        ],
                        "review_rules": [
                            "Reject drafts that make another tool sound like Leadpoet.",
                            "Remove smooth filler before saving.",
                        ],
                    }
                ),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            saved_groups = {group["key"]: group["items"] for group in saved["groups"]}
            self.assertTrue(any("Leadpoet owns buyer timing" in item for item in saved_groups["positioning_rules"]))
            self.assertIn("Write like Gavin saw", saved_groups["voice_rules"][0])
            self.assertIn("Reject drafts", saved_groups["review_rules"][0])

            store = SQLiteStore(db_path)
            context = [
                section for section in store.list_context_sections(kinds={"positioning"})
                if section.name == "active_positioning_rules"
            ][0]
            self.assertIn("Leadpoet owns buyer timing", context.content)
            prompt = SocialBrainGenerator(store).build_prompt(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight")
            )
            self.assertIn("Leadpoet owns buyer timing and proprietary account intent.", prompt)
            self.assertIn("Write like Gavin saw the problem himself.", prompt)
            self.assertIn("Reject drafts that make another tool sound like Leadpoet.", prompt)
            self.assertIn("worth posting now", prompt)
            store.close()

    def test_voice_avoid_phrases_can_load_save_and_appear_in_prompts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            self.assertEqual(load_avoid_phrases(store), DEFAULT_AVOID_PHRASES)
            saved_phrases = save_avoid_phrases(store, ["game changer", "unlock", "unlock", " AI is transforming "])
            self.assertEqual(saved_phrases, ["game changer", "unlock", "AI is transforming"])
            store.close()

            status, loaded = handle_api_request("GET", "/api/voice/avoid-phrases", db_path=db_path)
            self.assertEqual(status, 200)
            self.assertEqual(loaded["items"], ["game changer", "unlock", "AI is transforming"])
            self.assertNotIn("future of sales", loaded["defaults"])

            status, posted = handle_api_request(
                "POST",
                "/api/voice/avoid-phrases",
                json.dumps({"phrases": ["game changer", "overly polished thought leadership"]}),
                db_path=db_path,
            )
            self.assertEqual(status, 200)
            self.assertEqual(posted["items"], ["game changer", "overly polished thought leadership"])

            store = SQLiteStore(db_path)
            provider = FakeProvider(["Sales AI should make buyer timing obvious."])
            SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(platform="x", topic="sales LLMs", post_type="founder_insight"),
                variants=1,
            )
            self.assertIn("Avoid Phrases:", provider.prompts[0])
            self.assertIn("- game changer", provider.prompts[0])
            self.assertIn("Do not use these phrases or close variants", provider.prompts[0])
            draft = store.conn.execute("SELECT context_snapshot_json FROM generated_drafts LIMIT 1").fetchone()
            snapshot = json.loads(draft["context_snapshot_json"])
            self.assertEqual(snapshot["avoid_phrases"], ["game changer", "overly polished thought leadership"])
            store.close()

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

            self.assertIn("Voice profile:", prompt)
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
        self.assertEqual(TASK_DEFAULTS["publish-due-x"], {"limit": 20})
        self.assertEqual(
            TASK_DEFAULTS["refresh-trends"],
            {
                "limit": 5,
                "no_enrich": True,
                "scrapingdog_timeout_seconds": 12,
                "max_news_queries_per_window": 2,
                "deadline_seconds": 85,
            },
        )
        self.assertEqual(TASK_DEFAULTS["refresh-patterns"], {"limit": 5, "window": "weekly"})
        self.assertEqual(TASK_DEFAULTS["refresh-patterns-weekly"], {"limit": 5, "windows": ["weekly", "monthly"]})
        self.assertEqual(task_client.DEFAULT_TASK_TIMEOUT_SECONDS, 180)
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
            timeout_seconds=180,
        )
        with patch(
            "leadpoet_social_brain.web.task_client.post_task",
            return_value={"results": [{"error": "X write failed"}]},
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = task_client_main(
                    [
                        "publish-due-x",
                        "--url",
                        "https://example.com",
                        "--secret",
                        "task-secret",
                    ]
                )
        self.assertEqual(exit_code, 1)
        with patch(
            "leadpoet_social_brain.web.task_client.post_task",
            return_value={
                "errors": ["daily Scrapingdog Google News failed: HTTP Error 429 Too Many Requests"],
                "sync": {"status": "partial"},
                "trends_stored": 1,
            },
        ) as post_task:
            with redirect_stdout(io.StringIO()):
                exit_code = task_client_main(
                    [
                        "refresh-trends",
                        "--url",
                        "https://example.com",
                        "--secret",
                        "task-secret",
                        "--payload",
                        '{"limit":3}',
                    ]
                )
        self.assertEqual(exit_code, 0)
        post_task.assert_called_once_with(
            "https://example.com",
            "refresh-trends",
            "task-secret",
            {
                "limit": 3,
                "no_enrich": True,
                "scrapingdog_timeout_seconds": 12,
                "max_news_queries_per_window": 2,
                "deadline_seconds": 85,
            },
            timeout_seconds=180,
        )
        with patch(
            "leadpoet_social_brain.web.task_client.post_task",
            return_value={
                "errors": ["ScrapingDog API key is not configured; trend refresh skipped."],
                "sync": {"status": "partial"},
                "trends_stored": 0,
            },
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = task_client_main(
                    [
                        "refresh-trends",
                        "--url",
                        "https://example.com",
                        "--secret",
                        "task-secret",
                    ]
                )
        self.assertEqual(exit_code, 1)

    def test_railway_entrypoint_routes_cron_services_to_tasks(self):
        from leadpoet_social_brain.web.railway_entrypoint import task_from_environment

        railway_config = Path("railway.toml").read_text(encoding="utf-8")
        self.assertIn("leadpoet_social_brain.web.railway_entrypoint", railway_config)
        self.assertEqual(task_from_environment({"RAILWAY_SERVICE_NAME": "publish-due-x"}), "publish-due-x")
        self.assertEqual(task_from_environment({"RAILWAY_SERVICE_NAME": "sync-x-posts"}), "sync-x-posts")
        self.assertEqual(task_from_environment({"RAILWAY_SERVICE_NAME": "refresh-patterns"}), "refresh-patterns-weekly")
        self.assertEqual(task_from_environment({"SOCIAL_BRAIN_TASK": "sync-x-metrics"}), "sync-x-metrics")
        self.assertIsNone(task_from_environment({"RAILWAY_SERVICE_NAME": "koinoniko"}))

    def test_pattern_refresh_cli_defaults_to_weekly_and_supports_monthly(self):
        parser = build_cli_parser()

        default_args = parser.parse_args(["refresh-patterns"])
        weekly_args = parser.parse_args(["refresh-patterns", "--window", "weekly"])
        monthly_args = parser.parse_args(["refresh-patterns", "--window", "monthly"])

        self.assertEqual(default_args.window, "weekly")
        self.assertEqual(weekly_args.window, "weekly")
        self.assertEqual(monthly_args.window, "monthly")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["refresh-patterns", "--window", "daily"])

    def test_audit_naturalness_x_cli_defaults_to_twenty_unused_trends(self):
        parser = build_cli_parser()

        args = parser.parse_args(["audit-naturalness-x"])

        self.assertEqual(args.count, 20)
        self.assertEqual(args.trend_limit, 0)
        self.assertEqual(args.trend_status, "unused")
        self.assertEqual(args.post_type, "founder_insight")
        self.assertEqual(args.length, "standard")
        self.assertFalse(args.save_drafts)
        self.assertFalse(args.fail_on_warning)
        self.assertTrue(parser.parse_args(["audit-naturalness-x", "--fail-on-warning"]).fail_on_warning)

    def test_weekly_pattern_refresh_task_updates_weekly_and_monthly_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            provider = FakeProvider()
            with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=provider), patch(
                "leadpoet_social_brain.web.app.refresh_patterns",
                return_value=PatternRefreshResult(fetched=1, stored=0, skipped=1),
            ) as refresh:
                status, payload = handle_api_request(
                    "POST",
                    "/tasks/refresh-patterns-weekly",
                    json.dumps({"limit": 7, "window": "daily"}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["fetched"], 2)
            self.assertEqual(payload["windows"]["weekly"]["fetched"], 1)
            self.assertEqual(payload["windows"]["monthly"]["fetched"], 1)
            self.assertEqual(refresh.call_count, 2)
            self.assertEqual([call.kwargs["window"] for call in refresh.call_args_list], ["weekly", "monthly"])
            self.assertTrue(all(call.kwargs["limit"] == 7 for call in refresh.call_args_list))

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

    def test_task_publish_due_x_posts_due_approved_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            due = store.save_content_queue_item(
                ContentQueueItem(
                    text="Due approved post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-27T23:35:00+00:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            future = store.save_content_queue_item(
                ContentQueueItem(
                    text="Future approved post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-28T23:35:00+00:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            store.close()

            fake_client = FakeXWriteClient()
            with patch("leadpoet_social_brain.web.app.XWriteClient.from_env", return_value=fake_client):
                status, payload = handle_api_request(
                    "POST",
                    "/tasks/publish-due-x",
                    json.dumps({"now": "2026-06-27T23:36:00+00:00", "limit": 20}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(len(payload["results"]), 1)
            self.assertEqual(payload["errors"], [])
            self.assertTrue(payload["results"][0]["posted"])
            self.assertEqual(payload["results"][0]["queue_item"]["id"], due.id)
            self.assertEqual(fake_client.posts, ["Due approved post."])

            store = SQLiteStore(db_path)
            self.assertEqual(store.get_content_queue_item(due.id).status, "posted")
            self.assertEqual(store.get_content_queue_item(future.id).status, "approved")
            store.close()

    def test_task_publish_due_x_surfaces_publish_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            due = store.save_content_queue_item(
                ContentQueueItem(
                    text="Due approved post.",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    scheduled_at="2026-06-27T23:35:00+00:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )
            store.close()

            with patch("leadpoet_social_brain.web.app.XWriteClient.from_env", return_value=FailingXWriteClient()):
                status, payload = handle_api_request(
                    "POST",
                    "/tasks/publish-due-x",
                    json.dumps({"now": "2026-06-27T23:36:00+00:00", "limit": 20}),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["errors"], ["X write failed"])
            self.assertEqual(payload["results"][0]["error"], "X write failed")
            self.assertEqual(payload["results"][0]["queue_item"]["id"], due.id)

            store = SQLiteStore(db_path)
            failed = store.get_content_queue_item(due.id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.failure_reason, "X write failed")
            store.close()

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

    def test_post_queue_item_uses_source_draft_length_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            long_text = (
                "Leadpoet sits in the part of sales AI that gets missed when everyone talks about copy.\n\n"
                "The email can be good and the pipeline can still be fake if the account is not actually in a buying window. "
                "That is the difference between topic interest and timing.\n\n"
                "The useful layer is proprietary buyer context, account-level intent, and a clear reason to act this week."
            )
            provider = SequentialProvider(
                [
                    json.dumps(
                        {
                            "thesis": "Buyer timing matters more than copy.",
                            "concrete_detail": "account-level intent",
                            "source_bridge": "Use saved Leadpoet positioning.",
                            "evidence_boundary": "Do not invent proof.",
                        }
                    ),
                    long_text,
                    "Leadpoet is the intent layer.",
                    "Buyer timing beats volume.",
                    json.dumps(
                        {
                            "ranked_drafts": [1, 2, 3],
                            "quality_notes": "Long-form but grounded.",
                            "warnings": [],
                            "suggested_final_version": long_text,
                        }
                    ),
                ]
            )
            draft = SocialBrainGenerator(store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats copy",
                    length_mode="long",
                )
            )[0]
            self.assertGreater(len(draft.text), 280)
            approved = store.save_content_queue_item(
                ContentQueueItem(
                    draft_id=draft.id,
                    text=draft.text,
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="buyer timing beats copy",
                    scheduled_at="2026-06-30T10:00:00-04:00",
                    status="approved",
                    approved_at=utc_now(),
                )
            )

            fake_client = FakeXWriteClient()
            result = post_queue_item_x(store, fake_client, item=approved)

            self.assertIsNone(result.error)
            self.assertEqual(fake_client.posts, [long_text])
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

    def test_generation_uses_pattern_scout_and_published_metrics_analyze_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            store.seed_defaults()
            active = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/pattern-active",
                source_author="Operator",
                source_text="Most AI SDR takes are volume with a better adjective.",
                topic="sales LLMs",
                category="sales",
                pattern_name="Weak default teardown",
                hook_style="names the weak default",
                structure="weak default, concrete counterexample, practical replacement",
                tone="direct",
                why_it_worked="It turns a vague market claim into an obvious product point.",
                adaptation_rule="Use the structure, not the wording.",
                likes=90,
                replies=12,
                window="daily",
            )
            ignored = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/pattern-ignored",
                topic="sales LLMs",
                category="sales",
                pattern_name="Ignored pattern",
                structure="do not use this",
                status="ignored",
                window="daily",
            )

            request = DraftRequest(
                platform="x",
                topic="sales LLMs",
                post_type="founder_insight",
                angle="sales AI should know why the buyer cares now",
                use_patterns=True,
            )
            prompt = SocialBrainGenerator(store).build_prompt(request)
            draft = SocialBrainGenerator(
                store,
                provider=FakeProvider(["Sales AI should not make outbound louder. It should make buyer timing obvious."]),
            ).generate(request, variants=1)[0]
            post = publish_x_draft(
                store,
                draft_id=draft.id,
                url="https://x.com/gavin/status/patternpublished",
                date="2026-06-25",
                author="Gavin",
                metrics=EngagementMetrics(impressions=2500, likes=90, replies=8, reposts=10, bookmarks=24),
            )
            influences = store.list_post_influences([post.id], influence_type="pattern")
            summary = build_analytics_summary(store)

            self.assertIn("Pattern mechanics to adapt:", prompt)
            self.assertIn("Weak default teardown", prompt)
            self.assertIn("Use the structure, not the wording.", prompt)
            self.assertNotIn(ignored.pattern_name, prompt)
            self.assertEqual(draft.context_snapshot["selected_viral_patterns"][0]["id"], active.id)
            self.assertIn(active.id, {influence.ref_id for influence in influences})
            self.assertIn("Weak default teardown", {row.key for row in summary.by_pattern})
            self.assertNotIn("names the weak default", {row.key for row in summary.by_pattern})
            self.assertIn("names the weak default", {row.key for row in summary.by_hook})
            self.assertIn("weak default, concrete counterexample, practical replacement", {row.key for row in summary.by_structure})
            store.close()

    def test_web_create_post_endpoint_accepts_pattern_inspiration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            store.seed_defaults()
            pattern = add_pattern_example(
                store,
                source_url="https://x.com/operator/status/createpattern",
                topic="sales LLMs",
                category="sales",
                pattern_name="Concrete replacement",
                structure="weak default, concrete replacement",
                why_it_worked="It makes the product point less abstract.",
                window="daily",
            )
            store.close()

            with patch(
                "leadpoet_social_brain.web.app.load_provider_from_env",
                return_value=FakeProvider(["Sales AI needs account context before it needs another generated sentence."]),
            ):
                status, payload = handle_api_request(
                    "POST",
                    "/api/content/create-post",
                    json.dumps(
                        {
                            "instruction": "Write about account context beating generated email volume.",
                            "topic": "sales LLMs",
                            "post_type": "founder_insight",
                            "use_patterns": True,
                            "pattern_ids": [pattern.id],
                        }
                    ),
                    db_path=db_path,
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["item"]["status"], "needs_review")
            store = SQLiteStore(db_path)
            saved_draft = store.get_generated_draft(payload["item"]["draft_id"])
            self.assertEqual(saved_draft.context_snapshot["quality_mode"], "quality_review")
            self.assertEqual(saved_draft.context_snapshot["selected_viral_patterns"][0]["id"], pattern.id)
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
