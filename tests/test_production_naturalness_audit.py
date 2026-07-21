from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from leadpoet_social_brain.cli import (
    build_naturalness_plan_audit,
    build_production_naturalness_audit,
    cmd_audit_naturalness_x,
    production_pipeline_receipt,
    production_naturalness_audit_request_for_trend,
)
from leadpoet_social_brain.core.models import GeneratedDraft, TrendItem, utc_now
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


def valid_quality_audit(final_text: str) -> dict[str, object]:
    return {
        "reader_payoff": {"passed": True, "type": "mechanism", "quote": final_text},
        "generic_swap": {
            "passed": True,
            "genericized_text": final_text,
            "specific_mechanism": final_text,
        },
        "originality": {
            "passed": True,
            "nearest_source": None,
            "reason": "Fixture output is distinct from the available sources.",
        },
    }


def valid_production_snapshot(
    request,
    trend: TrendItem,
    *,
    final_text: str,
    gate_passed: bool = True,
) -> dict[str, object]:
    return {
        "request": request.__dict__,
        "generation_mode": "provider",
        "quality_mode": "quality_review",
        "strategy_summary": {"thesis": request.angle, "evidence_boundary": "closed"},
        "models_used": {
            "default": "fixture-model",
            "strategist": None,
            "natural_writer": "fixture-writer",
            "evaluator": "fixture-evaluator",
        },
        "evaluation": {
            "ranked_drafts": [1, 2, 3],
            "quality_notes": "Grounded and direct.",
            "warnings": [],
            "logic_audit": {
                "premise": final_text,
                "conclusion": final_text,
                "bridge_status": "supported",
            },
            "claim_audit": {},
            "quality_audit": valid_quality_audit(final_text),
            "selection_mode": "writer_option",
            "initial_evaluator_audited_text_sha256": hashlib.sha256(
                final_text.encode("utf-8")
            ).hexdigest(),
            "contract": {"valid": True, "attempts": 1, "initial_violations": []},
            "writer_option_diversity": {
                "valid": True,
                "attempts": 1,
                "initial_violations": [],
            },
            "exact_final_audit": {
                "contract_version": "natural_exact_final_v1",
                "audited_text_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
                "passed": True,
                "source": "initial_typed_evaluator",
                "audit_call_attempts": 0,
                "reused_initial_audit": True,
                "initial_reuse_violations": [],
            },
            "natural_quality_gate": {
                "passed": gate_passed,
                "initial_violations": [],
                "resolution": "selected_text_passed",
                "originality": {
                    "passed": True,
                    "violations": [],
                    "diagnostics": {
                        "source_count": 0,
                        "raw_source_count": 0,
                        "source_kind_counts": {},
                        "match_count": 0,
                        "matches": [],
                        "semantic_source_count": 0,
                        "semantic_lineage_count": 0,
                        "semantic_candidate_atoms": [],
                        "semantic_exempt_atoms": [],
                        "semantic_prior_lineage_counts": {},
                        "semantic_match_count": 0,
                        "semantic_matches": [],
                    },
                },
            },
        },
        "quality_notes": "Grounded and direct.",
        "selected_context_sections": [],
        "selected_examples": [],
        "skills": [{"path": "skills/x.md", "sha256": "fixture"}],
        "active_writing_rules": {"voice_rules": [], "review_rules": []},
        "selected_trends": [{"id": trend.id, "title": trend.title}],
    }


class ReceiptGenerator:
    def __init__(self, trends: dict[str, TrendItem], *, gate_passed: bool = True):
        self.trends = trends
        self.gate_passed = gate_passed
        self.requests = []
        self.recent_output_batches: list[list[str]] = []
        self.texts = [
            "Pipeline discipline starts with an evidence boundary. Clear inputs improve account decisions.",
            "Buyer timing changes the next action.\n\nA useful signal tells a seller what to inspect before outreach.",
        ]

    def generate(self, request, *, variants, save, recent_output_texts):
        self.requests.append(request)
        self.recent_output_batches.append(list(recent_output_texts or []))
        trend = self.trends[request.trend_ids[0]]
        text = self.texts[(len(self.requests) - 1) % len(self.texts)]
        return [
            GeneratedDraft(
                text=text,
                request=request,
                explanation="fixture",
                influences=[],
                context_snapshot=valid_production_snapshot(
                    request,
                    trend,
                    final_text=text,
                    gate_passed=self.gate_passed,
                ),
            )
        ]


class ValidNaturalProvider:
    def __init__(self):
        self.prompts: list[str] = []
        self.options = [
            "A new account signal is trivia until it changes what a seller does next.",
            "The useful question is not whether a signal exists. It is which seller action the evidence changes.",
            "Sales teams do not need another alert. They need enough context to choose the next account action.",
        ]

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Write 3 X draft options for this workspace" in prompt:
            return self.options[:variants]
        if "final line edit" in prompt:
            # Grounding-preserving polish pass. The selected copy is already
            # tight, so the line editor returns it unchanged.
            return [self.options[0]]
        if "Choose the best X post for this workspace" in prompt:
            return [
                json.dumps(
                    {
                        "ranked_drafts": [
                            {"draft": 1, "rank": 1},
                            {"draft": 2, "rank": 2},
                            {"draft": 3, "rank": 3},
                        ],
                        "quality_notes": "The first option is direct, grounded, and useful.",
                        "logic_audit": {
                            "premise": self.options[0],
                            "conclusion": self.options[0],
                            "bridge_status": "supported",
                        },
                        "claim_audit": {},
                        "quality_audit": valid_quality_audit(self.options[0]),
                        "warnings": [],
                        "suggested_final_version": self.options[0],
                    }
                )
            ]
        raise AssertionError(f"Unexpected production prompt: {prompt[:120]}")


class ProductionNaturalnessAuditTests(unittest.TestCase):
    def make_store_with_trends(self, tmpdir: str, count: int = 2) -> tuple[SQLiteStore, list[TrendItem]]:
        store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
        store.seed_defaults()
        trends = []
        for index in range(count):
            trend = store.save_trend_item(
                TrendItem(
                    id=f"production-audit-trend-{index}",
                    title=f"Production trend {index}",
                    source_type="web",
                    topic="sales AI",
                    summary=f"A concrete market change {index}.",
                    why_it_matters=f"It changes seller action {index}.",
                    leadpoet_angle=f"Explain what the market change means for seller action {index}.",
                    date=utc_now(),
                )
            )
            trends.append(trend)
        return store, trends

    def test_production_request_uses_natural_profile_without_legacy_audit_instruction(self):
        trend = TrendItem(
            id="trend-1",
            title="A market change",
            source_type="web",
            topic="sales AI",
            summary="Something changed.",
            why_it_matters="The next action changes.",
            leadpoet_angle="Explain the operational consequence.",
            date=utc_now(),
        )

        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type="founder_insight",
            length_mode="standard",
        )

        self.assertEqual(request.prompt_profile, "natural")
        self.assertIsNone(request.extra_context)
        self.assertEqual(request.trend_ids, [trend.id])
        self.assertNotIn("Naturalness audit", str(request.__dict__))
        self.assertNotIn("Batch variation slot", str(request.__dict__))

    def test_production_builder_verifies_receipts_and_feeds_batch_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store, trends = self.make_store_with_trends(tmpdir)
            generator = ReceiptGenerator({trend.id: trend for trend in trends})

            payload = build_production_naturalness_audit(
                store,
                generator,
                count=2,
                trend_limit=2,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["mode"], "production_natural")
            self.assertEqual(payload["pipeline"], "natural_writer_evaluator_final_gate")
            self.assertEqual(payload["post_count"], 2)
            self.assertEqual(payload["generation_failure_count"], 0)
            self.assertEqual(payload["pipeline_receipt_pass_count"], 2)
            self.assertEqual(payload["pipeline_receipt_failure_count"], 0)
            self.assertEqual(payload["exact_duplicate_count"], 0)
            self.assertEqual(payload["selection_mode_counts"], {"writer_option": 2})
            self.assertEqual(payload["evaluator_contract_attempt_counts"], {"1": 2})
            self.assertEqual(payload["quality_gate_resolution_counts"], {"selected_text_passed": 2})
            self.assertTrue(all(request.prompt_profile == "natural" for request in generator.requests))
            self.assertTrue(all(request.extra_context is None for request in generator.requests))
            self.assertEqual(generator.recent_output_batches[0], [])
            self.assertEqual(generator.recent_output_batches[1], [generator.texts[0]])
            self.assertFalse(any("receipt verification failed" in warning for warning in payload["warnings"]))
            store.close()

    def test_production_builder_warns_when_final_gate_receipt_is_not_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store, trends = self.make_store_with_trends(tmpdir, count=1)
            generator = ReceiptGenerator(
                {trend.id: trend for trend in trends},
                gate_passed=False,
            )

            payload = build_production_naturalness_audit(
                store,
                generator,
                count=1,
                trend_limit=1,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["pipeline_receipt_failure_count"], 1)
            self.assertIn(
                "final_quality_gate_passed",
                payload["pipeline_receipt_failures"][0]["violations"],
            )
            self.assertTrue(any("receipt verification failed" in warning for warning in payload["warnings"]))
            store.close()

    def test_receipt_rejects_missing_bridge(self):
        trend = TrendItem(
            id="receipt-trend",
            title="Synthetic change",
            source_type="synthetic",
            topic="sales",
            summary="A field changed.",
            why_it_matters="",
            leadpoet_angle="",
        )
        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type="founder_insight",
            length_mode="standard",
        )
        text = "A forecast change should name the evidence behind it."
        snapshot = valid_production_snapshot(request, trend, final_text=text)
        snapshot["evaluation"]["logic_audit"]["bridge_status"] = "missing"
        draft = GeneratedDraft(
            text=text,
            request=request,
            explanation="fixture",
            influences=[],
            context_snapshot=snapshot,
        )

        receipt = production_pipeline_receipt(draft, trend_id=trend.id)

        self.assertFalse(receipt["passed"])
        self.assertIn("logic_audit_recorded", receipt["violations"])

    def test_receipt_rejects_empty_claim_audit_for_high_risk_clause(self):
        trend = TrendItem(
            id="receipt-funding-trend",
            title="Synthetic funding",
            source_type="synthetic",
            topic="sales",
            summary="Acme announced $40 million in Series B funding.",
            why_it_matters="",
            leadpoet_angle="",
        )
        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type="founder_insight",
            length_mode="standard",
        )
        text = "Acme raised $40 million in its Series B."
        snapshot = valid_production_snapshot(request, trend, final_text=text)
        snapshot["evaluation"]["claim_audit"] = []
        draft = GeneratedDraft(
            text=text,
            request=request,
            explanation="fixture",
            influences=[],
            context_snapshot=snapshot,
        )

        receipt = production_pipeline_receipt(draft, trend_id=trend.id)

        self.assertFalse(receipt["passed"])
        self.assertIn("claim_audit_recorded", receipt["violations"])

    def test_receipt_rejects_claim_audit_marked_unsupported(self):
        trend = TrendItem(
            id="receipt-unsupported-trend",
            title="Synthetic funding",
            source_type="synthetic",
            topic="sales",
            summary="Acme announced $40 million in Series B funding.",
            why_it_matters="",
            leadpoet_angle="",
        )
        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type="founder_insight",
            length_mode="standard",
        )
        text = "Acme raised $40 million in its Series B."
        snapshot = valid_production_snapshot(request, trend, final_text=text)
        snapshot["evaluation"]["claim_audit"] = [
            {"claim": text, "support": "unsupported"}
        ]
        draft = GeneratedDraft(
            text=text,
            request=request,
            explanation="fixture",
            influences=[],
            context_snapshot=snapshot,
        )

        receipt = production_pipeline_receipt(draft, trend_id=trend.id)

        self.assertFalse(receipt["passed"])
        self.assertIn("claim_audit_recorded", receipt["violations"])

    def test_receipt_rejects_internally_inconsistent_exact_audit_source(self):
        trend = TrendItem(
            id="receipt-source-trend",
            title="Synthetic change",
            source_type="synthetic",
            topic="sales",
            summary="A field changed.",
            why_it_matters="",
            leadpoet_angle="",
        )
        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type="founder_insight",
            length_mode="standard",
        )
        text = "A forecast change should name the evidence behind it."
        mutations = [
            {"initial_reuse_violations": ["initial audit hash mismatch"]},
            {"audit_call_attempts": True},
        ]

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                snapshot = valid_production_snapshot(request, trend, final_text=text)
                snapshot["evaluation"]["exact_final_audit"].update(mutation)
                draft = GeneratedDraft(
                    text=text,
                    request=request,
                    explanation="fixture",
                    influences=[],
                    context_snapshot=snapshot,
                )

                receipt = production_pipeline_receipt(draft, trend_id=trend.id)

                self.assertFalse(receipt["passed"])
                self.assertIn("exact_final_audit_recorded", receipt["violations"])

    def test_production_builder_receipt_matches_real_natural_generator_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _trends = self.make_store_with_trends(tmpdir, count=1)
            provider = ValidNaturalProvider()

            from leadpoet_social_brain.generation import SocialBrainGenerator

            payload = build_production_naturalness_audit(
                store,
                SocialBrainGenerator(store, provider=provider),
                count=1,
                trend_limit=1,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["post_count"], 1)
            self.assertEqual(payload["pipeline_receipt_pass_count"], 1)
            self.assertEqual(payload["pipeline_receipt_failure_count"], 0)
            # writer options -> typed evaluator -> grounding-preserving polish.
            self.assertEqual(len(provider.prompts), 3)
            self.assertTrue(any("final line edit" in prompt for prompt in provider.prompts))
            self.assertFalse(any("Do not write the post yet" in prompt for prompt in provider.prompts))
            self.assertFalse(any("Batch variation slot" in prompt for prompt in provider.prompts))
            store.close()

    def test_production_builder_reports_quality_failure_but_does_not_hide_programming_error(self):
        class QualityFailureGenerator:
            def generate(self, request, *, variants, save, recent_output_texts):
                raise ValueError("Natural quality gate rejected the repaired post")

        class BuggyGenerator:
            def generate(self, request, *, variants, save, recent_output_texts):
                raise AttributeError("fixture programmer error")

        with tempfile.TemporaryDirectory() as tmpdir:
            store, _trends = self.make_store_with_trends(tmpdir, count=1)
            payload = build_production_naturalness_audit(
                store,
                QualityFailureGenerator(),
                count=1,
                trend_limit=1,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
                save_drafts=False,
            )

            self.assertEqual(payload["post_count"], 0)
            self.assertEqual(payload["generation_failure_count"], 1)
            self.assertEqual(payload["generation_failures"][0]["error_type"], "ValueError")
            self.assertTrue(any("generation failed" in warning for warning in payload["warnings"]))

            with self.assertRaisesRegex(AttributeError, "fixture programmer error"):
                build_production_naturalness_audit(
                    store,
                    BuggyGenerator(),
                    count=1,
                    trend_limit=1,
                    trend_status="unused",
                    post_type="founder_insight",
                    length_mode="standard",
                    save_drafts=False,
                )
            store.close()

    def test_plan_payload_explicitly_labels_legacy_only_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _trends = self.make_store_with_trends(tmpdir, count=1)

            payload = build_naturalness_plan_audit(
                store,
                count=1,
                trend_limit=1,
                trend_status="unused",
                post_type="founder_insight",
                length_mode="standard",
            )

            self.assertEqual(payload["mode"], "plan_only")
            self.assertEqual(payload["scope"], "legacy_structure_source_seed_only")
            self.assertFalse(payload["production_pipeline_exercised"])
            store.close()

    def test_cli_live_audit_defaults_to_production_and_legacy_is_opt_in(self):
        base_args = {
            "plan_only": False,
            "count": 1,
            "trend_limit": 1,
            "trend_status": "unused",
            "post_type": "founder_insight",
            "length": "standard",
            "save_drafts": False,
            "cover_all_trends": False,
            "json_output": True,
            "fail_on_warning": False,
        }
        production_payload = {"mode": "production_natural", "warnings": []}
        legacy_payload = {"warnings": []}

        with patch("leadpoet_social_brain.cli.load_configured_provider", return_value=object()), patch(
            "leadpoet_social_brain.cli.SocialBrainGenerator", return_value=object()
        ), patch(
            "leadpoet_social_brain.cli.build_production_naturalness_audit",
            return_value=production_payload,
        ) as production_builder, patch(
            "leadpoet_social_brain.cli.build_naturalness_audit",
            return_value=legacy_payload,
        ) as legacy_builder, redirect_stdout(io.StringIO()):
            exit_code = cmd_audit_naturalness_x(
                object(),
                SimpleNamespace(**base_args, legacy_structure=False),
            )

        self.assertEqual(exit_code, 0)
        production_builder.assert_called_once()
        legacy_builder.assert_not_called()

        with patch("leadpoet_social_brain.cli.load_configured_provider", return_value=object()), patch(
            "leadpoet_social_brain.cli.SocialBrainGenerator", return_value=object()
        ), patch(
            "leadpoet_social_brain.cli.build_production_naturalness_audit",
            return_value=production_payload,
        ) as production_builder, patch(
            "leadpoet_social_brain.cli.build_naturalness_audit",
            return_value=legacy_payload,
        ) as legacy_builder, redirect_stdout(io.StringIO()):
            exit_code = cmd_audit_naturalness_x(
                object(),
                SimpleNamespace(**base_args, legacy_structure=True),
            )

        self.assertEqual(exit_code, 0)
        production_builder.assert_not_called()
        legacy_builder.assert_called_once()


if __name__ == "__main__":
    unittest.main()
