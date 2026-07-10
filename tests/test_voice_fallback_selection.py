from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from leadpoet_social_brain.assistant_context import build_assistant_context
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import EngagementMetrics, PostNote, SocialPost
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.web.app import voice_suggestions_payload


class VoiceFallbackSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "brain.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def add_post(
        self,
        *,
        text: str,
        topic: str = "sales LLMs",
        post_type: str = "founder_insight",
        impressions: int | None = 1_000,
        engagements: int = 30,
        measured_at: str = "2026-07-01T12:00:00+00:00",
        published_at: str = "2026-07-01T12:00:00+00:00",
        structure: str | None = None,
        imitate: bool = False,
        source_draft_id: str | None = None,
    ) -> SocialPost:
        post = self.store.save_post(
            SocialPost(
                text=text,
                platform="x",
                author="Gavin" if source_draft_id is None else "Koinoniko",
                topic=topic,
                post_type=post_type,
                status="published",
                source_draft_id=source_draft_id,
                published_at=published_at,
            )
        )
        if impressions is not None:
            self.store.add_metrics(
                post.id,
                EngagementMetrics(
                    impressions=impressions,
                    likes=engagements,
                    measured_at=measured_at,
                ),
            )
        if structure or imitate:
            self.store.save_note(
                PostNote(
                    post_id=post.id,
                    structure=structure,
                    imitate=imitate,
                )
            )
        return post

    def select(self, *, max_examples: int = 4, topic: str = "sales LLMs") -> list[SocialPost]:
        return ContextManager(self.store).build_bundle(
            platform="x",
            topic=topic,
            post_type="founder_insight",
            max_examples=max_examples,
        ).examples

    def test_explicit_voice_examples_keep_precedence_over_observed_performance(self):
        explicit = self.add_post(
            text="Buyer timing is the part of sales AI worth teaching the model.",
            impressions=5_000,
            engagements=25,
            imitate=True,
        )
        self.add_post(
            text="A strong observed result should not overrule a Voice Example the writer chose.",
            impressions=5_000,
            engagements=500,
        )

        selected = self.select(max_examples=1)

        self.assertEqual([post.id for post in selected], [explicit.id])

    def test_observed_fallback_excludes_generated_posts_and_dedupes_exact_text(self):
        explicit = self.add_post(
            text="Sending got cheaper. Knowing why this account cares did not.",
            imitate=True,
        )
        self.add_post(
            text="  SENDING got cheaper.\nKnowing why this account cares did not.  ",
            impressions=10_000,
            engagements=1_000,
        )
        alternate = self.add_post(
            text="The useful sales model explains account priority before it writes a sentence.",
            impressions=3_000,
            engagements=120,
        )
        self.add_post(
            text="Generated copy must not become evidence for the next generated post.",
            impressions=10_000,
            engagements=5_000,
            source_draft_id="draft-generated-loop",
        )

        selected = self.select(max_examples=2)

        self.assertEqual([post.id for post in selected], [explicit.id, alternate.id])
        self.assertTrue(all(post.source_draft_id is None for post in selected))

    def test_confidence_adjustment_beats_a_tiny_sample_raw_engagement_rate(self):
        self.add_post(
            text="One impression and one like is not enough evidence to calibrate a writing voice.",
            impressions=1,
            engagements=1,
        )
        credible = self.add_post(
            text="A repeated result across real reach is better evidence for a durable writing voice.",
            impressions=10_000,
            engagements=400,
        )

        selected = self.select(max_examples=1)

        self.assertEqual([post.id for post in selected], [credible.id])

    def test_candidate_read_model_uses_the_deterministic_latest_metrics(self):
        changed = self.add_post(
            text="A post can look strong early and settle once the full audience has seen it.",
            impressions=100,
            engagements=100,
            measured_at="2026-07-01T12:00:00+00:00",
        )
        self.store.add_metrics(
            changed.id,
            EngagementMetrics(
                impressions=10_000,
                likes=100,
                measured_at="2026-07-02T12:00:00+00:00",
            ),
        )
        credible = self.add_post(
            text="Use the complete measurement window before calling a post a Voice signal.",
            impressions=10_000,
            engagements=300,
            measured_at="2026-07-02T12:00:00+00:00",
        )

        candidates = {item.post.id: item for item in self.store.list_authored_voice_candidates(platform="x")}
        selected = self.select(max_examples=1)

        self.assertEqual(candidates[changed.id].metrics.measured_at, "2026-07-02T12:00:00+00:00")
        self.assertEqual(candidates[changed.id].metrics.engagement_rate, 0.01)
        self.assertEqual([post.id for post in selected], [credible.id])

    def test_fallback_prefers_a_self_contained_post_when_evidence_is_equal(self):
        self.add_post(
            text="Exactly. This is the whole sales AI problem that everyone keeps missing.",
            impressions=4_000,
            engagements=120,
            published_at="2026-07-02T12:00:00+00:00",
        )
        standalone = self.add_post(
            text="Sales AI is useful when it can explain why an account deserves attention today.",
            impressions=4_000,
            engagements=120,
            published_at="2026-07-01T12:00:00+00:00",
        )

        selected = self.select(max_examples=1)

        self.assertEqual([post.id for post in selected], [standalone.id])

    def test_fallback_does_not_let_a_link_promo_become_the_primary_voice_reference(self):
        self.add_post(
            text=(
                "Claude DESTROYS every other lead tool. I put the prompt vault below so you can "
                "book more meetings today. https://example.com/vault"
            ),
            impressions=25_000,
            engagements=1_250,
            published_at="2026-07-02T12:00:00+00:00",
        )
        standalone = self.add_post(
            text=(
                "A timing signal is useful when it changes which account a rep opens first. "
                "Everything else is another alert."
            ),
            impressions=5_000,
            engagements=150,
            published_at="2026-07-01T12:00:00+00:00",
        )

        selected = self.select(max_examples=1)

        self.assertEqual([post.id for post in selected], [standalone.id])

    def test_fallback_diversifies_thesis_and_structure_inside_the_same_relevance_tier(self):
        best = self.add_post(
            text=(
                "Writing the email is downstream of judging which account has a reason to listen. "
                "The hard part is buyer context."
            ),
            impressions=5_000,
            engagements=200,
            structure="contrast",
            published_at="2026-07-03T12:00:00+00:00",
        )
        repeated = self.add_post(
            text=(
                "Copy is the easy part. Knowing which buyer account has the right signal is the hard part."
            ),
            impressions=5_000,
            engagements=200,
            structure="contrast",
            published_at="2026-07-02T12:00:00+00:00",
        )
        diverse = self.add_post(
            text=(
                "A clean CRM record is not the same as verified buying intent. "
                "Check the evidence before outreach."
            ),
            impressions=5_000,
            engagements=200,
            structure="diagnostic",
            published_at="2026-07-01T12:00:00+00:00",
        )

        selected = self.select(max_examples=2)

        self.assertEqual([post.id for post in selected], [best.id, diverse.id])
        self.assertNotIn(repeated.id, {post.id for post in selected})

    def test_relevance_is_not_traded_away_for_global_performance(self):
        exact = self.add_post(
            text="Sales LLMs need account evidence before they decide what to write.",
            impressions=5_000,
            engagements=100,
        )
        self.add_post(
            text="A globally strong founder post can still be the wrong Voice reference for this request.",
            topic="fundraising",
            impressions=5_000,
            engagements=500,
        )

        selected = self.select(max_examples=1)

        self.assertEqual([post.id for post in selected], [exact.id])

    def test_generated_copy_is_not_automatically_suggested_as_voice(self):
        authored = self.add_post(
            text="An authored sales lesson should remain eligible for deliberate Voice selection.",
            impressions=5_000,
            engagements=100,
        )
        generated = self.add_post(
            text="A generated post with strong metrics must not nominate itself as future Voice.",
            impressions=5_000,
            engagements=1_000,
            source_draft_id="generated-draft-id",
        )

        suggestion_ids = {row["id"] for row in voice_suggestions_payload(self.store, limit=10)}
        assistant_ids = {
            row["id"]
            for row in build_assistant_context(
                self.store,
                "What are the best posts to use as Voice Examples?",
            )["voice"]["candidate_examples"]
        }

        self.assertIn(authored.id, suggestion_ids)
        self.assertIn(authored.id, assistant_ids)
        self.assertNotIn(generated.id, suggestion_ids)
        self.assertNotIn(generated.id, assistant_ids)


if __name__ == "__main__":
    unittest.main()
