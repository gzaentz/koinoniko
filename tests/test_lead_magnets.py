from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from leadpoet_social_brain.core.models import (
    ContextSection,
    LeadMagnet,
    LeadMagnetResearchRun,
    LeadMagnetSource,
    LeadMagnetVersion,
)
from leadpoet_social_brain.lead_magnet_scout import fetch_x_lead_magnet_candidates, research_lead_magnets
from leadpoet_social_brain.lead_magnets import generate_lead_magnet
from leadpoet_social_brain.platforms.x_api import XApiClient, XWriteClient
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


NOW = "2026-07-10T12:00:00+00:00"


class StaticLeadMagnetProvider:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        _ = model
        self.prompts.append(prompt)
        return [json.dumps(self.response) for _index in range(variants)]


class FailingLeadMagnetProvider:
    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        raise RuntimeError("classification provider unavailable")


def lead_magnet_candidate(
    *,
    title: str,
    published_at: str,
    source_url: str,
    **metrics: object,
) -> dict[str, object]:
    return {
        "title": title,
        "source_summary": title,
        "published_at": published_at,
        "source_url": source_url,
        **metrics,
    }


class LeadMagnetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/lead-magnets.sqlite3"
        self.store = SQLiteStore(self.db_path, workspace_id="alpha")

    def tearDown(self) -> None:
        self.store.close()
        self.tmpdir.cleanup()

    def test_x_clients_use_current_api_hostname(self):
        self.assertEqual(XApiClient("token").base_url, "https://api.x.com/2")
        self.assertEqual(
            XWriteClient(oauth2_access_token="token").base_url,
            "https://api.x.com/2",
        )

    def test_storage_round_trip_status_and_version_cascade(self):
        source = self.store.save_lead_magnet_source(
            LeadMagnetSource(
                title="Outbound account research prompt library",
                source_url="https://example.com/prompts?utm_source=x",
                canonical_url="https://example.com/prompts",
                platform="linkedin",
                author="Sales Operator",
                published_at="2026-07-01T10:00:00+00:00",
                format="prompt_library",
                sales_topic="outbound",
                hook="Free prompt library",
                promise="Research accounts before writing outreach",
                audience="SDRs",
                delivery_mechanism="notion",
                cta="Comment to receive it",
                leadpoet_integration="Use account timing as an input",
                why_it_worked="Specific workflow and immediate utility",
                likes=120,
                replies=18,
                reposts=21,
                bookmarks=80,
                impressions=12_000,
                follower_count=4_000,
                metric_observed_at=NOW,
                performance_score=77.5,
                evidence_strength="metric_backed",
                raw_text="Observed source payload",
                status="active",
            )
        )
        run = self.store.save_lead_magnet_research_run(
            LeadMagnetResearchRun(
                window_start="2026-06-10T12:00:00+00:00",
                window_end=NOW,
                period_key="2026-07-fortnight-1",
                query="sales lead magnets",
                provider="deterministic",
                candidates_found=4,
                sources_qualified=1,
                source_ids=[source.id],
                warnings=["directional sample"],
            )
        )
        magnet = self.store.save_lead_magnet(
            LeadMagnet(
                title="Account Research Prompt Library",
                promise="Turn account evidence into a useful sales brief",
                audience="SDRs",
                sales_outcome="Better account research",
                format="prompt_library",
                depth="deep",
                product_integration="embedded",
                cta="Try the workflow",
                status="needs_review",
                source_ids=[source.id],
                research_run_id=run.id,
                evidence_summary="One metric-backed source.",
                content={"sections": [{"heading": "Research", "prompts": ["Prompt"]}]},
                markdown="# Account Research Prompt Library\n",
                generation_brief={"audience": "SDRs", "depth": "deep"},
            )
        )
        version = self.store.save_lead_magnet_version(
            LeadMagnetVersion(
                lead_magnet_id=magnet.id,
                content=magnet.content,
                markdown=magnet.markdown,
                edit_type="generated",
                source_ids=[source.id],
            )
        )

        restored_source = self.store.get_lead_magnet_source(source.id)
        restored_run = self.store.get_lead_magnet_research_run(run.id)
        restored_magnet = self.store.get_lead_magnet(magnet.id)
        self.assertEqual(restored_source.performance_score, 77.5)
        self.assertEqual(restored_run.source_ids, [source.id])
        self.assertEqual(restored_run.warnings, ["directional sample"])
        self.assertEqual(restored_magnet.content, magnet.content)
        self.assertEqual(restored_magnet.generation_brief, magnet.generation_brief)
        self.assertEqual(self.store.latest_lead_magnet_version(magnet.id).id, version.id)
        self.assertEqual(self.store.list_lead_magnets(status="needs_review"), [restored_magnet])

        self.assertEqual(self.store.mark_lead_magnet_source_status(source.id, "qualified").status, "qualified")
        completed_run = self.store.mark_lead_magnet_research_run_status(run.id, "completed")
        self.assertEqual(completed_run.status, "completed")
        self.assertIsNotNone(completed_run.completed_at)
        published = self.store.mark_lead_magnet_status(magnet.id, "published")
        self.assertEqual(published.status, "published")
        self.assertIsNotNone(published.published_at)

        self.assertTrue(self.store.delete_lead_magnet(magnet.id))
        self.assertIsNone(self.store.get_lead_magnet_version(version.id))

    def test_scout_enforces_rolling_thirty_day_window(self):
        result = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Free outbound sales prompt library",
                    published_at="2026-06-11T12:00:00+00:00",
                    source_url="https://example.com/recent",
                    likes=25,
                ),
                lead_magnet_candidate(
                    title="Free outbound sales playbook",
                    published_at="2026-06-09T12:00:00+00:00",
                    source_url="https://example.com/stale",
                    likes=500,
                ),
            ],
            now=NOW,
            window_days=30,
            period_key="window-test",
        )

        self.assertEqual([source.canonical_url for source in result.sources], ["https://example.com/recent"])
        self.assertEqual(result.run.candidates_found, 2)
        self.assertEqual(result.run.sources_qualified, 1)
        self.assertEqual(result.run.window_start, "2026-06-10T12:00:00+00:00")

    def test_scout_distinguishes_metric_backed_and_qualitative_sources(self):
        result = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Free GTM prompt library for account research",
                    published_at="2026-07-05T12:00:00+00:00",
                    source_url="https://example.com/metric",
                    likes=100,
                    replies=12,
                    reposts=14,
                    bookmarks=60,
                    impressions=10_000,
                ),
                lead_magnet_candidate(
                    title="Sales prospecting workflow guide",
                    published_at="2026-07-04T12:00:00+00:00",
                    source_url="https://example.com/qualitative",
                ),
            ],
            now=NOW,
            period_key="evidence-test",
        )

        by_url = {source.canonical_url: source for source in result.sources}
        self.assertEqual(by_url["https://example.com/metric"].evidence_strength, "metric_backed")
        self.assertGreater(by_url["https://example.com/metric"].performance_score, 0)
        self.assertEqual(by_url["https://example.com/qualitative"].evidence_strength, "qualitative")
        self.assertEqual(by_url["https://example.com/qualitative"].performance_score, 0)
        self.assertTrue(any("Only 1 source" in warning for warning in result.warnings))
        self.assertEqual(result.run.status, "partial")

    def test_scout_keeps_deterministic_source_when_ai_classification_fails(self):
        result = research_lead_magnets(
            self.store,
            FailingLeadMagnetProvider(),
            candidates=[
                lead_magnet_candidate(
                    title="Free outbound sales prompt library",
                    published_at="2026-07-05T12:00:00+00:00",
                    source_url="https://example.com/provider-failure",
                    likes=125,
                    bookmarks=45,
                )
            ],
            now=NOW,
            period_key="provider-failure-test",
        )

        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].format, "prompt_library")
        self.assertEqual(result.sources[0].evidence_strength, "metric_backed")
        self.assertTrue(any("AI classification failed" in warning for warning in result.warnings))

    def test_scout_rejects_non_sales_and_non_magnet_candidates(self):
        result = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Free home cooking checklist",
                    published_at="2026-07-02T12:00:00+00:00",
                    source_url="https://example.com/cooking",
                ),
                lead_magnet_candidate(
                    title="Sales AI buyer intent market analysis",
                    published_at="2026-07-02T12:00:00+00:00",
                    source_url="https://example.com/sales-news",
                ),
            ],
            now=NOW,
            period_key="relevance-test",
        )

        self.assertEqual(result.sources, [])
        self.assertEqual(self.store.list_lead_magnet_sources(), [])
        self.assertTrue(any("not both sales-related and lead-magnet-like" in warning for warning in result.warnings))

    def test_scout_canonicalizes_and_deduplicates_tracking_urls(self):
        result = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Free sales template pack for outbound",
                    published_at="2026-07-03T12:00:00+00:00",
                    source_url="https://www.example.com/templates/?utm_source=x&ref=feed",
                    likes=30,
                ),
                lead_magnet_candidate(
                    title="Free sales template pack for outbound",
                    published_at="2026-07-03T12:00:00+00:00",
                    source_url="https://example.com/templates?utm_campaign=launch&fbclid=123",
                    likes=300,
                ),
            ],
            now=NOW,
            period_key="dedupe-test",
        )

        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].canonical_url, "https://example.com/templates")
        self.assertEqual(result.sources[0].likes, 30)
        self.assertEqual(result.run.candidates_found, 2)

    def test_x_search_supplies_recent_metric_backed_candidates(self):
        requests: list[tuple[str, dict[str, str]]] = []

        class FakeXClient:
            def get_json(self, path: str, params: dict[str, str]):
                requests.append((path, params))
                return {
                    "data": [
                        {
                            "id": "123",
                            "author_id": "author-1",
                            "created_at": "2026-07-04T12:00:00.000Z",
                            "text": "Free sales prospecting prompt library and Notion template for outbound teams.",
                            "public_metrics": {
                                "like_count": 900,
                                "reply_count": 140,
                                "retweet_count": 85,
                                "bookmark_count": 310,
                                "impression_count": 40_000,
                            },
                        },
                        {
                            "id": "456",
                            "author_id": "author-1",
                            "created_at": "2026-07-04T12:00:00.000Z",
                            "text": "Ordinary sales market commentary without a resource.",
                            "public_metrics": {"like_count": 9_000},
                        },
                    ],
                    "includes": {
                        "users": [
                            {
                                "id": "author-1",
                                "username": "salesops",
                                "name": "Sales Ops",
                                "public_metrics": {"followers_count": 12_000},
                            }
                        ]
                    },
                    "meta": {},
                }

        candidates = fetch_x_lead_magnet_candidates(
            client=FakeXClient(),
            now=NOW,
            window_days=30,
            endpoint="all",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_url"], "https://x.com/salesops/status/123")
        self.assertEqual(candidates[0]["likes"], 900)
        self.assertEqual(candidates[0]["bookmarks"], 310)
        self.assertEqual(candidates[0]["follower_count"], 12_000)
        self.assertEqual(requests[0][0], "/tweets/search/all")
        self.assertEqual(requests[0][1]["start_time"], "2026-06-10T12:00:00Z")
        self.assertEqual(requests[0][1]["end_time"], "2026-07-10T11:59:30Z")
        self.assertIn("prompt library", requests[0][1]["query"])

    def test_scout_reports_x_failure_when_qualitative_fallback_runs(self):
        fallback = {
            "title": "Free outbound sales prompt library",
            "source_url": "https://example.com/fallback",
        }
        with patch.dict(os.environ, {"X_BEARER_TOKEN": "test-token"}):
            with patch(
                "leadpoet_social_brain.lead_magnet_scout.fetch_x_lead_magnet_candidates",
                side_effect=RuntimeError("X API request failed (429): Too Many Requests"),
            ):
                with patch(
                    "leadpoet_social_brain.lead_magnet_scout.fetch_scrapingdog_pattern_candidates",
                    return_value=[fallback],
                ):
                    result = research_lead_magnets(
                        self.store,
                        now=NOW,
                        period_key="x-fallback-warning-test",
                    )

        self.assertEqual(result.sources, [])
        self.assertTrue(any("X API request failed (429)" in warning for warning in result.warnings))
        self.assertTrue(any("without a verifiable publish date" in warning for warning in result.warnings))

    def test_workspace_isolation_and_period_idempotency(self):
        first = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Sales prompt library for SDR account research",
                    published_at="2026-07-01T12:00:00+00:00",
                    source_url="https://example.com/alpha",
                    likes=50,
                )
            ],
            now=NOW,
            period_key="2026-fortnight-14",
        )
        retried = research_lead_magnets(
            self.store,
            candidates=[
                lead_magnet_candidate(
                    title="Sales prompt library for SDR account research",
                    published_at="2026-07-01T12:00:00+00:00",
                    source_url="https://example.com/alpha?utm_source=retry",
                    likes=75,
                )
            ],
            now=NOW,
            period_key="2026-fortnight-14",
        )

        self.assertEqual(retried.run.id, first.run.id)
        self.assertEqual(len(self.store.list_lead_magnet_research_runs(period_key="2026-fortnight-14")), 1)
        self.assertEqual(len(self.store.list_lead_magnet_sources()), 1)
        self.assertEqual(self.store.list_lead_magnet_sources()[0].likes, 75)

        other = SQLiteStore(self.db_path, workspace_id="beta")
        try:
            self.assertIsNone(other.get_lead_magnet_source(first.sources[0].id))
            self.assertEqual(other.list_lead_magnet_sources(), [])
            beta = research_lead_magnets(
                other,
                candidates=[
                    lead_magnet_candidate(
                        title="Sales playbook for outbound teams",
                        published_at="2026-07-02T12:00:00+00:00",
                        source_url="https://example.com/beta",
                        likes=20,
                    )
                ],
                now=NOW,
                period_key="2026-fortnight-14",
            )
            self.assertNotEqual(beta.run.id, first.run.id)
            self.assertEqual(len(other.list_lead_magnet_research_runs()), 1)
            self.assertEqual(len(self.store.list_lead_magnet_research_runs()), 1)
        finally:
            other.close()

    def test_generation_persists_prompt_library_and_leadpoet_workflow(self):
        self.store.save_context_section(
            ContextSection(
                kind="product",
                name="leadpoet_product_test",
                content=(
                    "Leadpoet helps sales teams use buyer timing and account context to prioritize accounts "
                    "before they draft outreach."
                ),
            )
        )
        source = self.store.save_lead_magnet_source(
            LeadMagnetSource(
                title="Sales account research prompt library",
                source_url="https://example.com/source",
                canonical_url="https://example.com/source",
                published_at="2026-07-01T12:00:00+00:00",
                format="prompt_library",
                sales_topic="account research",
                promise="Give SDRs a repeatable account research workflow",
                likes=80,
                bookmarks=45,
                impressions=8_000,
                performance_score=50,
                evidence_strength="metric_backed",
                status="active",
            )
        )
        provider = StaticLeadMagnetProvider(
            {
                "title": "The Account Research Prompt Library",
                "promise": "Turn account evidence into a useful outbound brief.",
                "audience": "SDRs and sales founders",
                "expected_outcome": "A grounded account brief before message drafting.",
                "overview": "Collect evidence, identify the account reason, then prepare the handoff.",
                "format": "prompt_library",
                "sections": [
                    {
                        "heading": "Account evidence prompts",
                        "prompts": [
                            {
                                "name": "Account change scan",
                                "purpose": "Find a concrete reason to prioritize the account.",
                                "required_inputs": ["company", "recent changes"],
                                "prompt": "Summarize the observed account change without inventing a buying claim.",
                                "usage_steps": ["Add source evidence", "Review unsupported claims"],
                                "expected_output": "A sourced account-change summary.",
                            }
                        ],
                    }
                ],
                "implementation_checklist": ["Collect source evidence", "Review the account reason"],
                "leadpoet_workflow": {
                    "heading": "Prioritization handoff",
                    "context_basis": "Saved account evidence",
                    "steps": ["Confirm that the account reason is supported."],
                },
                "source_notes": [
                    {
                        "source_id": source.id,
                        "observation": "The source packages account research as reusable prompts.",
                        "adaptation": "Use the repository format, not the source wording.",
                    }
                ],
                "cta": "Use the first prompt on one account.",
            }
        )

        result = generate_lead_magnet(
            self.store,
            provider,
            audience="SDRs and sales founders",
            sales_outcome="Create a grounded account brief before outreach",
            format="prompt_library",
            depth="deep",
            product_integration="embedded",
            source_ids=[source.id],
        )

        self.assertEqual(result.item.format, "prompt_library")
        self.assertEqual(result.item.status, "needs_review")
        self.assertEqual(result.item.source_ids, [source.id])
        self.assertEqual(result.version.content, result.item.content)
        self.assertEqual(self.store.latest_lead_magnet_version(result.item.id).id, result.version.id)
        workflow_steps = result.item.content["leadpoet_workflow"]["steps"]
        self.assertTrue(any("Leadpoet" in step for step in workflow_steps))
        self.assertIn("## Prioritization handoff", result.item.markdown)
        self.assertIn("### Account change scan", result.item.markdown)
        self.assertIn("Saved Leadpoet product facts", provider.prompts[0])
        self.assertIn(source.id, provider.prompts[0])
        self.assertEqual(result.warnings, [])

    def test_generation_supports_every_dynamic_artifact_format(self):
        format_sections = {
            "prompt_library": {
                "heading": "Prompts",
                "prompts": [
                    {
                        "name": "Research prompt",
                        "purpose": "Research an account",
                        "required_inputs": ["account"],
                        "prompt": "Research this account using only supplied sources.",
                        "usage_steps": ["Add sources", "Verify"],
                        "expected_output": "A sourced brief",
                    }
                ],
            },
            "playbook": {
                "heading": "Playbook",
                "steps": ["Collect account evidence", "Choose the observable reason", "Review the message"],
            },
            "template_pack": {
                "heading": "Templates",
                "templates": [
                    {"name": "Account brief", "template": "Account: [name]\nObservable reason: [source-backed reason]"}
                ],
            },
            "scorecard": {
                "heading": "Scorecard",
                "questions": [
                    {"name": "Timing evidence", "text": "Is there a recent, sourced timing signal?", "score": "0-2"}
                ],
            },
        }
        for format_name, section in format_sections.items():
            with self.subTest(format=format_name):
                response = {
                    "title": f"Sales {format_name}",
                    "promise": "Turn account evidence into an actionable sales workflow.",
                    "audience": "sales operators",
                    "expected_outcome": "A verified account action plan.",
                    "overview": "Follow the sections in order and verify every claim.",
                    "format": format_name,
                    "sections": [section],
                    "implementation_checklist": ["Collect sources", "Verify claims", "Run the workflow"],
                    "leadpoet_workflow": {
                        "heading": "Leadpoet handoff",
                        "context_basis": "Product review",
                        "steps": ["Review the verified account context before the Leadpoet handoff."],
                    },
                    "source_notes": [],
                    "cta": "Apply this to one account.",
                }
                result = generate_lead_magnet(
                    self.store,
                    StaticLeadMagnetProvider(response),
                    audience="sales operators",
                    sales_outcome="create a verified account action plan",
                    format=format_name,
                    source_ids=[],
                )
                self.assertEqual(result.item.format, format_name)
                self.assertEqual(result.item.status, "needs_review")
                self.assertIn("Leadpoet", result.item.markdown)
                self.assertIn(str(response["title"]), result.item.markdown)


if __name__ == "__main__":
    unittest.main()
