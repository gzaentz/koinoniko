from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from leadpoet_social_brain.web.app import handle_api_request
from leadpoet_social_brain.web.railway_entrypoint import TASK_BY_SERVICE_NAME
from leadpoet_social_brain.web.task_client import TASK_DEFAULTS


def artifact_payload() -> dict[str, object]:
    return {
        "title": "The signal-aware outbound prompt vault",
        "promise": "Turn account context into a repeatable outbound workflow.",
        "audience": "sales and GTM operators",
        "expected_outcome": "A researched account brief and usable first-touch sequence.",
        "overview": "Work through the prompts in order and review every claim before use.",
        "format": "prompt_library",
        "sections": [
            {
                "heading": "Account research",
                "prompts": [
                    {
                        "name": "Account reason prompt",
                        "purpose": "Find the observable reason to prioritize an account.",
                        "required_inputs": ["account", "recent signals"],
                        "prompt": "Using only the supplied facts, identify the strongest account reason.",
                        "usage_steps": ["Add sources", "Run", "Verify"],
                        "expected_output": "A sourced account reason.",
                    }
                ],
            },
            {
                "heading": "Message review",
                "items": ["Remove unsupported claims", "Keep the observable account reason"],
            },
        ],
        "implementation_checklist": ["Collect account evidence", "Verify every claim", "Review the final message"],
        "leadpoet_workflow": {
            "heading": "Where Leadpoet fits",
            "context_basis": "Saved product context",
            "steps": ["Use Leadpoet to carry the verified account context into the approved workflow."],
        },
        "source_notes": [],
        "cta": "Use the vault on one live account, then adapt it to your process.",
    }


class FakeProvider:
    model = "fake-model"

    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        return [json.dumps(artifact_payload())]


def candidate() -> dict[str, object]:
    return {
        "title": "Free sales prospecting prompt library",
        "source_url": "https://example.com/sales-prompts?utm_source=test",
        "platform": "linkedin",
        "author": "Example Author",
        "published_at": "2026-07-03T12:00:00+00:00",
        "source_text": "Free sales prospecting prompt library and Notion template for outbound teams.",
        "likes": 800,
        "replies": 120,
        "reposts": 75,
        "bookmarks": 240,
        "impressions": 25_000,
    }


class LeadMagnetWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "lead-magnets.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def request(self, method: str, path: str, payload: dict[str, object] | None = None):
        return handle_api_request(
            method,
            path,
            json.dumps(payload or {}).encode("utf-8"),
            db_path=self.db_path,
        )

    def test_research_generate_approve_and_publish_workflow(self):
        with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=None):
            status, research = self.request(
                "POST",
                "/api/lead-magnets/research",
                {"window_days": 30, "candidates": [candidate()]},
            )
        self.assertEqual(status, 200)
        self.assertEqual(research["run"]["sources_qualified"], 1)
        self.assertEqual(research["sources"][0]["evidence_strength"], "metric_backed")
        self.assertEqual(research["sources"][0]["canonical_url"], "https://example.com/sales-prompts")

        source_id = research["sources"][0]["id"]
        run_id = research["run"]["id"]
        with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=FakeProvider()):
            status, created = self.request(
                "POST",
                "/api/lead-magnets",
                {
                    "audience": "sales and GTM operators",
                    "sales_outcome": "build a signal-aware outbound workflow",
                    "format": "prompt_library",
                    "source_ids": [source_id],
                    "research_run_id": run_id,
                },
            )
        self.assertEqual(status, 200)
        item = created["item"]
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["research_strength"], "directional")
        self.assertIn("Leadpoet", item["markdown"])
        self.assertEqual(len(item["versions"]), 1)

        magnet_id = item["id"]
        status, approved = self.request("POST", f"/api/lead-magnets/{magnet_id}/approve")
        self.assertEqual(status, 200)
        self.assertEqual(approved["item"]["status"], "approved")

        notion_result = SimpleNamespace(
            page_id="notion-page-1",
            url="https://notion.so/notion-page-1",
            public_url=None,
            created=True,
            block_count=12,
            batch_count=1,
            content_hash="abc123",
        )
        with patch("leadpoet_social_brain.web.app.publish_to_notion", return_value=notion_result) as publish:
            status, published = self.request("POST", f"/api/lead-magnets/{magnet_id}/publish")
        self.assertEqual(status, 200)
        self.assertEqual(published["item"]["status"], "published")
        self.assertEqual(published["item"]["notion_page_id"], "notion-page-1")
        publish.assert_called_once()

    def test_publish_requires_approval(self):
        with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=FakeProvider()):
            status, created = self.request(
                "POST",
                "/api/lead-magnets",
                {"audience": "sales teams", "sales_outcome": "improve account research"},
            )
        self.assertEqual(status, 200)
        status, payload = self.request("POST", f"/api/lead-magnets/{created['item']['id']}/publish")
        self.assertEqual(status, 400)
        self.assertIn("Approve", payload["error"])

    def test_manual_research_continues_when_provider_configuration_fails(self):
        with patch(
            "leadpoet_social_brain.web.app.load_configured_provider",
            side_effect=RuntimeError("provider configuration failed"),
        ):
            status, payload = self.request(
                "POST",
                "/api/lead-magnets/research",
                {"window_days": 30, "candidates": [candidate()]},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["sources_qualified"], 1)
        self.assertTrue(any("deterministic research continued" in warning for warning in payload["warnings"]))

    def test_autopilot_is_idempotent_for_fourteen_day_period(self):
        payload = {
            "now": "2026-07-06T12:00:00+00:00",
            "candidates": [candidate()],
            "window_days": 30,
            "audience": "sales teams",
            "sales_outcome": "improve prospecting research",
        }
        with patch("leadpoet_social_brain.web.app.load_configured_provider", return_value=FakeProvider()):
            first_status, first = self.request("POST", "/tasks/autopilot-lead-magnet", payload)
            second_status, second = self.request("POST", "/tasks/autopilot-lead-magnet", payload)
        self.assertEqual(first_status, 200)
        self.assertFalse(first["skipped"])
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(second_status, 200)
        self.assertTrue(second["skipped"])
        self.assertEqual(second["created"], 1)
        self.assertEqual(second["items"][0]["id"], first["item"]["id"])

    def test_task_registration_includes_biweekly_autopilot(self):
        self.assertIn("autopilot-lead-magnet", TASK_DEFAULTS)
        self.assertEqual(TASK_BY_SERVICE_NAME["autopilot-lead-magnet"], "autopilot-lead-magnet")
        self.assertEqual(TASK_DEFAULTS["autopilot-lead-magnet"]["window_days"], 30)

    def test_static_ui_exposes_research_create_review_and_publish(self):
        static_dir = Path(__file__).parents[1] / "leadpoet_social_brain" / "web" / "static"
        html = (static_dir / "index.html").read_text()
        javascript = (static_dir / "app.js").read_text()
        styles = (static_dir / "styles.css").read_text()
        self.assertIn('data-view="leadMagnets"', html)
        self.assertIn('id="leadMagnetsView"', html)
        self.assertIn('id="researchLeadMagnetsButton"', html)
        self.assertIn('id="leadMagnetCreateModal"', html)
        self.assertIn('id="approveLeadMagnetButton"', html)
        self.assertIn('id="publishLeadMagnetButton"', html)
        self.assertIn('api("/api/lead-magnets/research"', javascript)
        self.assertIn('/api/lead-magnets/${encodeURIComponent(id)}/${action}', javascript)
        self.assertIn("observed_metrics", javascript)
        self.assertIn(".lead-magnet-evidence-card", styles)
        self.assertIn(".lead-magnet-markdown", styles)


if __name__ == "__main__":
    unittest.main()
