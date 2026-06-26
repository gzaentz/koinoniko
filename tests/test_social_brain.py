from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.content_queue import plan_week_x, post_queue_item_x
from leadpoet_social_brain.cli import main as cli_main
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContentQueueItem, DraftRequest, EngagementMetrics, TrendItem, utc_now
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
from leadpoet_social_brain.platforms.x_api import (
    MISSING_X_WRITE_CREDENTIALS_MESSAGE,
    XApiClient,
    XPostedTweet,
    XWriteClient,
    normalize_x_api_post,
    sync_x_metrics,
    sync_x_user_posts,
)
from leadpoet_social_brain.skills import generation_skills, load_skill
from leadpoet_social_brain.storage.sqlite_store import DB_ENV_VAR, SQLiteStore, resolve_db_path
from leadpoet_social_brain.web.app import (
    DEFAULT_AUTH_USERNAME,
    SESSION_COOKIE,
    auth_is_configured,
    create_session_cookie_value,
    handle_api_request,
    request_is_authenticated,
    validate_session_cookie_value,
    verify_credentials,
)


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


class FakeXApiClient:
    def __init__(self, tweets, metrics_by_id=None):
        self.tweets = tweets
        self.metrics_by_id = metrics_by_id or {}
        self.resolved_usernames: list[str] = []

    def resolve_user_id(self, username: str) -> str:
        self.resolved_usernames.append(username)
        return "42"

    def iter_user_posts(self, user_id: str, **kwargs):
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

    def test_sqlite_store_resolves_env_db_and_sets_pragmas(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_db = Path(tmpdir) / "volume" / "social_brain.sqlite3"
            explicit_db = Path(tmpdir) / "explicit" / "brain.sqlite3"
            with patch.dict(os.environ, {DB_ENV_VAR: str(env_db)}, clear=False):
                self.assertEqual(resolve_db_path(), env_db)
                self.assertEqual(resolve_db_path(explicit_db), explicit_db)

                store = SQLiteStore()
                self.assertEqual(store.path, env_db)
                self.assertTrue(env_db.parent.exists())
                self.assertEqual(store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
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

            self.assertIn("Observed Gavin/Leadpoet Writing Guide", section.content)
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
