from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from leadpoet_social_brain.core.models import (
    ContextSection,
    DraftRequest,
    PostNote,
    SocialPost,
    TrendItem,
)
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    auditable_final_claim_clauses,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


VOICE_EXAMPLE = "Sending got cheaper. Knowing why this account cares did not."

SAVED_FACTS = (
    "Leadpoet combines proprietary buyer context, timing signals, and account-level intent "
    "so a rep can reason about account priority. A confirmed security review can change "
    "account priority. CRM record accuracy does not establish buyer intent or a reason to "
    "act this week."
)

SAVED_GUARDRAILS = (
    "Do not claim that Leadpoet autonomously monitors every CRM account, predicts buyer "
    "intent, routes accounts, sends messages, increases replies, or creates revenue outcomes."
)


def distinct_adversarial_options(final_text: str, variants: int) -> list[str]:
    options = [
        final_text,
        "One observation deserves a direct statement without extra framing. " + final_text,
        "The point can be stated plainly without a polished category recap. " + final_text,
    ]
    return options[:variants]


@dataclass(frozen=True)
class CorpusCase:
    name: str
    final_text: str
    angle: str
    trend_fixture: str | None = None
    writer_options: tuple[str, str, str] | None = None
    expects_voice_context: bool = False


class AdversarialNaturalProvider:
    """A schema-valid evaluator that insists every candidate is publishable.

    The provider also returns the same bad text to every repair prompt. Rejection
    therefore has to come from production validation, not evaluator cooperation or
    a lucky clean alternative.
    """

    def __init__(self, case: CorpusCase):
        self.case = case
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Write 3 X draft options for this workspace" in prompt:
            options = list(
                self.case.writer_options
                or distinct_adversarial_options(self.case.final_text, 3)
            )
            while len(options) < variants:
                options.append(options[-1])
            return options[:variants]
        if "Choose the best X post for this workspace" in prompt:
            return [self._evaluation_json()]
        if "Audit this exact final X post" in prompt:
            return [self._exact_audit_json(prompt)]

        # This deliberately covers grounding, coherence, length, and final quality
        # repair prompts. A bad corpus row must remain bad after the bounded repair.
        return [self.case.final_text]

    def _exact_audit_json(self, prompt: str) -> str:
        digest_match = re.search(r"Exact UTF-8 SHA-256: ([a-f0-9]{64})", prompt)
        if self.case.trend_fixture == "causal":
            support = "Acme expanded because demand rose."
        elif self.case.trend_fixture == "funding" and "$60" in self.case.final_text:
            support = "Acme raised $60 million in Series B funding."
        else:
            support = self.case.angle
        claim_audit = [
            {"claim": clause, "support": support}
            for clause in auditable_final_claim_clauses(self.case.final_text)
        ]
        return json.dumps(
            {
                "contract_version": "natural_exact_final_v1",
                "audited_text_sha256": digest_match.group(1) if digest_match else "",
                "passed": True,
                "quality_notes": "The fixture audited the exact final text.",
                "warnings": [],
                "logic_audit": {
                    "premise": self.case.final_text,
                    "conclusion": self.case.final_text,
                    "bridge_status": "supported",
                },
                "claim_audit": claim_audit,
                "quality_audit": {
                    "reader_payoff": {
                        "passed": True,
                        "type": "operating_lesson",
                        "quote": self.case.final_text,
                    },
                    "generic_swap": {
                        "passed": True,
                        "genericized_text": self.case.final_text.replace(
                            "Leadpoet", "the product"
                        ),
                        "specific_mechanism": self.case.final_text,
                    },
                    "originality": {
                        "passed": True,
                        "nearest_source": None,
                        "reason": "The fixture treats the exact final as original.",
                    },
                },
            }
        )

    def _evaluation_json(self) -> str:
        return json.dumps(
            {
                "ranked_drafts": [1, 2, 3],
                "quality_notes": "Specific, grounded, original, and ready to publish.",
                "warnings": [],
                "logic_audit": {
                    "premise": self.case.final_text,
                    "conclusion": self.case.final_text,
                    "bridge_status": "supported",
                },
                # Empty is intentionally valid under the production contract. The
                # final gate must not treat evaluator omission as proof of grounding.
                "claim_audit": [],
                "quality_audit": {
                    "reader_payoff": {
                        "passed": True,
                        "type": "operating_lesson",
                        "quote": self.case.final_text,
                    },
                    "generic_swap": {
                        "passed": True,
                        "genericized_text": self.case.final_text.replace("Leadpoet", "the product"),
                        "specific_mechanism": self.case.final_text,
                    },
                    "originality": {
                        "passed": True,
                        "nearest_source": None,
                        "reason": "The adversarial evaluator claims the output is original.",
                    },
                },
                "suggested_final_version": self.case.final_text,
            }
        )


class NaturalQualityCorpusIntegrationTests(unittest.TestCase):
    def test_evaluator_contract_fails_closed_when_structured_quality_audit_is_missing(self):
        final_text = "A forecast update is useful when it names the evidence that changed deal probability."

        class MissingQualityAuditProvider:
            def __init__(self):
                self.prompts: list[str] = []

            def generate(self, prompt: str, *, variants: int) -> list[str]:
                self.prompts.append(prompt)
                if "Write 3 X draft options for this workspace" in prompt:
                    return distinct_adversarial_options(final_text, variants)
                return [
                    json.dumps(
                        {
                            "ranked_drafts": [1, 2, 3],
                            "quality_notes": "Looks useful.",
                            "warnings": [],
                            "logic_audit": {
                                "premise": final_text,
                                "conclusion": final_text,
                                "bridge_status": "supported",
                            },
                            "claim_audit": [],
                            "suggested_final_version": final_text,
                        }
                    )
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            provider = MissingQualityAuditProvider()
            try:
                with self.assertRaisesRegex(ValueError, "trustworthy review after one retry"):
                    SocialBrainGenerator(store, provider=provider).generate(
                        DraftRequest(
                            platform="x",
                            topic="sales quality corpus",
                            post_type="founder_insight",
                            angle=final_text,
                            prompt_profile="natural",
                        ),
                        save=True,
                    )
                self.assertEqual(len(provider.prompts), 3)
                self.assertEqual(self._saved_draft_count(store), 0)
            finally:
                store.close()

    def test_evaluator_quality_audit_failures_cannot_be_queued_as_clean_copy(self):
        final_text = "A forecast review needs one test: name the evidence that changed deal probability."

        class FailedQualityAuditProvider(AdversarialNaturalProvider):
            def _evaluation_json(self) -> str:
                payload = json.loads(super()._evaluation_json())
                payload["quality_audit"]["reader_payoff"] = {
                    "passed": False,
                    "type": "none",
                    "quote": "",
                }
                return json.dumps(payload)

        case = CorpusCase(
            name="structured_quality_audit_failure",
            angle="Give sales leaders a forecast diagnostic.",
            final_text=final_text,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            provider = FailedQualityAuditProvider(case)
            try:
                with self.assertRaisesRegex(ValueError, "unresolved warning"):
                    SocialBrainGenerator(store, provider=provider).generate(
                        self._request_for_case(store, case),
                        save=True,
                    )
                self.assertEqual(self._saved_draft_count(store), 0)
            finally:
                store.close()

    def test_rejects_company_swap_copy_generic_ai_takes_and_empty_sales_slogans(self):
        cases = [
            CorpusCase(
                name="company_swap_benefits",
                angle="Explain one specific Leadpoet mechanism for account prioritization.",
                final_text=(
                    "Leadpoet empowers sales teams to work smarter, move faster, and build "
                    "stronger customer relationships."
                ),
            ),
            CorpusCase(
                name="generic_ai_and_human_judgment",
                angle="Give sales leaders a concrete decision rule for evaluating sales AI.",
                final_text=(
                    "The companies that win with AI will be the ones that combine technology "
                    "with human judgment."
                ),
            ),
            CorpusCase(
                name="empty_sales_slogan",
                angle="Give sales leaders one useful operating lesson.",
                final_text="The best sales teams don't chase change. They create it.",
            ),
        ]

        for case in cases:
            with self.subTest(case=case.name):
                self._assert_rejected(case)

    def test_rejects_an_invented_attributed_quote(self):
        self._assert_rejected(
            CorpusCase(
                name="invented_cro_quote",
                angle="Explain why CRM accuracy is not the same as buyer intent.",
                final_text=(
                    "A CRO told me, “Clean CRM data is the same thing as intent.” They were wrong."
                ),
            )
        )

    def test_rejects_unsupported_product_capability_despite_passing_evaluator(self):
        self._assert_rejected(
            CorpusCase(
                name="unsupported_product_capability",
                angle="Explain why account timing matters without inventing product capabilities.",
                final_text=(
                    "Leadpoet monitors every account, predicts buyer intent, and sends reps "
                    "the exact message at the exact moment."
                ),
            )
        )

    def test_rejects_negated_first_person_authorization(self):
        self._assert_rejected(
            CorpusCase(
                name="negated_first_person_authorization",
                angle="Do not claim we tested this; no test or customer study exists.",
                final_text="We tested this with sales teams and found context beats volume.",
            )
        )

    def test_rejects_exact_and_near_voice_example_copying(self):
        cases = [
            CorpusCase(
                name="exact_voice_copy",
                angle="Explain why an account-specific reason still matters.",
                final_text=VOICE_EXAMPLE,
                expects_voice_context=True,
            ),
            CorpusCase(
                name="near_voice_copy_from_evaluator_rewrite",
                angle="Explain why an account-specific reason still matters.",
                final_text="Sending is cheap now. Knowing why an account cares is not.",
                # The evaluator creates a distinct rewrite after three independently
                # bad writer options. This covers final validation after selection,
                # including the evaluator-rewrite path, without violating the
                # three-option diversity contract first.
                writer_options=(
                    VOICE_EXAMPLE,
                    "Sales is changing. The teams that adapt will win.",
                    "Leadpoet predicts buyer intent and sends the exact message.",
                ),
                expects_voice_context=True,
            ),
        ]

        for case in cases:
            with self.subTest(case=case.name):
                self._assert_rejected(case)

    def test_rejects_numeric_meaning_and_causal_token_laundering(self):
        cases = [
            CorpusCase(
                name="numeric_meaning_laundering",
                angle="Use the Acme funding source without inventing operating results.",
                trend_fixture="funding",
                final_text="Acme added $60 million in annual recurring revenue.",
            ),
            CorpusCase(
                name="causal_token_laundering",
                angle="State only the cause established by the Acme source.",
                trend_fixture="causal",
                final_text="Acme expanded because its AI agent cut the sales cycle.",
            ),
        ]

        for case in cases:
            with self.subTest(case=case.name):
                self._assert_rejected(case)

    def test_approves_grounded_product_and_first_person_controls(self):
        cases = [
            CorpusCase(
                name="grounded_product_mechanism",
                angle=(
                    "A confirmed security review can change account priority. Leadpoet "
                    "combines timing signals with buyer context so a rep can reason about priority."
                ),
                final_text=(
                    "A confirmed security review can change account priority. Leadpoet combines "
                    "that timing signal with buyer context before a rep chooses the next account."
                ),
            ),
            CorpusCase(
                name="affirmative_first_person_evidence",
                angle=(
                    "We tested one operating rule: a confirmed security review should change "
                    "account priority. The test supported that rule."
                ),
                final_text=(
                    "We tested one rule: a confirmed security review should change account priority."
                ),
            ),
        ]

        for case in cases:
            with self.subTest(case=case.name):
                self._assert_approved(case)

    def test_approves_original_copy_in_the_saved_voice(self):
        self._assert_approved(
            CorpusCase(
                name="voice_aligned_but_original",
                angle=(
                    "CRM record accuracy does not establish buyer intent or a reason to act this week."
                ),
                final_text=(
                    "CRM accuracy answers whether a record is usable. It does not tell a rep "
                    "whether the buyer has a reason to act this week."
                ),
                expects_voice_context=True,
            )
        )

    def test_approves_numeric_and_causal_claims_with_matching_meaning(self):
        cases = [
            CorpusCase(
                name="supported_funding_amount",
                angle="State what the Acme funding source establishes and no more.",
                trend_fixture="funding",
                final_text=(
                    "Acme raised $60 million. That establishes new capital, not buying intent."
                ),
            ),
            CorpusCase(
                name="supported_cause",
                angle="Use only the cause stated in the Acme expansion source.",
                trend_fixture="causal",
                final_text=(
                    "Acme expanded because demand rose. If you use that as a sales trigger, "
                    "keep the claim that narrow."
                ),
            ),
        ]

        for case in cases:
            with self.subTest(case=case.name):
                self._assert_approved(case)

    def test_approves_a_user_supplied_causal_premise_alongside_a_trend(self):
        self._assert_approved(
            CorpusCase(
                name="user_grounded_causal_premise",
                angle=(
                    "Public funding announcements are visible to every competitor, so outreach "
                    "needs an account-specific reason."
                ),
                trend_fixture="funding",
                final_text=(
                    "A funding announcement is public to every competitor. Because the trigger "
                    "is shared, outreach needs an account-specific reason."
                ),
            )
        )

    def _assert_rejected(self, case: CorpusCase) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            provider = AdversarialNaturalProvider(case)
            request = self._request_for_case(store, case)
            try:
                with self.assertRaises(ValueError):
                    SocialBrainGenerator(store, provider=provider).generate(
                        request,
                        variants=1,
                        save=True,
                    )
                self.assertEqual(self._saved_draft_count(store), 0)
                self._assert_full_natural_pipeline_ran(provider)
                self._assert_voice_fixture_was_visible(case, provider)
            finally:
                store.close()

    def _assert_approved(self, case: CorpusCase) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            provider = AdversarialNaturalProvider(case)
            request = self._request_for_case(store, case)
            try:
                drafts = SocialBrainGenerator(store, provider=provider).generate(
                    request,
                    variants=1,
                    save=True,
                )
                self.assertEqual([draft.text for draft in drafts], [case.final_text])
                self.assertEqual(self._saved_draft_count(store), 1)
                self.assertTrue(
                    drafts[0].context_snapshot["evaluation"]["natural_quality_gate"]["passed"]
                )
                self._assert_full_natural_pipeline_ran(provider)
                self._assert_voice_fixture_was_visible(case, provider)
            finally:
                store.close()

    def _make_store(self, tmpdir: str) -> SQLiteStore:
        store = SQLiteStore(Path(tmpdir) / "natural-quality-corpus.sqlite3")
        store.seed_defaults()
        store.save_context_section(
            ContextSection(
                kind="product",
                name="natural_quality_corpus_facts",
                content=SAVED_FACTS,
                weight=5.0,
            )
        )
        store.save_context_section(
            ContextSection(
                kind="guardrail",
                name="natural_quality_corpus_guardrails",
                content=SAVED_GUARDRAILS,
                weight=5.0,
            )
        )
        example = store.save_post(
            SocialPost(
                text=VOICE_EXAMPLE,
                platform="x",
                author="Gavin",
                topic="sales quality corpus",
                post_type="founder_insight",
                status="example",
            )
        )
        store.save_note(
            PostNote(
                post_id=example.id,
                worked_reason="Plain cadence; use the style without copying the wording or thesis.",
                imitate=True,
            )
        )
        return store

    def _request_for_case(self, store: SQLiteStore, case: CorpusCase) -> DraftRequest:
        trend_ids: list[str] = []
        if case.trend_fixture:
            trend_ids.append(self._save_trend_fixture(store, case.trend_fixture).id)
        return DraftRequest(
            platform="x",
            topic="sales quality corpus",
            post_type="founder_insight",
            angle=case.angle,
            use_trends=bool(trend_ids),
            trend_ids=trend_ids,
            prompt_profile="natural",
        )

    def _save_trend_fixture(self, store: SQLiteStore, fixture: str) -> TrendItem:
        if fixture == "funding":
            return store.save_trend_item(
                TrendItem(
                    title="Acme raises $60 million",
                    source_type="news",
                    topic="sales quality corpus",
                    summary="Acme announced $60 million in new Series B funding.",
                    why_it_matters="The company has new capital.",
                    leadpoet_angle="Separate public funding from account-specific buying evidence.",
                    source_excerpt="Acme raised $60 million in Series B funding.",
                )
            )
        if fixture == "causal":
            return store.save_trend_item(
                TrendItem(
                    title="Acme expands after demand rises",
                    source_type="news",
                    topic="sales quality corpus",
                    summary="Acme expanded because demand rose.",
                    why_it_matters="The source attributes the expansion to increased demand.",
                    leadpoet_angle="Keep the causal claim inside the stated evidence.",
                    source_excerpt="Acme expanded because demand rose.",
                )
            )
        raise AssertionError(f"Unknown trend fixture: {fixture}")

    def _assert_voice_fixture_was_visible(
        self,
        case: CorpusCase,
        provider: AdversarialNaturalProvider,
    ) -> None:
        if case.expects_voice_context:
            self.assertTrue(
                any(VOICE_EXAMPLE in prompt for prompt in provider.prompts),
                "Voice-copy corpus case did not exercise a prompt containing the saved example.",
            )

    def _assert_full_natural_pipeline_ran(
        self,
        provider: AdversarialNaturalProvider,
    ) -> None:
        self.assertTrue(
            any("Write 3 X draft options for this workspace" in prompt for prompt in provider.prompts)
        )
        self.assertTrue(
            any("Choose the best X post for this workspace" in prompt for prompt in provider.prompts)
        )

    @staticmethod
    def _saved_draft_count(store: SQLiteStore) -> int:
        row = store.conn.execute("SELECT COUNT(*) AS count FROM generated_drafts").fetchone()
        return int(row["count"])


if __name__ == "__main__":
    unittest.main()
