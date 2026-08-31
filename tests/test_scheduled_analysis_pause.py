from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.web import app as web_app
from leadpoet_social_brain.web.app import handle_api_request


class ScheduledAnalysisPauseTest(unittest.TestCase):
    def request(self, db_path: Path, method: str, path: str, payload: dict[str, object] | None = None):
        return handle_api_request(
            method,
            path,
            json.dumps(payload or {}).encode("utf-8"),
            db_path=db_path,
        )

    def test_pause_setting_persists_through_account_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"

            status, paused = self.request(
                db_path,
                "PATCH",
                "/api/account",
                {"scheduled_analysis_paused": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(paused["settings"]["scheduled_analysis_paused"])

            store = SQLiteStore(db_path)
            self.assertEqual(store.get_app_setting("scheduled_analysis_paused"), "true")
            store.close()

            status, loaded = self.request(db_path, "GET", "/api/account")
            self.assertEqual(status, 200)
            self.assertTrue(loaded["settings"]["scheduled_analysis_paused"])

            status, resumed = self.request(
                db_path,
                "PATCH",
                "/api/account",
                {"scheduled_analysis_paused": False},
            )
            self.assertEqual(status, 200)
            self.assertFalse(resumed["settings"]["scheduled_analysis_paused"])

    def test_paused_scheduled_analysis_tasks_skip_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            self.request(db_path, "PATCH", "/api/account", {"scheduled_analysis_paused": True})
            task_names = sorted(web_app.PAUSABLE_SCHEDULED_TASKS)
            dispatch_names = {
                "refresh-trends": "refresh_trends_task_payload",
                "refresh-patterns": "refresh_patterns_task_payload",
                "refresh-patterns-weekly": "refresh_pattern_windows_task_payload",
                "refresh-topics": "refresh_topics_task_payload",
                "autopilot-weekly": "autopilot_weekly_task_payload",
                "autopilot-lead-magnet": "autopilot_lead_magnet_task_payload",
            }
            with ExitStack() as stack:
                mocks = {
                    name: stack.enter_context(patch.object(web_app, dispatch_name, Mock()))
                    for name, dispatch_name in dispatch_names.items()
                }
                for task in task_names:
                    status, payload = self.request(db_path, "POST", f"/tasks/{task}")
                    self.assertEqual(status, 200)
                    self.assertTrue(payload["skipped"])
                    self.assertTrue(payload["paused"])
                    self.assertEqual(payload["task"], task)
                    self.assertIn("No provider or network calls", payload["reason"])
                    self.assertNotIn("errors", payload)
                for mocked in mocks.values():
                    mocked.assert_not_called()

    def test_enabled_task_dispatches_and_manual_refresh_stays_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brain.sqlite3"
            self.request(db_path, "PATCH", "/api/account", {"scheduled_analysis_paused": False})
            scheduled_result = {"ran": True}
            with patch.object(web_app, "refresh_trends_task_payload", Mock(return_value=scheduled_result)) as scheduled:
                status, payload = self.request(db_path, "POST", "/tasks/refresh-trends")
            self.assertEqual(status, 200)
            self.assertEqual(payload, scheduled_result)
            scheduled.assert_called_once()

            self.request(db_path, "PATCH", "/api/account", {"scheduled_analysis_paused": True})
            manual_result = {"ran": True, "manual": True}
            with patch.object(web_app, "refresh_trends_task_payload", Mock(return_value=manual_result)) as manual:
                status, payload = self.request(db_path, "POST", "/api/trends/refresh")
            self.assertEqual(status, 200)
            self.assertEqual(payload, manual_result)
            manual.assert_called_once()

    def test_account_settings_exposes_inline_pause_control(self):
        static_dir = Path(__file__).parents[1] / "leadpoet_social_brain" / "web" / "static"
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="accountAutomationRow"', index_html)
        self.assertIn('id="accountScheduledAnalysisButton"', index_html)
        self.assertIn('id="accountScheduledAnalysisButton" type="button" disabled', index_html)
        self.assertIn('id="accountAutomationSummary"', index_html)
        self.assertIn("news, trends, patterns, topics, weekly plans, and lead-magnet research", index_html + app_js)
        self.assertIn("saveScheduledAnalysisSetting", app_js)
        self.assertNotIn('data-account-panel="automation"', index_html)


if __name__ == "__main__":
    unittest.main()
