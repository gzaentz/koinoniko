from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from leadpoet_social_brain.core.models import ContextBundle, DraftRequest
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    exact_text_sha256,
    natural_exact_final_audit_contract_violations,
    writer_option_diversity_violations,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


def quality_audit(final_text: str) -> dict[str, object]:
    return {
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
            "reason": "The fixture considers this wording distinct.",
        },
    }


def evaluation_json(final_text: str) -> str:
    return json.dumps(
        {
            "ranked_drafts": [1, 2, 3],
            "quality_notes": "The evaluator selected a useful grounded option.",
            "warnings": [],
            "logic_audit": {
                "premise": final_text,
                "conclusion": final_text,
                "bridge_status": "supported",
            },
            "claim_audit": [],
            "quality_audit": quality_audit(final_text),
            "suggested_final_version": final_text,
        }
    )


def exact_final_from_prompt(prompt: str) -> tuple[str, str]:
    text_match = re.search(
        r"<(?P<tag>exact_final_post_[a-f0-9]{24})>\n(.*?)\n</(?P=tag)>",
        prompt,
        flags=re.S,
    )
    hash_match = re.search(r"Exact UTF-8 SHA-256: ([a-f0-9]{64})", prompt)
    return (
        text_match.group(2) if text_match else "",
        hash_match.group(1) if hash_match else "",
    )


def exact_audit_json(
    prompt: str,
    *,
    hash_override: str | None = None,
    quote_override: str | None = None,
    bridge_status: str = "supported",
    warnings: list[str] | None = None,
    passed: bool = True,
) -> str:
    final_text, digest = exact_final_from_prompt(prompt)
    quote = quote_override if quote_override is not None else final_text
    return json.dumps(
        {
            "contract_version": "natural_exact_final_v1",
            "audited_text_sha256": hash_override if hash_override is not None else digest,
            "passed": passed,
            "quality_notes": "The exact final was audited without rewriting it.",
            "warnings": warnings or [],
            "logic_audit": {
                "premise": quote,
                "conclusion": quote,
                "bridge_status": bridge_status,
            },
            "claim_audit": [],
            "quality_audit": quality_audit(quote),
        }
    )


class ExactFinalProvider:
    def __init__(self, *, exact_mode: str = "valid") -> None:
        self.bad = "Here's the thing: the future of sales is unlocked at scale."
        self.clean = (
            "A timing signal is useful only when it changes which account a rep opens first."
        )
        self.options = [
            self.bad,
            self.clean,
            "A forecast change should name the evidence that changed deal probability.",
        ]
        self.exact_mode = exact_mode
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Write 3 X draft options for this workspace" in prompt:
            return self.options[:variants]
        if "Choose the best X post for this workspace" in prompt:
            return [evaluation_json(self.bad)]
        if "Audit this exact final X post" in prompt:
            if self.exact_mode == "stale_hash":
                stale = hashlib.sha256(self.bad.encode("utf-8")).hexdigest()
                return [exact_audit_json(prompt, hash_override=stale, quote_override=self.bad)]
            if self.exact_mode == "missing_bridge":
                return [
                    exact_audit_json(
                        prompt,
                        bridge_status="missing",
                        warnings=["unsupported_logical_bridge"],
                        passed=False,
                    )
                ]
            return [exact_audit_json(prompt)]
        return [self.bad]


class WriterSetProvider:
    def __init__(
        self,
        first: list[str],
        retry: list[str],
    ) -> None:
        self.first = first
        self.retry = retry
        self.writer_calls = 0
        self.evaluator_calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Write 3 X draft options for this workspace" in prompt:
            self.writer_calls += 1
            values = self.first if self.writer_calls == 1 else self.retry
            return values[:variants]
        if "Choose the best X post for this workspace" in prompt:
            self.evaluator_calls += 1
            final_text = (self.retry if self.writer_calls > 1 else self.first)[0]
            return [evaluation_json(final_text)]
        raise AssertionError(f"Unexpected prompt: {prompt[:120]}")


class RankedSuggestedMismatchProvider:
    def __init__(self) -> None:
        self.options = [
            "A forecast should move only when the evidence behind it changes.",
            "Before outreach, ask which observable event changed account priority.",
            "A CRM field matters only when it changes a seller decision.",
        ]
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Write 3 X draft options for this workspace" in prompt:
            return self.options[:variants]
        if "Choose the best X post for this workspace" in prompt:
            # Deliberately audit Draft 2 while ranking Draft 1 first. The local
            # selector may choose Draft 1, but it must not reuse Draft 2's audit.
            return [evaluation_json(self.options[1])]
        if "Audit this exact final X post" in prompt:
            return [exact_audit_json(prompt)]
        raise AssertionError(f"Unexpected prompt: {prompt[:120]}")


class ExactFinalAndWriterDiversityTests(unittest.TestCase):
    def request(self, *, angle: str = "Give sales leaders one useful operating rule.") -> DraftRequest:
        return DraftRequest(
            platform="x",
            topic="sales",
            post_type="founder_insight",
            angle=angle,
            prompt_profile="natural",
        )

    def make_store(self, tmpdir: str) -> SQLiteStore:
        store = SQLiteStore(Path(tmpdir) / "exact-final.sqlite3")
        store.seed_defaults()
        return store

    def empty_bundle(self) -> ContextBundle:
        return ContextBundle(
            platform="x",
            topic="sales",
            post_type="founder_insight",
            style_sections=[],
            positioning_sections=[],
            patterns=[],
            examples=[],
            notes_by_post_id={},
            metrics_by_post_id={},
            analytics_hints=[],
        )

    def test_quality_gate_replacement_gets_sha_bound_exact_final_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = ExactFinalProvider()
            try:
                draft = SocialBrainGenerator(store, provider=provider).generate(
                    self.request(),
                    save=True,
                )[0]

                self.assertEqual(draft.text, provider.clean)
                evaluation = draft.context_snapshot["evaluation"]
                receipt = evaluation["exact_final_audit"]
                self.assertFalse(receipt["reused_initial_audit"])
                self.assertEqual(receipt["source"], "exact_final_typed_evaluator")
                self.assertEqual(
                    receipt["audited_text_sha256"],
                    hashlib.sha256(draft.text.encode("utf-8")).hexdigest(),
                )
                self.assertEqual(evaluation["suggested_final_version"], draft.text)
                self.assertEqual(evaluation["quality_audit"]["reader_payoff"]["quote"], draft.text)
                self.assertEqual(len([p for p in provider.prompts if "Audit this exact final" in p]), 1)
            finally:
                store.close()

    def test_ranked_selection_different_from_suggested_forces_exact_final_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = RankedSuggestedMismatchProvider()
            try:
                draft = SocialBrainGenerator(store, provider=provider).generate(
                    self.request(),
                    save=True,
                )[0]

                self.assertEqual(draft.text, provider.options[0])
                evaluation = draft.context_snapshot["evaluation"]
                receipt = evaluation["exact_final_audit"]
                self.assertFalse(receipt["reused_initial_audit"])
                self.assertEqual(receipt["source"], "exact_final_typed_evaluator")
                self.assertIn(
                    "initial evaluator audit hash does not match the exact final text",
                    receipt["initial_reuse_violations"],
                )
                self.assertEqual(
                    evaluation["quality_audit"]["reader_payoff"]["quote"],
                    draft.text,
                )
                self.assertEqual(
                    len([p for p in provider.prompts if "Audit this exact final" in p]),
                    1,
                )
            finally:
                store.close()

    def test_stale_hash_or_stale_quote_fails_closed_after_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = ExactFinalProvider(exact_mode="stale_hash")
            try:
                with self.assertRaisesRegex(ValueError, "Exact-final evaluator"):
                    SocialBrainGenerator(store, provider=provider).generate(
                        self.request(),
                        save=True,
                    )
                self.assertEqual(store.list_generated_drafts(limit=None), [])
                self.assertEqual(len([p for p in provider.prompts if "Audit this exact final" in p]), 2)
            finally:
                store.close()

    def test_missing_bridge_in_exact_final_audit_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = ExactFinalProvider(exact_mode="missing_bridge")
            try:
                with self.assertRaisesRegex(ValueError, "Exact-final evaluator"):
                    SocialBrainGenerator(store, provider=provider).generate(
                        self.request(),
                        save=True,
                    )
                self.assertEqual(store.list_generated_drafts(limit=None), [])
            finally:
                store.close()

    def test_exact_audit_rejects_nested_or_conflicting_rows(self):
        final_text = "A forecast should move only when its evidence changes."
        base = {
            "contract_version": "natural_exact_final_v1",
            "audited_text_sha256": exact_text_sha256(final_text),
            "passed": True,
            "quality_notes": "The exact final was audited.",
            "warnings": [],
            "logic_audit": {
                "premise": final_text,
                "conclusion": final_text,
                "bridge_status": "supported",
            },
            "claim_audit": [],
            "quality_audit": quality_audit(final_text),
        }
        malformed = []

        nested_logic = json.loads(json.dumps(base))
        nested_logic["logic_audit"]["second_row"] = {
            "premise": final_text,
            "conclusion": final_text,
            "bridge_status": "missing",
        }
        malformed.append(nested_logic)

        nested_claim = json.loads(json.dumps(base))
        nested_claim["claim_audit"] = {
            "claim": final_text,
            "support": final_text,
            "second_row": {"claim": final_text, "support": "unsupported"},
        }
        malformed.append(nested_claim)

        conflicting_status = json.loads(json.dumps(base))
        conflicting_status["logic_audit"]["status"] = "missing"
        malformed.append(conflicting_status)

        for payload in malformed:
            with self.subTest(payload=payload):
                violations = natural_exact_final_audit_contract_violations(
                    json.dumps(payload),
                    final_text=final_text,
                    request=self.request(angle=final_text),
                    bundle=self.empty_bundle(),
                    trends=[],
                )
                self.assertTrue(violations)

    def test_identical_writer_set_retries_once_then_uses_distinct_set(self):
        duplicate = "A forecast review should name the evidence behind any probability change."
        retry = [
            duplicate,
            "Before outreach, ask which observable event changed account priority.",
            "A CRM field earns its place only when it changes a seller decision.",
        ]
        provider = WriterSetProvider([duplicate] * 3, retry)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            try:
                draft = SocialBrainGenerator(store, provider=provider).generate(
                    self.request(),
                    save=True,
                )[0]
                self.assertEqual(draft.text, retry[0])
                self.assertEqual(provider.writer_calls, 2)
                self.assertEqual(provider.evaluator_calls, 1)
                receipt = draft.context_snapshot["evaluation"]["writer_option_diversity"]
                self.assertEqual(receipt["attempts"], 2)
                self.assertTrue(receipt["initial_violations"])
            finally:
                store.close()

    def test_two_collapsed_writer_sets_fail_before_evaluation_or_save(self):
        duplicate = "A forecast review should name the evidence behind any probability change."
        provider = WriterSetProvider([duplicate] * 3, [duplicate] * 3)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            try:
                with self.assertRaisesRegex(ValueError, "three trustworthy, distinct options"):
                    SocialBrainGenerator(store, provider=provider).generate(
                        self.request(),
                        save=True,
                    )
                self.assertEqual(provider.writer_calls, 2)
                self.assertEqual(provider.evaluator_calls, 0)
                self.assertEqual(store.list_generated_drafts(limit=None), [])
            finally:
                store.close()

    def test_synonym_thesis_collapse_retries_unless_direct_brief_requires_it(self):
        collapsed = [
            "Buyer readiness beats firmographic fit.",
            "Firmographics matter less than readiness to buy.",
            "In-market timing is more important than ICP fit.",
        ]
        retry = [
            "A forecast review should name the evidence behind any probability change.",
            "Before outreach, ask which observable event changed account priority.",
            "A CRM field earns its place only when it changes a seller decision.",
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = WriterSetProvider(collapsed, retry)
            try:
                SocialBrainGenerator(store, provider=provider).generate(
                    self.request(angle="Discuss account prioritization."),
                    save=False,
                )
                self.assertEqual(provider.writer_calls, 2)
            finally:
                store.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = WriterSetProvider(collapsed, retry)
            try:
                SocialBrainGenerator(store, provider=provider).generate(
                    self.request(angle="Argue that buyer readiness beats firmographic fit."),
                    save=False,
                )
                self.assertEqual(provider.writer_calls, 2)
            finally:
                store.close()

    def test_pairwise_synonym_thesis_collapse_is_rejected(self):
        options = [
            "Buyer readiness beats firmographic fit.",
            "Firmographics matter less than readiness to buy.",
            "Mass outreach damages sender reputation.",
        ]

        violations = writer_option_diversity_violations(options, self.request())

        self.assertTrue(any("unrequested semantic thesis" in value for value in violations))

    def test_requested_thesis_can_repeat_only_with_developed_payoffs(self):
        options = [
            (
                "Buyer readiness beats firmographic fit because a recent signal changes "
                "which account a rep should work next."
            ),
            (
                "Firmographics matter less than readiness to buy. Use readiness to decide "
                "where research time goes before a sequence starts."
            ),
            (
                "In-market timing is more important than ICP fit. The practical payoff is "
                "a forecast tied to observable buyer movement."
            ),
        ]

        violations = writer_option_diversity_violations(
            options,
            self.request(angle="Argue that buyer readiness beats firmographic fit."),
        )

        self.assertFalse(any("thesis" in value for value in violations))


if __name__ == "__main__":
    unittest.main()
