from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from leadpoet_social_brain.cli import main as cli_main
from leadpoet_social_brain.content_queue import approve_queue_item, post_queue_item_x
from leadpoet_social_brain.core.models import ContentQueueItem
from leadpoet_social_brain.platforms.x_api import XPostedTweet
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.web.app import update_queue_item


APPROVED_AT = "2026-07-10T12:00:00+00:00"


class RecordingXClient:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_tweet(self, text: str) -> XPostedTweet:
        self.posts.append(text)
        return XPostedTweet(
            id=f"posted-{len(self.posts)}",
            text=text,
            url=f"https://x.com/i/web/status/posted-{len(self.posts)}",
        )


class QueueFeedbackSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "brain.sqlite3"
        self.store = SQLiteStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def save_item(self, *, status: str = "approved") -> ContentQueueItem:
        return self.store.save_content_queue_item(
            ContentQueueItem(
                text="The reviewed post.",
                topic="sales LLMs",
                post_type="founder_insight",
                angle="Buyer context should determine account priority.",
                scheduled_at="2026-07-11T10:00:00-04:00",
                status=status,
                approved_at=APPROVED_AT if status == "approved" else None,
            )
        )

    def run_cli(self, *args: str) -> None:
        self.store.close()
        with redirect_stdout(io.StringIO()):
            cli_main(["--db", str(self.db_path), *args])
        self.store = SQLiteStore(self.db_path)

    def test_web_text_edit_reopens_review_even_when_payload_asks_for_approval(self) -> None:
        item = self.save_item()

        updated = update_queue_item(
            self.store,
            item.id,
            {"text": "The materially edited post.", "status": "approved"},
        )

        self.assertEqual(updated.status, "needs_review")
        self.assertIsNone(updated.approved_at)
        versions = self.store.list_queued_post_versions(item.id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].text, "The reviewed post.")
        self.assertEqual(versions[0].edit_type, "manual")

    def test_web_each_material_copy_field_reopens_review(self) -> None:
        changes = [
            {"text": "Changed copy."},
            {"topic": "sales operations"},
            {"post_type": "technical_explanation"},
            {"angle": "A different thesis."},
        ]
        for payload in changes:
            with self.subTest(payload=payload):
                item = self.save_item()
                updated = update_queue_item(
                    self.store,
                    item.id,
                    {**payload, "status": "approved"},
                )
                self.assertEqual(updated.status, "needs_review")
                self.assertIsNone(updated.approved_at)

    def test_material_edit_cannot_approve_an_unreviewed_item_in_same_request(self) -> None:
        item = self.save_item(status="needs_review")

        updated = update_queue_item(
            self.store,
            item.id,
            {"text": "New unreviewed copy.", "status": "approved"},
        )

        self.assertEqual(updated.status, "needs_review")
        self.assertIsNone(updated.approved_at)

    def test_web_schedule_only_and_noop_edits_preserve_approval(self) -> None:
        item = self.save_item()

        rescheduled = update_queue_item(
            self.store,
            item.id,
            {"scheduled_at": "2026-07-12T10:00:00-04:00"},
        )
        unchanged = update_queue_item(self.store, item.id, {})

        self.assertEqual(rescheduled.status, "approved")
        self.assertEqual(rescheduled.approved_at, APPROVED_AT)
        self.assertEqual(unchanged.status, "approved")
        self.assertEqual(unchanged.approved_at, APPROVED_AT)
        self.assertEqual(self.store.list_queued_post_versions(item.id), [])

    def test_web_rejects_material_edits_to_posting_and_posted_items(self) -> None:
        for status in ["posting", "posted"]:
            with self.subTest(status=status):
                item = self.save_item(status=status)
                with self.assertRaisesRegex(ValueError, "cannot be materially edited"):
                    update_queue_item(self.store, item.id, {"text": "Too late to change."})
                saved = self.store.get_content_queue_item(item.id)
                self.assertEqual(saved.text, "The reviewed post.")
                self.assertEqual(saved.status, status)
                self.assertEqual(self.store.list_queued_post_versions(item.id), [])

    def test_cli_text_edit_archives_prior_text_and_reopens_review(self) -> None:
        item = self.save_item()

        self.run_cli(
            "update-queued-post",
            "--queue-id",
            item.id,
            "--text",
            "The CLI-edited post.",
            "--status",
            "approved",
        )

        updated = self.store.get_content_queue_item(item.id)
        self.assertEqual(updated.status, "needs_review")
        self.assertIsNone(updated.approved_at)
        versions = self.store.list_queued_post_versions(item.id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].text, "The reviewed post.")
        self.assertEqual(versions[0].edit_type, "manual")

    def test_cli_material_metadata_edit_reopens_review(self) -> None:
        item = self.save_item()

        self.run_cli(
            "update-queued-post",
            "--queue-id",
            item.id,
            "--topic",
            "sales operations",
            "--status",
            "approved",
        )

        updated = self.store.get_content_queue_item(item.id)
        self.assertEqual(updated.topic, "sales operations")
        self.assertEqual(updated.status, "needs_review")
        self.assertIsNone(updated.approved_at)

    def test_cli_schedule_only_edit_preserves_approval(self) -> None:
        item = self.save_item()

        self.run_cli(
            "update-queued-post",
            "--queue-id",
            item.id,
            "--scheduled-at",
            "2026-07-12T10:00:00-04:00",
        )

        updated = self.store.get_content_queue_item(item.id)
        self.assertEqual(updated.status, "approved")
        self.assertEqual(updated.approved_at, APPROVED_AT)
        self.assertEqual(self.store.list_queued_post_versions(item.id), [])

    def test_cli_rejects_material_edit_to_posted_item(self) -> None:
        item = self.save_item(status="posted")
        self.store.close()

        with self.assertRaisesRegex(SystemExit, "cannot be materially edited"):
            cli_main(
                [
                    "--db",
                    str(self.db_path),
                    "update-queued-post",
                    "--queue-id",
                    item.id,
                    "--text",
                    "Too late to change.",
                ]
            )

        self.store = SQLiteStore(self.db_path)
        saved = self.store.get_content_queue_item(item.id)
        self.assertEqual(saved.text, "The reviewed post.")
        self.assertEqual(saved.status, "posted")
        self.assertEqual(self.store.list_queued_post_versions(item.id), [])

    def test_two_distinct_identical_queue_items_cannot_both_publish(self) -> None:
        first = self.save_item()
        second = self.save_item()
        client = RecordingXClient()

        first_result = post_queue_item_x(self.store, client, item=first)
        with self.assertRaisesRegex(ValueError, "duplicates copy that is already published"):
            post_queue_item_x(self.store, client, item=second)

        self.assertIsNotNone(first_result.posted)
        self.assertEqual(client.posts, ["The reviewed post."])
        self.assertEqual(self.store.get_content_queue_item(first.id).status, "posted")
        self.assertEqual(self.store.get_content_queue_item(second.id).status, "approved")

    def test_posted_or_inflight_item_cannot_be_reapproved(self) -> None:
        for status in ["posting", "posted"]:
            with self.subTest(status=status):
                item = self.save_item(status=status)
                with self.assertRaisesRegex(ValueError, "cannot be approved again"):
                    approve_queue_item(self.store, item.id)


if __name__ == "__main__":
    unittest.main()
