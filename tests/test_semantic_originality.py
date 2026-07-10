from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    DraftRequest,
    GeneratedDraft,
    SocialPost,
)
from leadpoet_social_brain.originality import (
    SEMANTIC_THESIS_VIOLATION,
    build_originality_corpus,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


ATOM = "buyer-readiness-outperforms-firmographic-fit"
FIRST = "Buyer readiness beats firmographic fit."
SECOND = "Firmographics matter less than readiness to buy."
CANDIDATE = "In-market timing is more important than ICP fit."


class SemanticOriginalityCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "semantic-originality.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def request(self, *, angle: str = "Discuss sales prioritization.", **values) -> DraftRequest:
        return DraftRequest(
            platform="x",
            topic="sales prioritization",
            post_type="founder_insight",
            angle=angle,
            prompt_profile="natural",
            **values,
        )

    def save_post(
        self,
        text: str,
        *,
        source_draft_id: str | None = None,
        published_at: str | None = None,
    ) -> SocialPost:
        return self.store.save_post(
            SocialPost(
                text=text,
                platform="x",
                author="Gavin",
                topic="sales",
                post_type="founder_insight",
                status="example",
                source_draft_id=source_draft_id,
                published_at=published_at,
            )
        )

    def test_two_independent_history_lineages_block_synonym_only_thesis_copy(self):
        self.save_post(FIRST)
        self.save_post(SECOND)

        check = build_originality_corpus(self.store, self.request()).check(CANDIDATE)

        self.assertEqual(check.matches, ())
        self.assertEqual(check.semantic_candidate_atoms, (ATOM,))
        self.assertEqual(check.semantic_prior_lineage_counts, {ATOM: 2})
        self.assertEqual(check.violations, [SEMANTIC_THESIS_VIOLATION])
        self.assertEqual(check.semantic_matches[0].history_lineage_count, 2)
        self.assertEqual(check.semantic_matches[0].batch_lineage_count, 0)

    def test_one_history_lineage_is_recorded_as_pressure_but_not_hard_rejected(self):
        self.save_post(FIRST)

        check = build_originality_corpus(self.store, self.request()).check(CANDIDATE)

        self.assertTrue(check.passed)
        self.assertEqual(check.semantic_prior_lineage_counts, {ATOM: 1})
        self.assertEqual(check.semantic_matches, ())

    def test_one_same_batch_lineage_blocks_a_second_use(self):
        corpus = build_originality_corpus(
            self.store,
            self.request(),
            extra_texts=[FIRST],
        )

        check = corpus.check(CANDIDATE)

        self.assertEqual(check.violations, [SEMANTIC_THESIS_VIOLATION])
        self.assertEqual(check.semantic_matches[0].history_lineage_count, 0)
        self.assertEqual(check.semantic_matches[0].batch_lineage_count, 1)

    def test_post_and_queue_with_one_source_draft_count_as_one_lineage(self):
        draft_id = "shared-draft"
        self.save_post(FIRST, source_draft_id=draft_id)
        self.store.save_content_queue_item(
            ContentQueueItem(
                text=SECOND,
                topic="sales",
                post_type="founder_insight",
                scheduled_at="2026-07-11T12:00:00+00:00",
                draft_id=draft_id,
                status="needs_review",
            )
        )

        check = build_originality_corpus(self.store, self.request()).check(CANDIDATE)

        self.assertTrue(check.passed)
        self.assertEqual(check.semantic_prior_lineage_counts, {ATOM: 1})

    def test_generated_only_and_rejected_copy_do_not_poison_semantic_history(self):
        for text in (FIRST, SECOND):
            self.store.save_generated_draft(
                GeneratedDraft(
                    text=text,
                    request=self.request(),
                    explanation="fixture",
                    influences=[],
                )
            )
            self.store.save_content_queue_item(
                ContentQueueItem(
                    text=text,
                    topic="sales",
                    post_type="founder_insight",
                    scheduled_at="",
                    status="rejected",
                )
            )

        check = build_originality_corpus(self.store, self.request()).check(CANDIDATE)

        self.assertTrue(check.passed)
        self.assertEqual(check.semantic_prior_lineage_counts, {ATOM: 0})

    def test_only_a_full_direct_user_relation_exempts_saturation(self):
        self.save_post(FIRST)
        self.save_post(SECOND)

        vague = build_originality_corpus(
            self.store,
            self.request(angle="Discuss timing in account prioritization."),
        ).check(CANDIDATE)
        explicit = build_originality_corpus(
            self.store,
            self.request(angle="Argue that buyer readiness beats firmographic fit."),
        ).check(CANDIDATE)

        self.assertEqual(vague.violations, [SEMANTIC_THESIS_VIOLATION])
        self.assertTrue(explicit.passed)
        self.assertEqual(explicit.semantic_exempt_atoms, (ATOM,))

    def test_style_revision_keeps_thesis_but_new_angle_revision_does_not(self):
        self.save_post(FIRST)
        self.save_post(SECOND)
        style_request = self.request(
            revision_source_text=CANDIDATE,
            revision_instruction="Make it shorter and clearer.",
        )
        new_angle_request = self.request(
            revision_source_text=CANDIDATE,
            revision_instruction="Replace the thesis with a different angle.",
        )

        style_check = build_originality_corpus(self.store, style_request).check(CANDIDATE)
        new_angle_check = build_originality_corpus(self.store, new_angle_request).check(CANDIDATE)

        self.assertTrue(style_check.passed)
        self.assertEqual(style_check.semantic_exempt_atoms, (ATOM,))
        self.assertEqual(new_angle_check.violations, [SEMANTIC_THESIS_VIOLATION])

    def test_revision_exemption_fails_closed_for_semantic_edits(self):
        self.save_post(FIRST)
        self.save_post(SECOND)
        semantic_edits = (
            "Remove this thesis and make it tighter.",
            "Trim it, but argue the opposite.",
            "Make it concise and replace the core claim.",
        )

        for instruction in semantic_edits:
            with self.subTest(instruction=instruction):
                check = build_originality_corpus(
                    self.store,
                    self.request(
                        revision_source_text=CANDIDATE,
                        revision_instruction=instruction,
                    ),
                ).check(CANDIDATE)
                self.assertEqual(check.semantic_exempt_atoms, ())
                self.assertEqual(check.violations, [SEMANTIC_THESIS_VIOLATION])

        preserving = build_originality_corpus(
            self.store,
            self.request(
                revision_source_text=CANDIDATE,
                revision_instruction="Shorten it without changing the thesis.",
            ),
        ).check(CANDIDATE)
        self.assertTrue(preserving.passed)
        self.assertEqual(preserving.semantic_exempt_atoms, (ATOM,))

    def test_saturated_atom_survives_more_than_thirty_newer_semantic_lineages(self):
        self.save_post(FIRST, published_at="2026-01-01T00:00:00+00:00")
        self.save_post(SECOND, published_at="2026-01-02T00:00:00+00:00")
        for index in range(30):
            self.save_post(
                f"Mass outreach damages sender reputation. Observation {index}.",
                published_at=f"2026-02-{(index % 28) + 1:02d}T{index % 24:02d}:00:00+00:00",
            )

        check = build_originality_corpus(self.store, self.request()).check(CANDIDATE)

        self.assertEqual(check.semantic_prior_lineage_counts[ATOM], 2)
        self.assertEqual(check.violations, [SEMANTIC_THESIS_VIOLATION])

    def test_bounded_queue_corpus_uses_newest_items_not_oldest_schedule(self):
        for scheduled_at, text in (
            ("2026-01-01T00:00:00+00:00", "An unrelated coaching note."),
            ("2026-01-02T00:00:00+00:00", "A separate pipeline review note."),
            ("2026-12-31T00:00:00+00:00", FIRST),
        ):
            self.store.save_content_queue_item(
                ContentQueueItem(
                    text=text,
                    topic="sales",
                    post_type="founder_insight",
                    scheduled_at=scheduled_at,
                    status="approved",
                )
            )

        check = build_originality_corpus(
            self.store,
            self.request(),
            limit_per_kind=2,
        ).check(CANDIDATE)

        self.assertEqual(check.semantic_prior_lineage_counts, {ATOM: 1})

    def test_receipts_prompt_and_repr_never_contain_historical_prose(self):
        self.save_post(FIRST)
        self.save_post(SECOND)
        corpus = build_originality_corpus(self.store, self.request())
        check = corpus.check(CANDIDATE)
        serialized = json.dumps(
            {
                "summary": corpus.summary(),
                "check": check.to_dict(),
                "prompt": corpus.semantic_prompt_section(),
                "repr": repr(corpus),
            },
            sort_keys=True,
        ).casefold()

        self.assertNotIn(FIRST.casefold(), serialized)
        self.assertNotIn(SECOND.casefold(), serialized)
        self.assertNotIn("firmographics matter less", serialized)
        self.assertIn(ATOM, serialized)


if __name__ == "__main__":
    unittest.main()
