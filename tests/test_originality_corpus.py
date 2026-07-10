from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    DraftRequest,
    GeneratedDraft,
    PostNote,
    QueuedPostVersion,
    SocialPost,
    ViralPattern,
)
from leadpoet_social_brain.generation import SocialBrainGenerator
from leadpoet_social_brain.originality import (
    COPIED_PASSAGE_VIOLATION,
    EXACT_COPY_VIOLATION,
    NEAR_COPY_VIOLATION,
    build_originality_corpus,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


def _evaluation_json(final_text: str) -> str:
    return json.dumps(
        {
            "ranked_drafts": [1, 2, 3],
            "quality_notes": "The evaluator selected one complete grounded option.",
            "warnings": [],
            "logic_audit": {
                "premise": final_text,
                "conclusion": final_text,
                "bridge_status": "supported",
            },
            "claim_audit": [],
            "quality_audit": {
                "reader_payoff": {
                    "passed": True,
                    "type": "decision_rule",
                    "quote": final_text,
                },
                "generic_swap": {
                    "passed": True,
                    "genericized_text": final_text,
                    "specific_mechanism": final_text,
                },
                "originality": {
                    "passed": True,
                    "nearest_source": None,
                    "reason": "The adversarial evaluator missed the historical match.",
                },
            },
            "suggested_final_version": final_text,
        }
    )


def _exact_final_audit_json(prompt: str) -> str:
    text_match = re.search(
        r"<(?P<tag>exact_final_post_[a-f0-9]{24})>\n(.*?)\n</(?P=tag)>",
        prompt,
        flags=re.S,
    )
    hash_match = re.search(r"Exact UTF-8 SHA-256: ([a-f0-9]{64})", prompt)
    final_text = text_match.group(2) if text_match else ""
    digest = hash_match.group(1) if hash_match else ""
    return json.dumps(
        {
            "contract_version": "natural_exact_final_v1",
            "audited_text_sha256": digest,
            "passed": True,
            "quality_notes": "The exact final is grounded, useful, and distinct.",
            "warnings": [],
            "logic_audit": {
                "premise": final_text,
                "conclusion": final_text,
                "bridge_status": "supported",
            },
            "claim_audit": [],
            "quality_audit": {
                "reader_payoff": {
                    "passed": True,
                    "type": "decision_rule",
                    "quote": final_text,
                },
                "generic_swap": {
                    "passed": True,
                    "genericized_text": final_text,
                    "specific_mechanism": final_text,
                },
                "originality": {
                    "passed": True,
                    "nearest_source": None,
                    "reason": "The exact final differs from the historical source.",
                },
            },
        }
    )


class HistoricalCopyProvider:
    def __init__(self, *, writer_options: list[str], evaluator_final: str, repair_text: str | None = None):
        self.writer_options = writer_options
        self.evaluator_final = evaluator_final
        self.repair_text = repair_text or evaluator_final

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        if "Write 3 X draft options for this workspace" in prompt:
            return self.writer_options[:variants]
        if "Choose the best X post for this workspace" in prompt:
            return [_evaluation_json(self.evaluator_final)]
        if "Audit this exact final X post" in prompt:
            return [_exact_final_audit_json(prompt)]
        return [self.repair_text]


class OriginalityCorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "brain.sqlite3")
        self.request = SimpleNamespace(platform="x")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def save_draft(
        self,
        text: str,
        *,
        platform: str = "x",
        created_at: str = "2026-07-01T12:00:00+00:00",
    ) -> GeneratedDraft:
        return self.store.save_generated_draft(
            GeneratedDraft(
                text=text,
                request=DraftRequest(
                    platform=platform,
                    topic="sales LLMs",
                    post_type="founder_insight",
                    prompt_profile="natural",
                ),
                explanation="fixture",
                influences=[],
                created_at=created_at,
            )
        )

    def save_queue(
        self,
        text: str,
        *,
        draft_id: str | None = None,
        status: str = "needs_review",
        scheduled_at: str = "2026-07-02T12:00:00+00:00",
    ) -> ContentQueueItem:
        return self.store.save_content_queue_item(
            ContentQueueItem(
                text=text,
                topic="sales LLMs",
                post_type="founder_insight",
                scheduled_at=scheduled_at,
                draft_id=draft_id,
                status=status,
            )
        )

    def save_post(self, text: str, *, imitate: bool = False) -> SocialPost:
        post = self.store.save_post(
            SocialPost(
                text=text,
                platform="x",
                author="Gavin",
                topic="sales LLMs",
                post_type="founder_insight",
                status="published",
                published_at="2026-07-03T12:00:00+00:00",
            )
        )
        if imitate:
            self.store.save_note(PostNote(post_id=post.id, imitate=True))
        return post

    def test_collects_each_source_family_and_voice_overlay(self):
        post = self.save_post(
            "A published diagnostic asks which account event changed the next action.",
            imitate=True,
        )
        draft = self.save_draft(
            "A generated-only draft explains why account priority needs evidence."
        )
        queue = self.save_queue(
            "The active queue keeps a distinct buyer-timing decision rule.",
            draft_id=draft.id,
        )
        self.store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=queue.id,
                text="The prior queue version used a different outbound mechanism.",
                edit_type="llm",
            )
        )
        pattern = ViralPattern(
            source_text="An external pattern source uses its own memorable opening and payoff.",
            source_summary="unused because source text is available",
        )

        corpus = build_originality_corpus(
            self.store,
            self.request,
            patterns=[pattern],
            extra_texts=["The current batch already used a compact forecast question."],
        )

        self.assertEqual(corpus.source_count, 6)
        self.assertEqual(corpus.raw_source_count, 6)
        self.assertEqual(
            corpus.source_kind_counts,
            {
                "batch": 1,
                "generated_draft": 1,
                "pattern": 1,
                "post": 1,
                "queue": 1,
                "queue:needs_review": 1,
                "queue_version": 1,
                "queue_version:llm": 1,
                "voice_example": 1,
            },
        )
        self.assertEqual(corpus.check(post.text).matches[0].source_kind, "post")

    def test_canonical_text_dedupes_lineage_and_uses_published_precedence(self):
        common = (
            "A signal should change the account decision before outreach. "
            "https://example.com/source"
        )
        post = self.save_post(common, imitate=True)
        draft = self.save_draft(
            "The signal should change the account decision before outreach!"
        )
        queue = self.save_queue(
            "The signal should change the account decision before outreach. @gavin",
            draft_id=draft.id,
        )
        version = self.store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=queue.id,
                text="A signal should change the account decision before outreach?",
                edit_type="manual",
            )
        )
        pattern = ViralPattern(
            source_text="The signal should change the account decision before outreach."
        )

        corpus = build_originality_corpus(
            self.store,
            self.request,
            patterns=[pattern],
            extra_texts=["A signal should change the account decision before outreach."],
        )
        result = corpus.check("The signal should change the account decision before outreach.")

        self.assertEqual(corpus.raw_source_count, 6)
        self.assertEqual(corpus.source_count, 1)
        self.assertEqual(result.violations, [EXACT_COPY_VIOLATION])
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.match_type, "exact")
        self.assertEqual(match.source_kind, "post")
        self.assertEqual(match.source_id, post.id)
        self.assertEqual(
            set(match.source_kinds),
            {
                "batch",
                "generated_draft",
                "pattern",
                "post",
                "queue",
                "queue:needs_review",
                "queue_version",
                "queue_version:manual",
                "voice_example",
            },
        )
        self.assertEqual(
            set(match.source_ids),
            {post.id, draft.id, queue.id, version.id, pattern.id, "batch:0"},
        )

    def test_revision_excludes_only_its_queue_versions_and_linked_draft(self):
        source_text = "The account-change check should determine the next seller action."
        linked_draft = self.save_draft(source_text)
        revision_queue = self.save_queue(source_text, draft_id=linked_draft.id)
        self.store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=revision_queue.id,
                text="An earlier version of the same queue lineage.",
                edit_type="manual",
            )
        )
        revision_request = SimpleNamespace(
            platform="x",
            revision_source_queue_id=revision_queue.id,
        )

        corpus = build_originality_corpus(self.store, revision_request)

        self.assertTrue(corpus.check(source_text).passed)
        self.assertTrue(corpus.check("An earlier version of the same queue lineage.").passed)

        other_queue = self.save_queue(
            source_text,
            scheduled_at="2026-07-04T12:00:00+00:00",
        )
        corpus = build_originality_corpus(self.store, revision_request)
        result = corpus.check(source_text)

        self.assertFalse(result.passed)
        self.assertEqual(result.matches[0].source_kind, "queue")
        self.assertEqual(result.matches[0].source_id, other_queue.id)

    def test_all_queue_states_are_included(self):
        statuses = ["needs_review", "approved", "posting", "failed", "posted"]
        for index, status in enumerate(statuses):
            self.save_queue(
                f"Queue state {status} carries unique source wording number {index}.",
                status=status,
                scheduled_at=f"2026-07-{index + 1:02d}T12:00:00+00:00",
            )

        corpus = build_originality_corpus(self.store, self.request)

        self.assertEqual(corpus.source_kind_counts["queue"], len(statuses))
        for status in statuses:
            self.assertEqual(corpus.source_kind_counts[f"queue:{status}"], 1)

    def test_near_copy_threshold_catches_number_and_punctuation_changes(self):
        self.save_post(
            "One reason I prefer smaller high quality lead lists is they let you test "
            "which angles work before burning through thousands of leads with bad messaging."
        )
        candidate = (
            "One reason I prefer smaller, high-quality lead lists: they let you test "
            "which angles work before burning through 1,000s of leads with bad messaging."
        )

        result = build_originality_corpus(self.store, self.request).check(candidate)

        self.assertEqual(result.violations, [NEAR_COPY_VIOLATION])
        self.assertEqual(result.matches[0].match_type, "near_copy")
        self.assertGreaterEqual(result.matches[0].shingle_containment, 0.70)
        self.assertGreaterEqual(result.matches[0].shingle_jaccard, 0.45)

    def test_copied_middle_passage_is_detected_independently_of_whole_post(self):
        copied_passage = (
            "Signal evidence should connect confirmed account change with seller action "
            "before outreach begins."
        )
        self.save_post(copied_passage)
        candidate = (
            "Forecast dashboards can reward motion without explaining decisions. Teams add "
            "fields, charts, colors, scoring bands, and alerts while the operating question "
            "stays unanswered.\n\n"
            f"{copied_passage}\n\n"
            "Otherwise the dashboard records activity while ownership, priority, follow-up, "
            "and judgment stay exactly where they started."
        )

        result = build_originality_corpus(self.store, self.request).check(candidate)

        self.assertEqual(result.violations, [COPIED_PASSAGE_VIOLATION])
        self.assertEqual(result.matches[0].match_type, "copied_passage")
        self.assertGreaterEqual(result.matches[0].longest_content_span, 8)

    def test_selected_pattern_summary_is_used_when_source_text_is_missing(self):
        summary = "A specific external source phrase should not become the evaluator's final post."
        pattern = ViralPattern(source_text="  \n", source_summary=summary)

        result = build_originality_corpus(
            self.store,
            self.request,
            patterns=[pattern],
        ).check(summary)

        self.assertEqual(result.matches[0].source_kind, "pattern")
        self.assertEqual(result.violations, [EXACT_COPY_VIOLATION])

    def test_distinct_sales_mechanisms_with_shared_domain_words_pass(self):
        self.save_post(
            "CRM accuracy tells a rep whether the contact record is usable. It does not "
            "establish buyer intent or a reason to act this week."
        )
        candidate = (
            "A confirmed security review can change account priority. Route that signal to "
            "the seller who owns the next procurement decision."
        )

        result = build_originality_corpus(self.store, self.request).check(candidate)

        self.assertTrue(result.passed)
        self.assertEqual(result.matches, ())
        self.assertEqual(result.violations, [])

    def test_receipts_and_repr_do_not_serialize_source_text(self):
        secret_source = "A uniquely identifying private source sentence about account evidence."
        self.save_post(secret_source)
        corpus = build_originality_corpus(self.store, self.request)
        result = corpus.check(secret_source)

        serialized = json.dumps(result.to_dict())
        self.assertNotIn(secret_source, serialized)
        self.assertNotIn(secret_source, repr(corpus))
        self.assertNotIn(secret_source, repr(result))
        self.assertEqual(result.violations, [EXACT_COPY_VIOLATION])
        self.assertEqual(result.to_dict()["diagnostics"]["match_count"], 1)

    def test_production_gate_rechecks_evaluator_rewrite_against_unselected_history(self):
        copied = "A forecast changes only when the evidence behind deal probability changes."
        copied_post = self.save_post(copied)
        # Explicit examples occupy every Voice slot, proving the hard corpus is
        # broader than the four examples exposed to the model.
        for index in range(4):
            self.save_post(
                f"Voice example {index} asks which observable account event changes the seller's next action.",
                imitate=True,
            )
        clean_options = [
            "Before a forecast moves, ask which new buyer event changed the deal evidence.",
            "A pipeline review needs one rule: change probability only when new evidence changes the deal.",
            "If no observable evidence changed, the forecast number should not move.",
        ]
        provider = HistoricalCopyProvider(
            writer_options=clean_options,
            evaluator_final=copied,
        )

        draft = SocialBrainGenerator(self.store, provider=provider).generate(
            DraftRequest(
                platform="x",
                topic="sales LLMs",
                post_type="founder_insight",
                angle="Give sales leaders a rule for changing forecast probability.",
                prompt_profile="natural",
            ),
            save=True,
        )[0]

        self.assertEqual(draft.text, clean_options[0])
        gate = draft.context_snapshot["evaluation"]["natural_quality_gate"]
        self.assertEqual(gate["resolution"], "clean_writer_alternative")
        self.assertFalse(gate["initial_originality"]["passed"])
        self.assertEqual(
            gate["initial_originality"]["diagnostics"]["matches"][0]["source_id"],
            copied_post.id,
        )
        self.assertTrue(gate["originality"]["passed"])
        self.assertNotIn(copied, json.dumps(gate))

    def test_production_gate_rejects_saved_generated_only_copy(self):
        copied = "A useful account signal changes the decision a seller makes before outreach."
        self.save_draft(copied)
        provider = HistoricalCopyProvider(
            writer_options=[
                copied,
                "One observation deserves direct language. " + copied,
                "The point can be stated without extra framing. " + copied,
            ],
            evaluator_final=copied,
            repair_text=copied,
        )

        with self.assertRaisesRegex(ValueError, "original"):
            SocialBrainGenerator(self.store, provider=provider).generate(
                DraftRequest(
                    platform="x",
                    topic="sales LLMs",
                    post_type="founder_insight",
                    angle="Explain why account signals should change a seller decision.",
                    prompt_profile="natural",
                ),
                save=True,
            )

        self.assertEqual(len(self.store.list_generated_drafts(platform="x", limit=None)), 1)

    def test_list_generated_drafts_is_scoped_ordered_and_bounded(self):
        older = self.save_draft(
            "Older X draft.",
            created_at="2026-07-01T12:00:00+00:00",
        )
        newer = self.save_draft(
            "Newer X draft.",
            created_at="2026-07-02T12:00:00+00:00",
        )
        self.save_draft(
            "A LinkedIn draft should not enter the X corpus.",
            platform="linkedin",
            created_at="2026-07-03T12:00:00+00:00",
        )

        self.assertEqual(
            [draft.id for draft in self.store.list_generated_drafts(platform="x", limit=1)],
            [newer.id],
        )
        self.assertEqual(
            [draft.id for draft in self.store.list_generated_drafts(platform="x", limit=None)],
            [newer.id, older.id],
        )
        self.assertEqual(self.store.list_generated_drafts(platform="x", limit=0), [])


if __name__ == "__main__":
    unittest.main()
