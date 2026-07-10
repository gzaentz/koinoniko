from __future__ import annotations

import json
import unittest

from leadpoet_social_brain.core.models import ContextBundle, ContextSection, DraftRequest
from leadpoet_social_brain.generation import (
    auditable_final_claim_clauses,
    exact_text_sha256,
    natural_exact_final_audit_contract_violations,
    natural_grounding_violations,
)


class ResidualGroundingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = DraftRequest(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            angle="Use only supplied evidence.",
            angle_is_factual_evidence=False,
            extra_context_is_factual_evidence=False,
            prompt_profile="natural",
        )

    def test_product_capabilities_bind_every_object_and_named_anchor(self):
        cases = [
            (
                "Leadpoet identifies job changes.",
                "Leadpoet identifies job changes and buyer browsing activity.",
            ),
            (
                "Leadpoet is a sales intelligence product.",
                "With Leadpoet, reps get buyer browsing activity and private inbox data.",
            ),
            (
                "Leadpoet is a sales intelligence product.",
                "Buyer-browsing intelligence from Leadpoet changes account priority.",
            ),
            (
                "Leadpoet identifies job changes.",
                "Acme guarantees meetings.",
            ),
        ]
        for evidence, claim in cases:
            with self.subTest(claim=claim):
                self.assert_has_violation(evidence, claim, "product capability")

    def test_product_capability_controls_preserve_paraphrase_and_advice(self):
        controls = [
            ("Leadpoet identifies job changes.", "Leadpoet surfaces career moves."),
            (
                "Fictional Acme now records confirmed security reviews as CRM events.",
                "Fictional Acme now tracks security reviews as CRM events.",
            ),
            (
                "Leadpoet is a sales intelligence product.",
                "With Leadpoet, reps should not monitor every buyer.",
            ),
            (
                "A confirmed security review can change account priority. "
                "Leadpoet combines timing signals with buyer context so a rep can reason about priority.",
                "A confirmed security review can change account priority. Leadpoet combines that "
                "timing signal with buyer context before a rep chooses the next account.",
            ),
            ("Acme guarantees meetings.", "Acme guarantees meetings."),
        ]
        for evidence, claim in controls:
            with self.subTest(claim=claim):
                self.assertEqual(self.violations(evidence, claim), [])

    def test_release_object_swaps_appendages_and_synonyms_are_rejected(self):
        cases = [
            ("Leadpoet launched account scoring.", "Leadpoet launched account routing."),
            (
                "Leadpoet launched account scoring.",
                "Leadpoet launched account scoring and inbox automation.",
            ),
            ("Leadpoet is a sales product.", "Leadpoet unveiled inbox automation today."),
        ]
        for evidence, claim in cases:
            with self.subTest(claim=claim):
                self.assert_has_violation(evidence, claim, "release claim")

    def test_release_controls_allow_generic_entity_and_object_paraphrase(self):
        controls = [
            ("Acme launched a forecast dashboard.", "The company launched a forecast dashboard."),
            ("Leadpoet launched account scoring.", "Leadpoet shipped a way to rank accounts."),
            (
                "Leadpoet launched job-change alerts.",
                "Leadpoet released alerts for job changes.",
            ),
        ]
        for evidence, claim in controls:
            with self.subTest(claim=claim):
                self.assertEqual(self.violations(evidence, claim), [])

    def test_polarity_is_bound_to_the_same_relation_object(self):
        self.assert_has_violation(
            "Clean CRM data is not buyer intent.",
            "Clean CRM data is buyer intent.",
            "contradicts supplied evidence",
        )
        self.assert_has_violation(
            "Clean CRM data does not guarantee buyer readiness.",
            "Clean CRM data guarantees buyer readiness.",
            "contradicts supplied evidence",
        )
        self.assertEqual(
            self.violations(
                "Clean CRM data does not guarantee buyer readiness.",
                "Clean CRM data guarantees accurate reporting.",
            ),
            [],
        )

    def test_qualitative_quantifiers_require_evidence(self):
        cases = [
            "Sales cycles were cut in half.",
            "Acme serves hundreds of enterprise customers.",
            "Nearly all qualified buyers reply.",
        ]
        for claim in cases:
            with self.subTest(claim=claim):
                self.assert_has_violation("", claim, "quantified claim")
                self.assertEqual(self.violations(claim, claim), [])

    def test_funding_amount_allows_tense_round_generalization_and_fictional_prefix(self):
        evidence = "Fictional Contoso announced $40 million in Series B funding."
        claim = "Contoso announces $40M in funding."
        self.assertEqual(self.violations(evidence, claim), [])

    def test_each_causal_relation_is_checked_with_direction_and_entity(self):
        self.assert_has_violation(
            "Acme expanded because it launched an AI assistant.",
            "Acme expanded because it launched an AI assistant, while discounts caused demand to rise.",
            "causal claim",
        )
        self.assert_has_violation(
            "Acme expanded into Europe because it launched an AI assistant.",
            "Beta expanded into Europe because it launched an AI assistant.",
            "causal claim",
        )
        self.assert_has_violation(
            "Acme launched an AI assistant. Acme expanded into Europe.",
            "The AI assistant drove Acme's expansion into Europe.",
            "causal claim",
        )

    def test_causal_controls_allow_faithful_paraphrase_and_normative_followup(self):
        controls = [
            ("Lower prices caused demand to rise.", "Demand grew because prices fell."),
            (
                "Acme expanded into Europe because it launched an AI assistant.",
                "The AI assistant drove Acme's expansion into Europe.",
            ),
            (
                "Acme expanded because demand rose.",
                "Acme expanded because demand rose. If you use that as a sales trigger, "
                "keep the claim that narrow.",
            ),
        ]
        for evidence, claim in controls:
            with self.subTest(claim=claim):
                self.assertEqual(self.violations(evidence, claim), [])

    def test_authority_markers_cannot_launder_unsupplied_experience(self):
        cases = [
            "CROs tell me clean CRM data is mistaken for buyer intent.",
            "In my experience, clean CRM data is mistaken for buyer intent.",
            "Maya Lee argues that clean CRM data is not buyer intent.",
        ]
        for claim in cases:
            with self.subTest(claim=claim):
                self.assert_has_violation("", claim, "attributed/experiential")
                self.assertEqual(self.violations(claim, claim), [])
        self.assertEqual(
            self.violations(
                "We interviewed CROs about missing forecast evidence.",
                "We spoke with CROs about missing forecast evidence.",
            ),
            [],
        )

    def test_external_references_bind_full_email_and_general_tld(self):
        self.assert_has_violation("", "Read signaldesk.xyz/research.", "external reference")
        self.assert_has_violation(
            "Contact hello@example.com.",
            "Contact invented@example.com.",
            "external reference",
        )
        self.assertEqual(
            self.violations(
                "Read https://example.com/report?utm_source=x.",
                "Read https://example.com/report.",
            ),
            [],
        )

    def test_named_events_require_matching_subject_action_and_object(self):
        cases = [
            ("Acme raised $60M.", "Acme partnered with Salesforce."),
            ("Acme partnered with Salesforce.", "Beta partnered with Salesforce."),
            ("Acme was founded in 2020.", "Acme serves enterprise buyers across Europe."),
        ]
        for evidence, claim in cases:
            with self.subTest(claim=claim):
                self.assert_has_violation(evidence, claim, "named event claim")
        self.assertEqual(
            self.violations("Acme works with Salesforce.", "The company partnered with Salesforce."),
            [],
        )
        self.assertEqual(
            self.violations("", "Acme should partner with HubSpot."),
            [],
        )

    def test_stakeholder_involvement_cannot_be_inferred_from_a_review_event(self):
        evidence = "Acme now records confirmed security reviews as CRM events."
        unsafe = (
            "Acme adds security reviews to their CRM events: now reps can see "
            "when legal got involved."
        )
        self.assert_has_violation(evidence, unsafe, "stakeholder involvement")

    def test_stakeholder_involvement_controls_preserve_support_and_advice(self):
        self.assertEqual(
            self.violations(
                "A confirmed security review showed that legal got involved.",
                "Legal got involved in the security review.",
            ),
            [],
        )
        self.assertEqual(
            self.violations("", "If legal gets involved, ask what changed."),
            [],
        )

    def test_modal_hedges_cannot_launder_untethered_premises(self):
        evidence = (
            "Acme now records confirmed security reviews as CRM events. "
            "A rep can inspect the event before account prioritization."
        )
        unsafe = (
            "Security reviews and buyer readiness might look identical in CRMs.\n\n"
            "One could block the process. The other may suggest the buyer isn't convinced yet."
        )
        violations = self.violations(evidence, unsafe)
        self.assertEqual(
            [value for value in violations if "untethered interpretation" in value],
            [
                "remove or anchor untethered interpretation: One could block the process.",
                "remove or anchor untethered interpretation: The other may suggest the buyer isn't convinced yet.",
            ],
        )

    def test_modal_interpretation_controls_preserve_anchors_and_heuristics(self):
        evidence = (
            "Acme now records confirmed security reviews as CRM events. "
            "A rep can inspect the event before account prioritization."
        )
        controls = [
            "A security-review event may be worth checking before reprioritizing the account.",
            "That could be a reason to check the account before changing priority.",
            "If that might change priority, ask what evidence changed.",
        ]
        for claim in controls:
            with self.subTest(claim=claim):
                self.assertEqual(self.violations(evidence, claim), [])

    def test_questions_cannot_presuppose_unsupported_prediction(self):
        evidence = (
            "Acme now records confirmed security reviews as CRM events. "
            "A rep can inspect the event before account prioritization."
        )
        unsafe = (
            "When Acme adds security reviews to its CRM, ask: what buyer decision "
            "does this event predict?\n\n"
            "A strong answer names the specific choice the buyer makes next."
        )
        self.assert_has_violation(evidence, unsafe, "unsupported presupposition")

    def test_presuppositional_question_controls_preserve_boundaries_and_support(self):
        evidence = (
            "Acme now records confirmed security reviews as CRM events. "
            "The event predicts whether a review will happen."
        )
        controls = [
            "Does this event predict buyer readiness?",
            "What does this event establish?",
            "What outcome does this event predict?",
        ]
        for claim in controls:
            with self.subTest(claim=claim):
                self.assertEqual(self.violations(evidence, claim), [])

    def test_exact_audit_rejects_irrelevant_support_for_stakeholder_involvement(self):
        evidence = "Acme now records confirmed security reviews as CRM events."
        final = (
            "Acme adds security reviews to their CRM events: now reps can see "
            "when legal got involved."
        )
        violations = natural_exact_final_audit_contract_violations(
            self.audit_json(final, [{"claim": final, "support": evidence}]),
            final_text=final,
            request=self.request,
            bundle=self.bundle(evidence),
            trends=[],
        )
        self.assertTrue(
            any("relevant source support" in value for value in violations),
            violations,
        )

    def test_exact_audit_requires_each_factual_conjunct_and_relevant_support(self):
        final = "Acme raised $60M in Series B and partnered with Salesforce."
        evidence = "Acme raised $60M in Series B funding."
        partial_rows = [
            {"claim": "Acme raised $60M in Series B", "support": evidence},
        ]
        violations = natural_exact_final_audit_contract_violations(
            self.audit_json(final, partial_rows),
            final_text=final,
            request=self.request,
            bundle=self.bundle(evidence),
            trends=[],
        )
        self.assertTrue(any("omits an externally checkable clause" in value for value in violations))

        unrelated_rows = [{"claim": final, "support": evidence}]
        violations = natural_exact_final_audit_contract_violations(
            self.audit_json(final, unrelated_rows),
            final_text=final,
            request=self.request,
            bundle=self.bundle(evidence),
            trends=[],
        )
        self.assertTrue(any("relevant source support" in value for value in violations))

    def test_exact_audit_accepts_independently_supported_conjuncts(self):
        final = "Acme raised $60M in Series B and partnered with Salesforce."
        evidence = (
            "Acme raised $60M in Series B funding. "
            "Acme partnered with Salesforce."
        )
        rows = [
            {
                "claim": "Acme raised $60M in Series B",
                "support": "Acme raised $60M in Series B funding.",
            },
            {
                "claim": "partnered with Salesforce",
                "support": "Acme partnered with Salesforce.",
            },
        ]
        self.assertEqual(
            natural_exact_final_audit_contract_violations(
                self.audit_json(final, rows),
                final_text=final,
                request=self.request,
                bundle=self.bundle(evidence),
                trends=[],
            ),
            [],
        )

    def test_receipt_claim_detection_infers_capability_without_bundle(self):
        clauses = auditable_final_claim_clauses(
            "Leadpoet identifies buyer browsing activity.",
            bundle=None,
        )
        self.assertEqual(clauses, ["Leadpoet identifies buyer browsing activity."])

    def violations(self, evidence: str, claim: str) -> list[str]:
        return natural_grounding_violations(
            claim,
            self.request,
            self.bundle(evidence),
            [],
        )

    def assert_has_violation(self, evidence: str, claim: str, fragment: str) -> None:
        violations = self.violations(evidence, claim)
        self.assertTrue(
            any(fragment in value for value in violations),
            f"Expected {fragment!r} in {violations!r}",
        )

    @staticmethod
    def bundle(evidence: str) -> ContextBundle:
        sections = (
            [ContextSection(kind="proof", name="evidence", content=evidence)]
            if evidence
            else []
        )
        return ContextBundle(
            platform="x",
            topic="sales AI",
            post_type="founder_insight",
            style_sections=[],
            positioning_sections=sections,
            patterns=[],
            examples=[],
            notes_by_post_id={},
            metrics_by_post_id={},
            analytics_hints=[],
        )

    @staticmethod
    def audit_json(final: str, claim_rows: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "contract_version": "natural_exact_final_v1",
                "audited_text_sha256": exact_text_sha256(final),
                "passed": True,
                "quality_notes": "Every final claim was checked.",
                "warnings": [],
                "logic_audit": {
                    "premise": "Acme raised $60M in Series B",
                    "conclusion": "partnered with Salesforce",
                    "bridge_status": "supported",
                },
                "claim_audit": claim_rows,
                "quality_audit": {
                    "reader_payoff": {
                        "passed": True,
                        "type": "specific evidence",
                        "quote": "Acme raised $60M in",
                    },
                    "generic_swap": {
                        "passed": True,
                        "genericized_text": "A company raised funding and formed a partnership.",
                        "specific_mechanism": "partnered with Salesforce",
                    },
                    "originality": {
                        "passed": True,
                        "nearest_source": None,
                        "reason": "No source phrasing was copied.",
                    },
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
