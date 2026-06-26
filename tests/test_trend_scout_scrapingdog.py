from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leadpoet_social_brain.core.models import TrendItem
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.trend_scout import WebTrendGroups, refresh_web_trends
from leadpoet_social_brain.web.app import handle_api_request


class ScrapingdogTrendScoutTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> SQLiteStore:
        return SQLiteStore(Path(tmpdir) / "brain.sqlite3")

    def test_scrapingdog_trends_fill_daily_and_weekly_with_relevant_news(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_trending_now(*, api_key: str, hours: int, limit: int, geo: str = "US", language: str = "en"):
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

            def fake_google_news(*, api_key: str, tbs: str, limit: int, country: str = "us", language: str = "en"):
                self.assertEqual(api_key, "dog-key")
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
                        with patch("leadpoet_social_brain.trend_scout.fetch_gdelt_articles", return_value=[]):
                            groups = refresh_web_trends(store)

            self.assertEqual(len(groups.daily), 5)
            self.assertEqual(len(groups.weekly), 5)
            self.assertEqual(groups.errors, [])
            self.assertIn("Scrapingdog Google Trends", groups.sources)
            self.assertIn("Scrapingdog Google News", groups.sources)
            self.assertFalse(any("Celebrity gossip" in trend.title for trend in groups.daily))
            store.close()

    def test_scrapingdog_refresh_keeps_gdelt_fallback_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)

            def fake_gdelt(*, timespan: str, limit: int):
                return [
                    {
                        "title": f"{timespan} AI sales trend {index}",
                        "url": f"https://example.com/{timespan}/{index}",
                        "seendate": "20260626120000",
                        "sourceCommonName": "Example News",
                    }
                    for index in range(7)
                ]

            with patch.dict(os.environ, {"SCRAPINGDOG_API_KEY": "dog-key"}, clear=False):
                with patch(
                    "leadpoet_social_brain.trend_scout.fetch_scrapingdog_trending_now",
                    side_effect=RuntimeError("trends unavailable"),
                ):
                    with patch(
                        "leadpoet_social_brain.trend_scout.fetch_scrapingdog_google_news",
                        side_effect=RuntimeError("news unavailable"),
                    ):
                        with patch("leadpoet_social_brain.trend_scout.fetch_gdelt_articles", side_effect=fake_gdelt):
                            groups = refresh_web_trends(store)

            self.assertEqual(len(groups.daily), 5)
            self.assertEqual(len(groups.weekly), 5)
            self.assertTrue(any("Scrapingdog Google Trends failed" in error for error in groups.errors))
            self.assertTrue(any("Scrapingdog Google News failed" in error for error in groups.errors))
            self.assertIn("GDELT", groups.sources)
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
                sources=["Scrapingdog Google Trends", "GDELT"],
            )

            with patch("leadpoet_social_brain.web.app.refresh_web_trends", return_value=groups):
                status, payload = handle_api_request("POST", "/api/trends/refresh", b"{}", db_path=db_path)

            self.assertEqual(status, 200)
            self.assertEqual(payload["trend_groups"]["daily"][0]["title"], "Daily AI sales signal")
            self.assertEqual(payload["errors"], groups.errors)
            self.assertEqual(payload["sources"], groups.sources)


if __name__ == "__main__":
    unittest.main()
