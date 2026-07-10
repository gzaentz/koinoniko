from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    DraftRequest,
    EngagementMetrics,
    GeneratedDraft,
    QueuedPostVersion,
    SocialPost,
)
from leadpoet_social_brain.feedback import accepted_revision_preference_hints
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


class AcceptedRevisionFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "feedback.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def add_revision(
        self,
        instruction: str,
        *,
        accepted: bool = True,
        queue_id: str | None = None,
        before: str = "A queued post with an unnecessary opening about a concrete sales decision.",
        after: str = "A concrete sales decision.",
    ) -> QueuedPostVersion:
        draft = self.store.save_generated_draft(
            GeneratedDraft(
                text=after,
                request=DraftRequest(
                    platform="x",
                    topic="sales",
                    post_type="founder_insight",
                    prompt_profile="natural",
                    revision_source_text=before,
                    revision_instruction=instruction,
                    revision_source_queue_id=queue_id,
                ),
                explanation="feedback fixture",
                influences=[],
            )
        )
        item = self.store.get_content_queue_item(queue_id) if queue_id else None
        item = self.store.save_content_queue_item(
            ContentQueueItem(
                **(
                    {
                        **item.__dict__,
                        "text": after,
                        "draft_id": draft.id,
                    }
                    if item
                    else {
                        "text": after,
                        "topic": "sales",
                        "post_type": "founder_insight",
                        "scheduled_at": "",
                        "draft_id": draft.id,
                        **({"id": queue_id} if queue_id else {}),
                    }
                )
            )
        )
        return self.store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=item.id,
                text=after,
                edit_type="llm",
                edit_instruction=instruction,
                source_draft_id=draft.id,
                accepted_at="2026-07-10T12:00:00+00:00" if accepted else None,
            )
        )

    def test_preference_requires_three_distinct_accepted_queue_decisions(self) -> None:
        self.add_revision("Make it shorter and tighter.")
        self.add_revision("Please shorten this and cut the extra setup.")

        self.assertEqual(accepted_revision_preference_hints(self.store), [])

        self.add_revision("Use fewer words; make the point concise.")
        hints = accepted_revision_preference_hints(self.store)

        self.assertEqual(len(hints), 1)
        self.assertIn("3 independent posts", hints[0])
        self.assertIn("Prefer tighter copy", hints[0])

    def test_unaccepted_and_repeated_same_queue_revisions_do_not_create_a_rule(self) -> None:
        first = self.add_revision("Make this more specific.", accepted=True)
        self.add_revision("Name the mechanism and make it more specific.", accepted=True, queue_id=first.queue_id)
        self.add_revision("Use a concrete detail.", accepted=False)
        self.add_revision("Make the claim more specific.", accepted=True)

        self.assertEqual(accepted_revision_preference_hints(self.store), [])

    def test_raw_instruction_never_enters_bundle_and_fixed_hint_has_precedence(self) -> None:
        secret = "SECRET-DO-NOT-ECHO"
        for _ in range(3):
            self.add_revision(f"Make it more natural and sound human. {secret}")

        bundle = ContextManager(self.store).build_bundle(
            platform="x",
            topic="sales",
            post_type="founder_insight",
        )

        self.assertTrue(bundle.analytics_hints)
        self.assertTrue(bundle.analytics_hints[0].startswith("Accepted edit signal"))
        self.assertNotIn(secret, "\n".join(bundle.analytics_hints))

    def test_unmeasured_posts_do_not_create_a_performance_hint(self) -> None:
        measured = self.store.save_post(
            SocialPost(
                text="A measured post.",
                platform="x",
                author="Gavin",
                topic="sales operations",
                post_type="founder_insight",
                status="published",
            )
        )
        self.store.add_metrics(
            measured.id,
            EngagementMetrics(impressions=1_000, likes=100),
        )
        self.store.save_post(
            SocialPost(
                text="An unmeasured post must not double the sample size.",
                platform="x",
                author="Gavin",
                topic="sales operations",
                post_type="founder_insight",
                status="published",
            )
        )

        summary = build_analytics_summary(self.store)

        self.assertEqual(summary.by_topic[0].count, 1)
        self.assertFalse(any(hint.startswith("Early signal") for hint in summary.hints))

    def test_topic_format_hint_comes_from_an_observed_joint_cohort(self) -> None:
        for index in range(2):
            post = self.store.save_post(
                SocialPost(
                    text=f"Forecast evidence post {index}.",
                    platform="x",
                    author="Gavin",
                    topic="forecast evidence",
                    post_type="technical_explanation",
                    status="published",
                )
            )
            self.store.add_metrics(
                post.id,
                EngagementMetrics(impressions=1_000, likes=80),
            )

        summary = build_analytics_summary(self.store)

        self.assertIn(
            "posts about forecast evidence in a technical explanation format",
            summary.hints[0],
        )

    def test_tiny_sample_raw_rate_does_not_outrank_credible_performance(self) -> None:
        for topic, impressions, likes in [
            ("tiny sample", 1, 1),
            ("tiny sample", 1, 1),
            ("credible sample", 10_000, 400),
            ("credible sample", 10_000, 400),
        ]:
            post = self.store.save_post(
                SocialPost(
                    text=f"A post in the {topic} cohort.",
                    platform="x",
                    author="Gavin",
                    topic=topic,
                    post_type="founder_insight",
                    status="published",
                )
            )
            self.store.add_metrics(
                post.id,
                EngagementMetrics(impressions=impressions, likes=likes),
            )

        summary = build_analytics_summary(self.store)

        self.assertEqual(summary.by_topic[0].key, "credible sample")

    def test_negated_edit_instruction_does_not_create_opposite_preference(self) -> None:
        for _ in range(3):
            self.add_revision("Do not make it shorter or tighter.")

        self.assertEqual(accepted_revision_preference_hints(self.store), [])


if __name__ == "__main__":
    unittest.main()
