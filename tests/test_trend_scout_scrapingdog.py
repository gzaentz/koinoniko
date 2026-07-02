from __future__ import annotations

import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from leadpoet_social_brain.core.models import ContextBundle, DraftRequest, TrendItem
from leadpoet_social_brain.generation import build_generation_prompt
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.trend_scout import (
    MISSING_SCRAPINGDOG_TRENDS_MESSAGE,
    WebTrendGroups,
    TrendScoutProfile,
    extract_source_page_details,
    normalize_article_date,
    positioned_leadpoet_angle,
    refresh_web_trends,
    select_web_trend_articles,
    web_trend_relevance_score,
)
from leadpoet_social_brain.web.app import handle_api_request


class FakeDigestProvider:
    def __init__(self, response: dict[str, object] | None = None, error: Exception | None = None):
        self.response = response or {
            "what_happened": "Acme launched buyer-timing AI for outbound teams.",
            "what_is_new": "The launch connects intent data to sales workflow timing.",
            "why_it_matters": "Sales teams can prioritize accounts based on buying windows instead of email volume.",
            "why_it_matters_to_gtm": "GTM teams need better context before outreach, not another generic sequence.",
            "leadpoet_angle": "Leadpoet should frame this as proof that timing beats volume in sales AI.",
            "suggested_hooks": ["Sales AI is becoming a timing layer.", "The inbox is not the bottleneck."],
            "possible_post_angles": ["Buyer timing beats generated email volume."],
            "key_points": ["Acme launched the product.", "Timing data is now in the workflow."],
            "important_claims": ["Acme says teams saw 32% more qualified meetings."],
            "source_confidence": "high",
            "caveats": "Vendor-reported metric.",
        }
        self.error = error
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return [json.dumps(self.response)]


class ScrapingdogTrendScoutTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> SQLiteStore:
        return SQLiteStore(Path(tmpdir) / "brain.sqlite3")

    def test_relative_news_dates_are_normalized(self):
        parsed = datetime.fromisoformat(normalize_article_date("18 hours ago"))
        age = datetime.now(timezone.utc) - parsed

        self.assertLess(abs(age - timedelta(hours=18)), timedelta(minutes=1))

    def test_positioned_angle_rewrites_other_tool_leverage_framing(self):
        trend = TrendItem(
            title="Close launches Chloe",
            source_type="web_weekly",
            topic="sales AI",
            summary="Close launched an AI sales agent.",
            why_it_matters="Sales teams are embedding AI inside CRM workflows.",
            leadpoet_angle="Tie this back to buyer timing.",
        )

        angle = positioned_leadpoet_angle(
            "Leadpoet can leverage insights from advances like Chloe.",
            trend=trend,
            profile=TrendScoutProfile(),
        )

        self.assertIn("Leadpoet's angle", angle)
        self.assertIn("buyer timing", angle)
        self.assertNotIn("leverage insights from", angle)

    def test_relevance_gate_requires_gtm_specificity(self):
        articles = [
            {
                "title": "OpenAI launches faster consumer photo model",
                "summary": "A generic AI update for consumer image creation.",
                "url": "https://example.com/generic-ai",
            },
            {
                "title": "California budget deal mentions AI procurement",
                "summary": "Governor and senate leaders reached a budget deal.",
                "url": "https://example.com/budget",
            },
            {
                "title": "AI SDR platforms add buyer intent signals",
                "summary": "Outbound teams are using account signals to prioritize pipeline.",
                "url": "https://example.com/ai-sdr",
            },
            {
                "title": "CRM teams invest in data enrichment for lead generation",
                "summary": "RevOps teams need better account context before outreach.",
                "url": "https://example.com/crm-enrichment",
            },
            {
                "title": "2 Artificial Intelligence (AI) Stocks to Buy as Demand for Custom Chips Soars",
                "summary": "Chip sales may rise as investors look for stocks to buy in the AI market.",
                "sourceCommonName": "The Motley Fool",
                "url": "https://example.com/ai-stocks",
            },
            {
                "title": "Jim Cramer Discusses Big Salesforce (CRM) Deal",
                "summary": "Salesforce, Inc. (NYSE:CRM) is one of the stocks Cramer discussed.",
                "sourceCommonName": "Insider Monkey",
                "url": "https://example.com/cramer-salesforce-stock",
            },
            {
                "title": "Build an agentic AI healthcare claims pipeline with Amazon Bedrock",
                "summary": "Healthcare teams use Amazon Bedrock to automate claims processing.",
                "url": "https://example.com/healthcare-claims-pipeline",
            },
            {
                "title": "Top 10 Best AI Sales Agents In The World 2026",
                "summary": "A ranked list of vendors and tools.",
                "url": "https://example.com/top-ai-sales-tools",
            },
            {
                "title": "World Cup, Wanderlust and a Wait-and-See World: What's Shaping Global Summer Travel in 2026",
                "summary": "US outbound flight bookings are up while hotel searches are down.",
                "url": "https://example.com/travel",
            },
        ]

        selected = select_web_trend_articles(articles, limit=5)

        self.assertEqual(
            [article["title"] for article in selected],
            [
                "AI SDR platforms add buyer intent signals",
                "CRM teams invest in data enrichment for lead generation",
            ],
        )
        self.assertEqual(web_trend_relevance_score(articles[0]), 0)
        self.assertEqual(web_trend_relevance_score(articles[1]), 0)
        self.assertEqual(web_trend_relevance_score(articles[4]), 0)
        self.assertEqual(web_trend_relevance_score(articles[5]), 0)
        self.assertEqual(web_trend_relevance_score(articles[6]), 0)
        self.assertEqual(web_trend_relevance_score(articles[7]), 0)
        self.assertEqual(web_trend_relevance_score(articles[8]), 0)
        self.assertGreaterEqual(web_trend_relevance_score(articles[2]), 5)

    def test_source_parser_ignores_navigation_list_items(self):
        page = extract_source_page_details(
            """
            <html>
              <body>
                <nav>
                  <ul>
                    <li>How to Invest</li>
                    <li>Stock Market 101</li>
                    <li>Best Brokers for Beginners</li>
                  </ul>
                </nav>
                <article>
                  <h1>AI SDR platforms add buyer intent signals</h1>
                  <p>Sales teams are using account signals to prioritize outreach around buying windows.</p>
                </article>
              </body>
            </html>
            """
        )

        self.assertNotIn("How to Invest", page.text_excerpt or "")
        self.assertIn("buyer intent signals", page.text_excerpt or "")

    def test_investing_article_is_rejected_after_source_enrichment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": "2 Artificial Intelligence (AI) Stocks to Buy as Demand for Custom Chips Soars",
                        "url": f"https://example.com/{timespan}/ai-stocks",
                        "sourceCommonName": "The Motley Fool",
                        "summary": "Chip sales are rising as investors look for artificial intelligence stocks to buy.",
                    }
                ]

            def fake_source_page(url: str) -> str:
                return """
                <html>
                  <head><title>2 Artificial Intelligence Stocks to Buy</title></head>
                  <body>
                    <nav><li>How to Invest</li><li>Stock Market 101</li><li>Best Brokers for Beginners</li></nav>
                    <p>Broadcom could be a winner from rising demand for custom AI chips.</p>
                    <p>The article compares stock valuation, market demand, and chip revenue.</p>
                  </body>
                </html>
                """

            groups = refresh_web_trends(
                store,
                fetch_articles=fake_fetch,
                limit=1,
                enrich=True,
                fetch_source_page=fake_source_page,
            )

            self.assertEqual(groups.daily, [])
            self.assertEqual(groups.weekly, [])
            self.assertEqual(groups.trends_stored, 0)
            self.assertEqual(store.list_trends(status="unused"), [])
            store.close()

    def test_scrapingdog_trends_fill_daily_and_weekly_with_relevant_news(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_trending_now(
                *,
                api_key: str,
                hours: int,
                limit: int,
                geo: str = "US",
                language: str = "en",
                timeout_seconds: int = 45,
            ):
                self.assertEqual(api_key, "dog-key")
                return [
                    {
                        "title": f"AI SDR platforms {hours}",
                        "start_timestamp": 1782470400,
                        "search_volume": 5000,
                        "increase_percentage": 200,
                        "trend_breakdown": ["sales automation", "lead generation"],
                    },
                    {"title": f"Celebrity gossip {hours}", "search_volume": 900000},
                ]

            def fake_google_news(
                *,
                api_key: str,
                tbs: str,
                limit: int,
                country: str = "us",
                language: str = "en",
                query: str | None = None,
                timeout_seconds: int = 45,
            ):
                self.assertEqual(api_key, "dog-key")
                self.assertIn("sales AI", query or "")
                slug = tbs.replace(":", "-")
                return [
                    {
                        "title": f"Generative AI sales workflow {slug} {index}",
                        "link": f"https://example.com/{slug}/{index}",
                        "source": "Example News",
                        "snippet": "Lead generation and sales automation teams are changing pipeline work.",
                    }
                    for index in range(5)
                ]

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
                with patch(
                    "leadpoet_social_brain.trend_scout.fetch_scrapingdog_trending_now",
                    side_effect=fake_trending_now,
                ):
                    with patch(
                        "leadpoet_social_brain.trend_scout.fetch_scrapingdog_google_news",
                        side_effect=fake_google_news,
                    ):
                        groups = refresh_web_trends(store)

            self.assertEqual(len(groups.daily), 5)
            self.assertEqual(len(groups.weekly), 5)
            self.assertEqual(groups.errors, [])
            self.assertIn("Scrapingdog Google Trends", groups.sources)
            self.assertIn("Scrapingdog Google News", groups.sources)
            self.assertFalse(any("Celebrity gossip" in trend.title for trend in groups.daily))
            store.close()

    def test_scrapingdog_refresh_bounds_news_queries_for_hosted_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            news_calls: list[tuple[str, str | None, int]] = []

            def fake_trending_now(
                *,
                api_key: str,
                hours: int,
                limit: int,
                geo: str = "US",
                language: str = "en",
                timeout_seconds: int = 45,
            ):
                self.assertEqual(timeout_seconds, 7)
                return [{"title": f"Celebrity gossip {hours}", "search_volume": 900000}]

            def fake_google_news(
                *,
                api_key: str,
                tbs: str,
                limit: int,
                country: str = "us",
                language: str = "en",
                query: str | None = None,
                timeout_seconds: int = 45,
            ):
                news_calls.append((tbs, query, timeout_seconds))
                return [
                    {
                        "title": f"Celebrity gossip {tbs}",
                        "link": f"https://example.com/{tbs}/celebrity",
                        "source": "Example News",
                        "snippet": "Entertainment update without GTM or sales relevance.",
                    }
                ]

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
                with patch(
                    "leadpoet_social_brain.trend_scout.fetch_scrapingdog_trending_now",
                    side_effect=fake_trending_now,
                ):
                    with patch(
                        "leadpoet_social_brain.trend_scout.fetch_scrapingdog_google_news",
                        side_effect=fake_google_news,
                    ):
                        groups = refresh_web_trends(
                            store,
                            limit=1,
                            enrich=False,
                            scrapingdog_timeout_seconds=7,
                            max_news_queries_per_window=1,
                            deadline_seconds=90,
                        )

            self.assertEqual(groups.daily, [])
            self.assertEqual(groups.weekly, [])
            self.assertEqual(len(news_calls), 4)
            self.assertTrue(all(call[2] == 7 for call in news_calls))
            self.assertEqual([call[0] for call in news_calls], ["qdr:d", "qdr:w", "qdr:w", "qdr:m"])
            store.close()

    def test_scrapingdog_missing_key_reports_clear_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            with patch.dict(os.environ, {}, clear=True):
                groups = refresh_web_trends(store)

            self.assertEqual(groups.daily, [])
            self.assertEqual(groups.weekly, [])
            self.assertEqual(groups.errors, [MISSING_SCRAPINGDOG_TRENDS_MESSAGE])
            self.assertEqual(groups.trends_stored, 0)
            store.close()

    def test_scrapingdog_refresh_has_no_third_party_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
                with patch(
                    "leadpoet_social_brain.trend_scout.fetch_scrapingdog_trending_now",
                    side_effect=RuntimeError("trends unavailable"),
                ):
                    with patch(
                        "leadpoet_social_brain.trend_scout.fetch_scrapingdog_google_news",
                        side_effect=RuntimeError("news unavailable"),
                    ):
                        groups = refresh_web_trends(store)

            self.assertEqual(groups.daily, [])
            self.assertEqual(groups.weekly, [])
            self.assertTrue(any("Scrapingdog Google Trends failed" in error for error in groups.errors))
            self.assertTrue(any("Scrapingdog Google News failed" in error for error in groups.errors))
            self.assertEqual(groups.sources, [])
            store.close()

    def test_web_trends_refresh_endpoint_returns_warnings_and_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            SQLiteStore(db_path).close()
            daily = TrendItem(
                title="Daily AI sales signal",
                source_type="web_daily",
                topic="sales AI",
                summary="Daily signal.",
                why_it_matters="It may affect sales workflows.",
                leadpoet_angle="Connect it to buyer timing.",
            )
            groups = WebTrendGroups(
                daily=[daily],
                weekly=[],
                errors=["weekly Scrapingdog Google Trends failed: HTTP 429 Too Many Requests"],
                sources=["Scrapingdog Google Trends"],
            )

            with patch("leadpoet_social_brain.web.app.refresh_web_trends", return_value=groups):
                status, payload = handle_api_request("POST", "/api/trends/refresh", b"{}", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(payload["trend_groups"]["daily"][0]["title"], "Daily AI sales signal")
            self.assertEqual(payload["errors"], groups.errors)
            self.assertEqual(payload["sources"], groups.sources)

    def test_trend_refresh_stores_source_metadata_and_enriched_digest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeDigestProvider()

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"Acme buyer timing AI {timespan}",
                        "url": f"https://example.com/{timespan}/buyer-timing",
                        "sourceCommonName": "Example News",
                        "seendate": "20260626120000",
                        "summary": "Acme says sales teams are using AI to find buyer timing windows.",
                    }
                ]

            def fake_source_page(url: str) -> str:
                return """
                <html>
                  <head>
                    <title>Acme launches buyer timing AI</title>
                    <meta name="author" content="Jane Reporter">
                    <meta property="article:published_time" content="2026-06-26T12:00:00Z">
                  </head>
                  <body>
                    <p>Acme launched a sales AI product that watches buyer signals before reps send outbound emails.</p>
                    <p>The company says pilot customers booked 32% more qualified meetings after routing outreach around timing.</p>
                  </body>
                </html>
                """

            groups = refresh_web_trends(
                store,
                fetch_articles=fake_fetch,
                limit=1,
                provider=provider,
                fetch_source_page=fake_source_page,
            )

            self.assertEqual(groups.candidates_found, 2)
            self.assertEqual(groups.trends_stored, 2)
            self.assertEqual(groups.enriched, 2)
            self.assertEqual(groups.failed, 0)
            trend = groups.daily[0]
            self.assertEqual(trend.enrichment_status, "enriched")
            self.assertEqual(trend.source_title, "Acme launches buyer timing AI")
            self.assertEqual(trend.source_author, "Jane Reporter")
            self.assertEqual(trend.source_published_at, "2026-06-26T12:00:00+00:00")
            self.assertIn("buyer-timing AI", trend.summary)
            self.assertIn("timing beats volume", trend.leadpoet_angle)
            self.assertIn("32% more qualified meetings", trend.important_claims_json or "")
            self.assertEqual(trend.source_confidence, "high")
            self.assertIn("Return compact JSON", provider.prompts[0])
            self.assertIn("Keep only if it directly affects B2B GTM", provider.prompts[0])
            self.assertIn("Do not invent a Leadpoet angle", provider.prompts[0])
            stored = store.get_trend_item(trend.id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.enrichment_status, "enriched")
            store.close()

    def test_provider_relevance_rejection_prevents_storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            provider = FakeDigestProvider(
                response={
                    "keep": False,
                    "relevance_score": 3,
                    "relevance_reason": "Generic AI vendor update without GTM workflow impact.",
                    "reject_reason": "Leadpoet angle would be forced.",
                    "what_happened": "",
                    "what_is_new": "",
                    "why_it_matters": "",
                    "why_it_matters_to_gtm": "",
                    "leadpoet_angle": "",
                    "suggested_hooks": [],
                    "possible_post_angles": [],
                    "key_points": [],
                    "important_claims": [],
                    "source_confidence": "low",
                    "caveats": "",
                }
            )

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"Sales AI vendor launches generic assistant {timespan}",
                        "url": f"https://example.com/{timespan}/sales-ai",
                        "sourceCommonName": "Example News",
                        "summary": "Sales teams are mentioned, but the update has no buyer timing or workflow impact.",
                    }
                ]

            groups = refresh_web_trends(
                store,
                fetch_articles=fake_fetch,
                limit=1,
                provider=provider,
                fetch_source_page=lambda url: "<html><body><p>Sales AI assistant launched with generic features.</p></body></html>",
            )

            self.assertEqual(groups.daily, [])
            self.assertEqual(groups.weekly, [])
            self.assertEqual(groups.trends_stored, 0)
            self.assertEqual(store.list_trends(status="unused"), [])
            self.assertIn("relevance_score", provider.prompts[0])
            store.close()

    def test_trend_enrichment_failure_does_not_fail_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_fetch(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"AI sales source {timespan}",
                        "url": f"https://example.com/{timespan}/broken",
                        "sourceCommonName": "Example News",
                        "summary": "AI sales and lead generation teams are changing workflow timing.",
                    }
                ]

            groups = refresh_web_trends(
                store,
                fetch_articles=fake_fetch,
                limit=1,
                provider=FakeDigestProvider(error=RuntimeError("model unavailable")),
                fetch_source_page=lambda url: (_ for _ in ()).throw(RuntimeError("page unavailable")),
            )

            self.assertEqual(len(groups.daily), 1)
            self.assertEqual(groups.failed, 2)
            self.assertEqual(groups.daily[0].enrichment_status, "failed")
            self.assertIn("source fetch failed", groups.daily[0].enrichment_error or "")
            store.close()

    def test_generation_prompt_uses_enriched_digest_not_raw_article(self):
        raw_article = "COPY THIS RAW ARTICLE LANGUAGE " * 100
        trend = TrendItem(
            title="Buyer timing AI launches",
            source_type="web_daily",
            topic="sales AI",
            summary="Acme launched buyer-timing AI for outbound teams.",
            why_it_matters="Sales teams can prioritize accounts around buying windows.",
            leadpoet_angle="Leadpoet should explain why timing beats generated email volume.",
            suggested_hooks="Sales AI is becoming a timing layer.\nThe inbox is not the bottleneck.",
            source_url="https://example.com/source",
            source_author="Example News",
            source_text_excerpt=raw_article,
            important_claims_json=json.dumps(["The new part is workflow-level buyer timing."]),
            post_angles_json=json.dumps(["Buyer timing beats generated email volume."]),
            source_confidence="high",
            enrichment_status="enriched",
        )
        prompt = build_generation_prompt(
            DraftRequest(platform="x", topic="sales AI", post_type="founder_insight", use_trends=True),
            ContextBundle(
                platform="x",
                topic="sales AI",
                post_type="founder_insight",
                style_sections=[],
                positioning_sections=[],
                patterns=[],
                examples=[],
                notes_by_post_id={},
                metrics_by_post_id={},
                analytics_hints=[],
            ),
            skills=[],
            trends=[trend],
            patterns=[],
            max_chars=280,
        )

        self.assertIn("What happened:\nAcme launched buyer-timing AI", prompt)
        self.assertIn("What is new:\nThe new part is workflow-level buyer timing.", prompt)
        self.assertIn("Saved angle:\nUse Buyer timing AI launches as market proof", prompt)
        self.assertNotIn("Leadpoet should explain", prompt)
        self.assertIn("Suggested hooks:", prompt)
        self.assertNotIn("COPY THIS RAW ARTICLE LANGUAGE", prompt)

    def test_trend_detail_payload_returns_enrichment_fields_without_raw_text_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            store = SQLiteStore(db_path)
            trend = store.save_trend_item(
                TrendItem(
                    title="AI sales timing",
                    source_type="web_daily",
                    topic="sales AI",
                    summary="Acme changed sales timing workflows.",
                    why_it_matters="It changes pipeline prioritization.",
                    leadpoet_angle="Leadpoet should talk about buyer timing.",
                    raw_text="RAW PROVIDER BLOB " * 500,
                    source_text_excerpt="ARTICLE EXCERPT " * 500,
                    key_points_json=json.dumps(["Acme launched", "Timing matters"]),
                    important_claims_json=json.dumps(["32% more meetings"]),
                    post_angles_json=json.dumps(["Timing beats volume"]),
                    source_confidence="medium",
                    enrichment_status="partial",
                )
            )
            store.close()

            status, payload = handle_api_request("GET", f"/api/trends/{trend.id}", b"", db_path=db_path)

            self.assertEqual(status, 200)
            item = payload["item"]
            self.assertNotIn("raw_text", item)
            self.assertLessEqual(len(item["source_text_excerpt"]), 1200)
            self.assertEqual(item["key_points"], ["Acme launched", "Timing matters"])
            self.assertEqual(item["source_confidence"], "medium")
            self.assertEqual(item["enrichment_status"], "partial")


if __name__ == "__main__":
    unittest.main()
