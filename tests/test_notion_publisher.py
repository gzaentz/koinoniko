from __future__ import annotations

import unittest
from typing import Any

from leadpoet_social_brain.notion_publisher import (
    NOTION_APPEND_BLOCK_LIMIT,
    NotionAPIError,
    NotionClient,
    NotionConfig,
    NotionConfigurationError,
    NotionPublishError,
    NotionPublisher,
    batch_notion_blocks,
    structured_content_to_notion_blocks,
)


class FakeNotionTransport:
    def __init__(self, *, fail_on_append: int | None = None):
        self.fail_on_append = fail_on_append
        self.append_calls = 0
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers,
        json_body,
        timeout_seconds: float,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": json_body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if "/blocks/" in url and url.endswith("/children"):
            self.append_calls += 1
            if self.fail_on_append == self.append_calls:
                raise NotionAPIError(
                    "Injected append failure.",
                    status_code=503,
                    code="service_unavailable",
                )
            return {
                "object": "list",
                "results": [{"object": "block", "id": f"block-{self.append_calls}"}],
                "has_more": False,
                "next_cursor": None,
            }
        if method == "POST" and url.endswith("/pages"):
            return {
                "object": "page",
                "id": "notion-page-1",
                "url": "https://www.notion.so/notion-page-1",
                "public_url": "https://example.notion.site/notion-page-1",
            }
        if method == "PATCH" and "/pages/" in url:
            return {
                "object": "page",
                "id": url.rsplit("/", 1)[-1],
                "url": "https://www.notion.so/existing-page",
                "public_url": None,
            }
        raise AssertionError(f"Unexpected fake Notion request: {method} {url}")


def make_client(transport: FakeNotionTransport, *, parent_page_id: str | None = "parent-page") -> NotionClient:
    return NotionClient(
        NotionConfig(
            token="secret-token",
            parent_page_id=parent_page_id,
            api_base_url="https://api.notion.test/v1",
        ),
        transport=transport,
    )


def block_text(block: dict[str, Any]) -> str:
    payload = block.get(block.get("type") or "") or {}
    rich_text = payload.get("rich_text") or payload.get("caption") or []
    return "".join(str(item.get("text", {}).get("content") or "") for item in rich_text)


class NotionPublisherTests(unittest.TestCase):
    def test_maps_current_lead_magnet_content_schema_to_blocks(self):
        content = {
            "title": "The Sales Research Prompt Vault",
            "promise": "Turn raw account data into a useful sales brief.",
            "audience": "SDRs and sales operators",
            "expected_outcome": "A prioritized account brief with evidence.",
            "overview": "Work through the sections in order, then review every claim.",
            "format": "prompt_library",
            "sections": [
                {
                    "heading": "Research the account",
                    "body": "Start from first-party facts before forming a thesis.",
                    "prompts": [
                        {
                            "name": "Account research brief",
                            "purpose": "Find relevant account context.",
                            "required_inputs": ["Company domain", "Target persona"],
                            "prompt": "Research {company_domain} for {target_persona}.",
                            "usage_steps": ["Add the inputs", "Audit every claim"],
                            "expected_output": "A source-backed account brief.",
                        }
                    ],
                },
                {
                    "heading": "Copyable template",
                    "templates": [
                        {
                            "name": "Account handoff",
                            "template": "Account: {account_name}\nReason now: {signal}",
                            "usage_notes": "Replace every placeholder before sharing.",
                        }
                    ],
                },
                {
                    "heading": "Quality check",
                    "questions": [
                        {
                            "question": "Is the timing signal directly observed?",
                            "options": ["Yes", "No"],
                            "scoring_rule": "Yes = 1; No = 0.",
                        }
                    ],
                },
            ],
            "implementation_checklist": [
                "Choose ten accounts.",
                "Run the research prompt.",
            ],
            "leadpoet_workflow": {
                "heading": "Where Leadpoet fits",
                "context_basis": "Saved Leadpoet product facts",
                "steps": ["Use Leadpoet to prioritize the account handoff."],
            },
            "source_notes": [
                {
                    "source_id": "source-1",
                    "observation": "The source used a copyable prompt-vault format.",
                    "adaptation": "Used the format without copying its prompts.",
                }
            ],
            "cta": "Review the workflow in Leadpoet.",
        }

        blocks = structured_content_to_notion_blocks(content)
        block_types = [block["type"] for block in blocks]
        texts = [block_text(block) for block in blocks]

        self.assertNotIn(content["title"], texts)
        self.assertEqual(block_types.count("to_do"), 2)
        self.assertEqual(block_types.count("code"), 2)
        self.assertIn("Where Leadpoet fits", texts)
        self.assertIn("Research {company_domain} for {target_persona}.", texts)
        self.assertIn("Account: {account_name}\nReason now: {signal}", texts)
        self.assertIn("Is the timing signal directly observed?", texts)
        self.assertTrue(any(text.startswith("source-1 — Observed:") for text in texts))
        self.assertEqual(block_types[-1], "bulleted_list_item")
        self.assertIn("Review the workflow in Leadpoet.", texts)

    def test_batches_children_at_notion_append_limit(self):
        blocks = [
            {"object": "block", "type": "divider", "divider": {}}
            for _ in range((NOTION_APPEND_BLOCK_LIMIT * 2) + 5)
        ]

        batches = batch_notion_blocks(blocks)

        self.assertEqual([len(batch) for batch in batches], [100, 100, 5])

    def test_creates_page_then_appends_rendered_children(self):
        transport = FakeNotionTransport()
        publisher = NotionPublisher(make_client(transport))

        result = publisher.publish(
            {
                "title": "Sales playbook",
                "sections": [{"heading": "Start here", "body": "Choose an account."}],
            },
            icon="📘",
        )

        self.assertTrue(result.created)
        self.assertEqual(result.page_id, "notion-page-1")
        self.assertEqual(result.url, "https://www.notion.so/notion-page-1")
        self.assertEqual(result.public_url, "https://example.notion.site/notion-page-1")
        self.assertEqual(result.batch_count, 1)
        self.assertEqual(len(result.content_hash), 64)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PATCH"])
        create = transport.calls[0]
        self.assertEqual(
            create["json_body"]["parent"],
            {"type": "page_id", "page_id": "parent-page"},
        )
        self.assertEqual(
            create["json_body"]["properties"]["title"]["title"][0]["text"]["content"],
            "Sales playbook",
        )
        self.assertEqual(create["json_body"]["icon"], {"type": "emoji", "emoji": "📘"})
        append = transport.calls[1]
        self.assertTrue(append["url"].endswith("/blocks/notion-page-1/children"))
        self.assertEqual(len(append["json_body"]["children"]), result.block_count)

    def test_repeated_existing_page_publish_replaces_same_page_without_creating(self):
        transport = FakeNotionTransport()
        publisher = NotionPublisher(make_client(transport, parent_page_id=None))
        content = {
            "title": "Updated prompt vault",
            "blocks": [{"type": "paragraph", "text": "Replacement content."}],
        }

        first = publisher.publish(content, existing_page_id="existing-page")
        second = publisher.publish(content, existing_page_id="existing-page")

        self.assertFalse(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.page_id, "existing-page")
        self.assertEqual(second.page_id, "existing-page")
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotIn("POST", [call["method"] for call in transport.calls])
        self.assertEqual(len(transport.calls), 4)
        for index in (0, 2):
            replace = transport.calls[index]
            self.assertTrue(replace["url"].endswith("/pages/existing-page"))
            self.assertIs(replace["json_body"]["erase_content"], True)
        for index in (1, 3):
            self.assertTrue(transport.calls[index]["url"].endswith("/blocks/existing-page/children"))

    def test_missing_environment_configuration_is_clear(self):
        with self.assertRaisesRegex(NotionConfigurationError, "NOTION_API_TOKEN"):
            NotionConfig.from_env({})

        publisher = NotionPublisher.from_env(
            environ={"NOTION_API_TOKEN": "secret-token"},
            transport=FakeNotionTransport(),
        )
        with self.assertRaisesRegex(NotionConfigurationError, "NOTION_PARENT_PAGE_ID"):
            publisher.publish({"title": "Cannot create yet", "blocks": []})

    def test_partial_upload_failure_preserves_created_page_id_in_error(self):
        transport = FakeNotionTransport(fail_on_append=2)
        publisher = NotionPublisher(make_client(transport))
        blocks = [{"type": "divider"} for _ in range(NOTION_APPEND_BLOCK_LIMIT + 1)]

        with self.assertRaises(NotionPublishError) as raised:
            publisher.publish({"title": "Large lead magnet", "blocks": blocks})

        error = raised.exception
        self.assertEqual(error.page_id, "notion-page-1")
        self.assertTrue(error.created)
        self.assertIn("content upload did not finish", str(error))
        self.assertIsInstance(error.__cause__, NotionAPIError)
        self.assertEqual(transport.append_calls, 2)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PATCH", "PATCH"])
        self.assertEqual(len(transport.calls[1]["json_body"]["children"]), 100)
        self.assertEqual(len(transport.calls[2]["json_body"]["children"]), 1)


if __name__ == "__main__":
    unittest.main()
