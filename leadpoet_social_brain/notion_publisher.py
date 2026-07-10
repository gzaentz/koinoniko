from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


NOTION_API_TOKEN_ENV = "NOTION_API_TOKEN"
NOTION_PARENT_PAGE_ID_ENV = "NOTION_PARENT_PAGE_ID"
NOTION_API_BASE_URL = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"
NOTION_APPEND_BLOCK_LIMIT = 100
NOTION_RICH_TEXT_CONTENT_LIMIT = 2_000
NOTION_RICH_TEXT_ARRAY_LIMIT = 100
NOTION_REQUEST_SIZE_LIMIT_BYTES = 500_000

# Leave room for request envelope bytes and JSON escaping beneath Notion's 500 KB
# request limit.
DEFAULT_BATCH_PAYLOAD_LIMIT_BYTES = 450_000

JsonObject = dict[str, Any]


class NotionPublisherError(RuntimeError):
    """Base error for Notion publishing failures."""


class NotionConfigurationError(NotionPublisherError):
    """Raised when required Notion environment configuration is missing."""


class NotionContentError(NotionPublisherError):
    """Raised when structured content cannot be represented safely in Notion."""


class NotionAPIError(NotionPublisherError):
    """A structured error returned by the Notion API or HTTP transport."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        response: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.response = dict(response or {})


class NotionPublishError(NotionPublisherError):
    """Raised when a page exists but its content could not be fully published."""

    def __init__(self, message: str, *, page_id: str, created: bool):
        super().__init__(message)
        self.page_id = page_id
        self.created = created


class NotionJsonTransport(Protocol):
    """Injectable JSON transport used by :class:`NotionClient`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class NotionConfig:
    token: str
    parent_page_id: str | None = None
    api_base_url: str = NOTION_API_BASE_URL
    api_version: str = NOTION_API_VERSION
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_parent: bool = False,
        api_base_url: str = NOTION_API_BASE_URL,
        api_version: str = NOTION_API_VERSION,
        timeout_seconds: float = 30.0,
    ) -> "NotionConfig":
        values = environ if environ is not None else os.environ
        token = _clean(values.get(NOTION_API_TOKEN_ENV))
        parent_page_id = _clean(values.get(NOTION_PARENT_PAGE_ID_ENV))
        if not token:
            raise NotionConfigurationError(
                f"{NOTION_API_TOKEN_ENV} is required to publish to Notion."
            )
        if require_parent and not parent_page_id:
            raise NotionConfigurationError(
                f"{NOTION_PARENT_PAGE_ID_ENV} is required when creating a Notion page. "
                "Share that parent page with the Notion integration, then set its page ID."
            )
        return cls(
            token=token,
            parent_page_id=parent_page_id or None,
            api_base_url=api_base_url.rstrip("/"),
            api_version=api_version,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class NotionPublishResult:
    page_id: str
    url: str | None
    public_url: str | None
    created: bool
    block_count: int
    batch_count: int
    content_hash: str
    page: Mapping[str, Any]


class UrllibJsonTransport:
    """Production Notion transport implemented only with the standard library."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers=dict(headers),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return _decode_json_response(response.read(), status_code=response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _decode_error_payload(raw)
            code = _clean(payload.get("code")) or None
            detail = _clean(payload.get("message")) or str(exc.reason)
            raise NotionAPIError(
                _notion_api_error_message(exc.code, detail),
                status_code=exc.code,
                code=code,
                response=payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise NotionAPIError(
                f"Could not reach the Notion API: {exc.reason}",
                code="network_error",
            ) from exc
        except TimeoutError as exc:
            raise NotionAPIError(
                "The Notion API request timed out.",
                code="timeout",
            ) from exc


class NotionClient:
    """Small direct client for the page and block endpoints used by publishing."""

    def __init__(
        self,
        config: NotionConfig,
        *,
        transport: NotionJsonTransport | None = None,
    ):
        self.config = config
        self.transport = transport or UrllibJsonTransport()

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        transport: NotionJsonTransport | None = None,
        require_parent: bool = False,
        timeout_seconds: float = 30.0,
    ) -> "NotionClient":
        return cls(
            NotionConfig.from_env(
                environ,
                require_parent=require_parent,
                timeout_seconds=timeout_seconds,
            ),
            transport=transport,
        )

    def create_child_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        icon: str | Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        parent_id = _required_text(parent_page_id, "parent_page_id")
        payload: JsonObject = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {"title": {"title": notion_rich_text(title)}},
        }
        normalized_icon = _notion_icon(icon)
        if normalized_icon:
            payload["icon"] = normalized_icon
        return self._request("POST", "/pages", payload)

    def replace_page(
        self,
        *,
        page_id: str,
        title: str,
        icon: str | Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        clean_page_id = _required_text(page_id, "page_id")
        payload: JsonObject = {
            "properties": {"title": {"title": notion_rich_text(title)}},
            "erase_content": True,
        }
        normalized_icon = _notion_icon(icon)
        if normalized_icon:
            payload["icon"] = normalized_icon
        return self._request(
            "PATCH",
            f"/pages/{urllib.parse.quote(clean_page_id, safe='')}",
            payload,
        )

    def append_block_children(
        self,
        *,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        clean_block_id = _required_text(block_id, "block_id")
        child_list = [dict(child) for child in children]
        if not child_list:
            return {"object": "list", "results": [], "has_more": False, "next_cursor": None}
        if len(child_list) > NOTION_APPEND_BLOCK_LIMIT:
            raise NotionContentError(
                f"Notion accepts at most {NOTION_APPEND_BLOCK_LIMIT} child blocks per append request; "
                f"received {len(child_list)}. Use batch_notion_blocks first."
            )
        return self._request(
            "PATCH",
            f"/blocks/{urllib.parse.quote(clean_block_id, safe='')}/children",
            {"children": child_list},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        return self.transport.request(
            method,
            f"{self.config.api_base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "Notion-Version": self.config.api_version,
                "User-Agent": "leadpoet-social-brain/0.2",
            },
            json_body=payload,
            timeout_seconds=self.config.timeout_seconds,
        )


class NotionPublisher:
    """Create a Notion child page or replace one previously created by the app.

    ``existing_page_id`` must identify an app-owned page: replacement deliberately
    erases its current block children before uploading the rendered document.
    """

    def __init__(
        self,
        client: NotionClient,
        *,
        parent_page_id: str | None = None,
        batch_payload_limit_bytes: int = DEFAULT_BATCH_PAYLOAD_LIMIT_BYTES,
    ):
        self.client = client
        self.parent_page_id = _clean(parent_page_id) or client.config.parent_page_id
        self.batch_payload_limit_bytes = batch_payload_limit_bytes

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        transport: NotionJsonTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> "NotionPublisher":
        client = NotionClient.from_env(
            environ=environ,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        return cls(client, parent_page_id=client.config.parent_page_id)

    def publish(
        self,
        structured_content: Any,
        *,
        title: str | None = None,
        existing_page_id: str | None = None,
        parent_page_id: str | None = None,
        icon: str | Mapping[str, Any] | None = None,
    ) -> NotionPublishResult:
        page_title = _clean(title) or title_from_structured_content(structured_content)
        if not page_title:
            raise NotionContentError(
                "A non-empty title is required. Pass title=... or include a title in structured_content."
            )
        blocks = structured_content_to_notion_blocks(structured_content)
        batches = batch_notion_blocks(
            blocks,
            max_payload_bytes=self.batch_payload_limit_bytes,
        )
        clean_existing_id = _clean(existing_page_id)
        created = not bool(clean_existing_id)

        if clean_existing_id:
            page = self.client.replace_page(
                page_id=clean_existing_id,
                title=page_title,
                icon=icon,
            )
            page_id = _clean(page.get("id")) or clean_existing_id
        else:
            target_parent_id = _clean(parent_page_id) or self.parent_page_id
            if not target_parent_id:
                raise NotionConfigurationError(
                    f"{NOTION_PARENT_PAGE_ID_ENV} is required when creating a Notion page. "
                    "It may also be passed as parent_page_id."
                )
            page = self.client.create_child_page(
                parent_page_id=target_parent_id,
                title=page_title,
                icon=icon,
            )
            page_id = _clean(page.get("id"))
            if not page_id:
                raise NotionAPIError(
                    "Notion created a page but did not return its page ID.",
                    code="invalid_response",
                    response=page,
                )

        try:
            for batch in batches:
                self.client.append_block_children(block_id=page_id, children=batch)
        except Exception as exc:
            action = "created" if created else "cleared for replacement"
            raise NotionPublishError(
                f"Notion page {page_id} was {action}, but its content upload did not finish: {exc}",
                page_id=page_id,
                created=created,
            ) from exc

        content_hash = notion_content_hash(page_title, blocks)
        return NotionPublishResult(
            page_id=page_id,
            url=_clean(page.get("url")) or None,
            public_url=_clean(page.get("public_url")) or None,
            created=created,
            block_count=len(blocks),
            batch_count=len(batches),
            content_hash=content_hash,
            page=dict(page),
        )


def publish_structured_content_to_notion(
    structured_content: Any,
    *,
    title: str | None = None,
    existing_page_id: str | None = None,
    parent_page_id: str | None = None,
    icon: str | Mapping[str, Any] | None = None,
    client: NotionClient | None = None,
    transport: NotionJsonTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> NotionPublishResult:
    """Convenience entry point for route/task layers.

    Pass ``existing_page_id`` after the first successful publish. Repeated calls
    then replace that page's title and children rather than creating duplicates.
    """

    publisher = (
        NotionPublisher(client, parent_page_id=parent_page_id)
        if client is not None
        else NotionPublisher.from_env(environ=environ, transport=transport)
    )
    return publisher.publish(
        structured_content,
        title=title,
        existing_page_id=existing_page_id,
        parent_page_id=parent_page_id,
        icon=icon,
    )


# Concise alias for callers that already know the target is Notion.
publish_to_notion = publish_structured_content_to_notion


def title_from_structured_content(content: Any) -> str:
    if not isinstance(content, Mapping):
        return ""
    return _clean(content.get("title") or content.get("name") or content.get("heading"))


def structured_content_to_notion_blocks(content: Any) -> list[JsonObject]:
    """Convert a flexible structured document into Notion block request objects.

    Supported explicit node types include paragraphs, headings, bullets, numbered
    items, checklists, quotes, callouts, code/prompts, dividers, bookmarks, tables,
    sections, and list containers. A root mapping may contain ``blocks`` and/or
    ``sections`` plus common lead-magnet fields such as ``summary``, ``audience``,
    ``steps``, ``prompts``, ``checklist``, ``leadpoet_step``, ``cta``, and
    ``sources``.
    """

    return _BlockRenderer().render_root(content)


# Backwards-friendly shorter name for route code and tests.
structured_content_to_blocks = structured_content_to_notion_blocks


def notion_rich_text(
    text: object,
    *,
    link: str | None = None,
    annotations: Mapping[str, Any] | None = None,
) -> list[JsonObject]:
    value = str(text or "")
    if link and len(link) > NOTION_RICH_TEXT_CONTENT_LIMIT:
        raise NotionContentError(
            f"Notion URLs may not exceed {NOTION_RICH_TEXT_CONTENT_LIMIT} characters."
        )
    chunks = _text_chunks(value, NOTION_RICH_TEXT_CONTENT_LIMIT)
    if len(chunks) > NOTION_RICH_TEXT_ARRAY_LIMIT:
        raise NotionContentError(
            "A single Notion block cannot contain more than "
            f"{NOTION_RICH_TEXT_ARRAY_LIMIT * NOTION_RICH_TEXT_CONTENT_LIMIT:,} text characters."
        )
    normalized_annotations = _notion_annotations(annotations)
    rich_text: list[JsonObject] = []
    for chunk in chunks:
        text_payload: JsonObject = {"content": chunk}
        if link:
            text_payload["link"] = {"url": link}
        item: JsonObject = {"type": "text", "text": text_payload}
        if normalized_annotations:
            item["annotations"] = normalized_annotations
        rich_text.append(item)
    return rich_text


def batch_notion_blocks(
    blocks: Sequence[Mapping[str, Any]],
    *,
    max_blocks: int = NOTION_APPEND_BLOCK_LIMIT,
    max_payload_bytes: int = DEFAULT_BATCH_PAYLOAD_LIMIT_BYTES,
) -> list[list[JsonObject]]:
    if max_blocks < 1 or max_blocks > NOTION_APPEND_BLOCK_LIMIT:
        raise ValueError(
            f"max_blocks must be between 1 and {NOTION_APPEND_BLOCK_LIMIT}."
        )
    if max_payload_bytes < 1 or max_payload_bytes >= NOTION_REQUEST_SIZE_LIMIT_BYTES:
        raise ValueError(
            f"max_payload_bytes must be between 1 and {NOTION_REQUEST_SIZE_LIMIT_BYTES - 1}."
        )

    batches: list[list[JsonObject]] = []
    current: list[JsonObject] = []
    for raw_block in blocks:
        block = dict(raw_block)
        single_size = _append_payload_size([block])
        if single_size > max_payload_bytes:
            raise NotionContentError(
                "One rendered Notion block exceeds the safe request payload limit "
                f"({single_size:,} bytes > {max_payload_bytes:,} bytes)."
            )
        candidate = [*current, block]
        if current and (
            len(candidate) > max_blocks
            or _append_payload_size(candidate) > max_payload_bytes
        ):
            batches.append(current)
            current = [block]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def notion_content_hash(title: str, blocks: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        {"title": title, "blocks": list(blocks)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _BlockRenderer:
    _ROOT_BLOCK_TYPES = {
        "paragraph",
        "text",
        "heading",
        "heading_1",
        "heading1",
        "h1",
        "heading_2",
        "heading2",
        "h2",
        "heading_3",
        "heading3",
        "h3",
        "bullet",
        "bulleted_list_item",
        "unordered_item",
        "numbered",
        "numbered_list_item",
        "ordered_item",
        "step",
        "to_do",
        "todo",
        "checklist_item",
        "quote",
        "callout",
        "code",
        "prompt",
        "divider",
        "bookmark",
        "link",
        "table",
    }
    _ROOT_META_KEYS = {
        "title",
        "name",
        "heading",
        "id",
        "status",
        "format",
        "kind",
        "type",
        "source_ids",
        "source_refs",
        "research_run_id",
        "metadata",
    }

    def render_root(self, content: Any) -> list[JsonObject]:
        if content is None:
            return []
        if isinstance(content, str):
            return self._paragraphs(content)
        if _is_sequence(content):
            return self._render_many(content)
        if not isinstance(content, Mapping):
            return self._paragraphs(str(content))

        root_type = _clean(content.get("type") or content.get("kind")).lower()
        root_type = root_type.replace("-", "_").replace(" ", "_")
        if root_type in self._ROOT_BLOCK_TYPES:
            return self._render_node(content)

        blocks: list[JsonObject] = []
        consumed = set(self._ROOT_META_KEYS)

        subtitle = content.get("subtitle")
        if _has_value(subtitle):
            blocks.extend(self._text_blocks("paragraph", subtitle, annotations={"italic": True}))
        consumed.add("subtitle")

        for key in ("one_line_promise", "promise", "summary", "description"):
            if _has_value(content.get(key)):
                blocks.extend(self._text_blocks("callout", content[key], icon="💡"))
            consumed.add(key)

        for key, label in (
            ("audience", "For"),
            ("outcome", "Outcome"),
            ("expected_outcome", "Outcome"),
        ):
            if _has_value(content.get(key)):
                blocks.extend(self._paragraphs(f"{label}: {_scalar_text(content[key])}"))
            consumed.add(key)

        for key in ("introduction", "intro"):
            if _has_value(content.get(key)):
                blocks.extend(self._render_value(content[key]))
            consumed.add(key)

        if _has_value(content.get("overview")):
            blocks.extend(self._heading("How to use this", 2))
            blocks.extend(self._render_value(content["overview"]))
        consumed.add("overview")

        if _has_value(content.get("blocks")):
            blocks.extend(self._render_value(content["blocks"]))
        consumed.add("blocks")

        if _has_value(content.get("sections")):
            blocks.extend(self._render_sections(content["sections"]))
        consumed.add("sections")

        blocks.extend(self._render_named_collection(content, "steps", "Steps", "numbered_list"))
        blocks.extend(self._render_named_collection(content, "prompts", "Prompts", "prompt_list"))
        blocks.extend(self._render_named_collection(content, "checklist", "Checklist", "checklist"))
        blocks.extend(self._render_named_collection(content, "examples", "Examples", "bulleted_list"))
        blocks.extend(
            self._render_named_collection(
                content,
                "implementation_checklist",
                "Implementation checklist",
                "checklist",
            )
        )
        consumed.update({"steps", "prompts", "checklist", "examples", "implementation_checklist"})

        for key in ("leadpoet_step", "leadpoet_workflow", "product_step"):
            if _has_value(content.get(key)):
                blocks.extend(self._render_workflow(content[key]))
            consumed.add(key)

        if _has_value(content.get("cta")):
            blocks.extend(self._text_blocks("callout", content["cta"], icon="📣"))
        consumed.add("cta")

        if _has_value(content.get("sources")):
            blocks.extend(self._heading("Sources", 2))
            blocks.extend(self._render_sources(content["sources"]))
        consumed.add("sources")

        if _has_value(content.get("source_notes")):
            blocks.extend(self._heading("Source notes", 2))
            blocks.extend(self._render_source_notes(content["source_notes"]))
        consumed.add("source_notes")

        for key, value in content.items():
            if key in consumed or not _has_value(value):
                continue
            blocks.extend(self._heading(_humanize_key(str(key)), 2))
            blocks.extend(self._render_value(value))
        return blocks

    def _render_named_collection(
        self,
        content: Mapping[str, Any],
        key: str,
        heading: str,
        node_type: str,
    ) -> list[JsonObject]:
        value = content.get(key)
        if not _has_value(value):
            return []
        return [
            *self._heading(heading, 2),
            *self._render_node({"type": node_type, "items": value}),
        ]

    def _render_sections(self, value: Any) -> list[JsonObject]:
        if _is_sequence(value):
            blocks: list[JsonObject] = []
            for section in value:
                blocks.extend(self._render_section(section))
            return blocks
        return self._render_section(value)

    def _render_section(self, section: Any) -> list[JsonObject]:
        if not isinstance(section, Mapping):
            return self._render_value(section)
        blocks: list[JsonObject] = []
        title = section.get("title") or section.get("heading") or section.get("name")
        if _has_value(title):
            blocks.extend(self._heading(_scalar_text(title), int(section.get("level") or 2)))
        for key in ("summary", "description", "content", "body"):
            if _has_value(section.get(key)):
                blocks.extend(self._render_value(section[key]))
        if _has_value(section.get("blocks")):
            blocks.extend(self._render_value(section["blocks"]))
        for key, node_type in (
            ("steps", "numbered_list"),
            ("items", "bulleted_list"),
            ("prompts", "prompt_list"),
            ("checklist", "checklist"),
            ("examples", "bulleted_list"),
        ):
            if _has_value(section.get(key)):
                blocks.extend(self._render_node({"type": node_type, "items": section[key]}))
        for key, entry_type in (("templates", "template"), ("questions", "question")):
            if _has_value(section.get(key)):
                blocks.extend(self._render_structured_entries(section[key], entry_type=entry_type))
        if _has_value(section.get("sources")):
            blocks.extend(self._render_sources(section["sources"]))
        return blocks

    def _render_value(self, value: Any) -> list[JsonObject]:
        if value is None:
            return []
        if isinstance(value, str):
            return self._paragraphs(value)
        if isinstance(value, Mapping):
            return self._render_node(value)
        if _is_sequence(value):
            return self._render_many(value)
        return self._paragraphs(str(value))

    def _render_many(self, values: Sequence[Any]) -> list[JsonObject]:
        blocks: list[JsonObject] = []
        for value in values:
            blocks.extend(self._render_value(value))
        return blocks

    def _render_node(self, node: Mapping[str, Any]) -> list[JsonObject]:
        raw_type = _clean(node.get("type") or node.get("kind"))
        if not raw_type:
            if any(key in node for key in ("title", "heading", "name")):
                return self._render_section(node)
            if node.get("url"):
                return self._render_source(node)
            return self.render_root(node)

        node_type = raw_type.lower().replace("-", "_").replace(" ", "_")
        if node_type in {"paragraph", "text"}:
            return self._paragraphs(_node_text(node), link=_node_url(node))
        if node_type in {"heading", "heading_1", "heading1", "h1"}:
            level = int(node.get("level") or (1 if node_type != "heading" else 2))
            return self._heading(_node_text(node), level)
        if node_type in {"heading_2", "heading2", "h2"}:
            return self._heading(_node_text(node), 2)
        if node_type in {"heading_3", "heading3", "h3"}:
            return self._heading(_node_text(node), 3)
        if node_type in {"bullet", "bulleted_list_item", "unordered_item"}:
            return self._list_item(node, "bulleted_list_item")
        if node_type in {"numbered", "numbered_list_item", "ordered_item", "step"}:
            return self._list_item(node, "numbered_list_item")
        if node_type in {"to_do", "todo", "checklist_item"}:
            return self._list_item(node, "to_do")
        if node_type in {"bulleted_list", "bullet_list", "unordered_list"}:
            return self._list_container(node.get("items"), "bulleted_list_item")
        if node_type in {"numbered_list", "ordered_list", "steps"}:
            return self._list_container(node.get("items"), "numbered_list_item")
        if node_type in {"checklist", "todo_list"}:
            return self._list_container(node.get("items"), "to_do")
        if node_type in {"prompt_list", "prompts"}:
            return self._prompt_list(node.get("items"))
        if node_type == "quote":
            return self._text_blocks("quote", _node_text(node))
        if node_type == "callout":
            return self._text_blocks("callout", _node_text(node), icon=_clean(node.get("icon")) or "💡")
        if node_type in {"code", "prompt"}:
            return self._code_blocks(
                _node_text(node),
                language=_clean(node.get("language")) or "plain text",
                caption=_clean(node.get("caption")) or None,
            )
        if node_type == "divider":
            return [{"object": "block", "type": "divider", "divider": {}}]
        if node_type in {"bookmark", "link"}:
            url = _node_url(node)
            if not url:
                raise NotionContentError("A bookmark/link block requires a non-empty url.")
            _validate_url(url)
            caption = _node_text(node)
            payload: JsonObject = {"url": url}
            if caption and caption != url:
                payload["caption"] = notion_rich_text(caption)
            return [{"object": "block", "type": "bookmark", "bookmark": payload}]
        if node_type == "table":
            return [self._table_block(node)]
        if node_type == "section":
            return self._render_section(node)
        raise NotionContentError(f"Unsupported structured content block type: {raw_type}")

    def _paragraphs(self, value: object, *, link: str | None = None) -> list[JsonObject]:
        text = str(value or "").strip()
        if not text:
            return []
        paragraphs = [piece.strip() for piece in text.split("\n\n") if piece.strip()]
        return [block for paragraph in paragraphs for block in self._text_blocks("paragraph", paragraph, link=link)]

    def _heading(self, value: object, level: int) -> list[JsonObject]:
        clean_level = max(1, min(3, int(level)))
        return self._text_blocks(f"heading_{clean_level}", value)

    def _text_blocks(
        self,
        block_type: str,
        value: object,
        *,
        link: str | None = None,
        annotations: Mapping[str, Any] | None = None,
        icon: str | None = None,
    ) -> list[JsonObject]:
        text = _scalar_text(value)
        if not text:
            return []
        chunks = _text_chunks(text, NOTION_RICH_TEXT_CONTENT_LIMIT)
        blocks: list[JsonObject] = []
        for chunk in chunks:
            payload: JsonObject = {
                "rich_text": notion_rich_text(chunk, link=link, annotations=annotations),
            }
            if block_type == "callout":
                payload["icon"] = {"type": "emoji", "emoji": icon or "💡"}
            blocks.append({"object": "block", "type": block_type, block_type: payload})
        return blocks

    def _list_container(self, values: Any, block_type: str) -> list[JsonObject]:
        items = list(values) if _is_sequence(values) else [values]
        blocks: list[JsonObject] = []
        for item in items:
            node = item if isinstance(item, Mapping) else {"text": item}
            blocks.extend(self._list_item(node, block_type))
        return blocks

    def _list_item(self, node: Mapping[str, Any], block_type: str) -> list[JsonObject]:
        text = _node_text(node)
        if not text:
            return []
        url = _node_url(node)
        chunks = _text_chunks(text, NOTION_RICH_TEXT_CONTENT_LIMIT)
        blocks: list[JsonObject] = []
        for chunk in chunks:
            payload: JsonObject = {"rich_text": notion_rich_text(chunk, link=url)}
            if block_type == "to_do":
                payload["checked"] = bool(node.get("checked", False))
            blocks.append({"object": "block", "type": block_type, block_type: payload})
        return blocks

    def _prompt_list(self, values: Any) -> list[JsonObject]:
        items = list(values) if _is_sequence(values) else [values]
        blocks: list[JsonObject] = []
        for item in items:
            if isinstance(item, Mapping):
                title = item.get("title") or item.get("name")
                if _has_value(title):
                    blocks.extend(self._heading(title, 3))
                if _has_value(item.get("purpose")):
                    blocks.extend(self._paragraphs(f"Purpose: {_scalar_text(item['purpose'])}"))
                if _has_value(item.get("required_inputs")):
                    blocks.extend(self._paragraphs("Required inputs:"))
                    blocks.extend(self._list_container(item["required_inputs"], "bulleted_list_item"))
                blocks.extend(
                    self._code_blocks(
                        _node_text(item),
                        language=_clean(item.get("language")) or "plain text",
                        caption=_clean(item.get("caption")) or None,
                    )
                )
                if _has_value(item.get("usage_steps")):
                    blocks.extend(self._paragraphs("How to use it:"))
                    blocks.extend(self._list_container(item["usage_steps"], "numbered_list_item"))
                if _has_value(item.get("expected_output")):
                    blocks.extend(
                        self._paragraphs(f"Expected output: {_scalar_text(item['expected_output'])}")
                    )
            else:
                blocks.extend(self._code_blocks(_scalar_text(item), language="plain text"))
        return blocks

    def _render_structured_entries(self, values: Any, *, entry_type: str) -> list[JsonObject]:
        items = list(values) if _is_sequence(values) else [values]
        blocks: list[JsonObject] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                if entry_type == "template":
                    blocks.extend(self._code_blocks(item, language="plain text"))
                else:
                    blocks.extend(
                        self._list_item({"text": item}, "numbered_list_item")
                    )
                continue
            title = _clean(
                item.get("title")
                or item.get("name")
                or item.get("question")
                or f"{entry_type.title()} {index}"
            )
            blocks.extend(self._heading(title, 3))
            for key, label in (
                ("purpose", "Purpose"),
                ("description", "Description"),
                ("usage_notes", "Usage notes"),
                ("scoring_rule", "Scoring"),
                ("expected_output", "Expected output"),
            ):
                if _has_value(item.get(key)):
                    blocks.extend(self._paragraphs(f"{label}: {_scalar_text(item[key])}"))
            body = item.get("template") or item.get("content") or item.get("text")
            if entry_type == "template" and _has_value(body):
                blocks.extend(self._code_blocks(body, language="plain text"))
            for key, block_type in (
                ("steps", "numbered_list_item"),
                ("options", "bulleted_list_item"),
                ("items", "bulleted_list_item"),
            ):
                if _has_value(item.get(key)):
                    blocks.extend(self._list_container(item[key], block_type))
        return blocks

    def _render_workflow(self, value: Any) -> list[JsonObject]:
        if not isinstance(value, Mapping):
            return [
                *self._heading("Leadpoet in the workflow", 2),
                *self._render_value(value),
            ]
        title = _clean(value.get("heading") or value.get("title")) or "Leadpoet in the workflow"
        blocks = self._heading(title, 2)
        if _has_value(value.get("context_basis")):
            blocks.extend(
                self._text_blocks(
                    "paragraph",
                    f"Basis: {_scalar_text(value['context_basis'])}",
                    annotations={"italic": True},
                )
            )
        if _has_value(value.get("body")):
            blocks.extend(self._render_value(value["body"]))
        if _has_value(value.get("steps")):
            blocks.extend(self._list_container(value["steps"], "numbered_list_item"))
        return blocks

    def _render_source_notes(self, value: Any) -> list[JsonObject]:
        notes = list(value) if _is_sequence(value) else [value]
        blocks: list[JsonObject] = []
        for note in notes:
            if not isinstance(note, Mapping):
                blocks.extend(self._list_item({"text": note}, "bulleted_list_item"))
                continue
            source_id = _clean(note.get("source_id"))
            observation = _clean(note.get("observation"))
            adaptation = _clean(note.get("adaptation"))
            parts = []
            if source_id:
                parts.append(source_id)
            if observation:
                parts.append(f"Observed: {observation}")
            if adaptation:
                parts.append(f"Adapted: {adaptation}")
            if parts:
                blocks.extend(
                    self._list_item({"text": " — ".join(parts)}, "bulleted_list_item")
                )
        return blocks

    def _code_blocks(
        self,
        value: object,
        *,
        language: str,
        caption: str | None = None,
    ) -> list[JsonObject]:
        text = _scalar_text(value)
        if not text:
            return []
        clean_language = language if language in _NOTION_CODE_LANGUAGES else "plain text"
        blocks: list[JsonObject] = []
        for index, chunk in enumerate(_text_chunks(text, NOTION_RICH_TEXT_CONTENT_LIMIT)):
            payload: JsonObject = {
                "rich_text": notion_rich_text(chunk),
                "language": clean_language,
            }
            if caption and index == 0:
                payload["caption"] = notion_rich_text(caption)
            blocks.append({"object": "block", "type": "code", "code": payload})
        return blocks

    def _render_sources(self, value: Any) -> list[JsonObject]:
        sources = list(value) if _is_sequence(value) else [value]
        blocks: list[JsonObject] = []
        for source in sources:
            blocks.extend(self._render_source(source))
        return blocks

    def _render_source(self, source: Any) -> list[JsonObject]:
        if isinstance(source, str):
            if source.startswith(("http://", "https://")):
                return self._list_item({"text": source, "url": source}, "bulleted_list_item")
            return self._list_item({"text": source}, "bulleted_list_item")
        if not isinstance(source, Mapping):
            return self._list_item({"text": str(source)}, "bulleted_list_item")
        url = _node_url(source)
        label = _clean(
            source.get("title")
            or source.get("label")
            or source.get("name")
            or source.get("text")
            or url
        )
        return self._list_item({"text": label, "url": url}, "bulleted_list_item")

    def _table_block(self, node: Mapping[str, Any]) -> JsonObject:
        headers = node.get("headers") or []
        rows = node.get("rows") or []
        if not _is_sequence(headers) or not _is_sequence(rows):
            raise NotionContentError("A table requires headers and rows arrays.")
        header_values = list(headers)
        row_values = [list(row) if _is_sequence(row) else [row] for row in rows]
        width = len(header_values) or max((len(row) for row in row_values), default=0)
        if width < 1:
            raise NotionContentError("A table must contain at least one column.")
        all_rows = ([header_values] if header_values else []) + row_values
        if len(all_rows) > NOTION_APPEND_BLOCK_LIMIT:
            raise NotionContentError(
                f"A Notion table may contain at most {NOTION_APPEND_BLOCK_LIMIT} rows in one rendered block."
            )
        table_rows: list[JsonObject] = []
        for row in all_rows:
            padded = [*row, *([""] * (width - len(row)))]
            cells = [notion_rich_text(_scalar_text(cell)) for cell in padded[:width]]
            table_rows.append(
                {"object": "block", "type": "table_row", "table_row": {"cells": cells}}
            )
        return {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": width,
                "has_column_header": bool(header_values),
                "has_row_header": bool(node.get("has_row_header", False)),
                "children": table_rows,
            },
        }


_NOTION_CODE_LANGUAGES = {
    "abap",
    "arduino",
    "bash",
    "basic",
    "c",
    "c#",
    "c++",
    "clojure",
    "css",
    "dart",
    "diff",
    "docker",
    "elixir",
    "elm",
    "erlang",
    "flow",
    "fortran",
    "f#",
    "gherkin",
    "glsl",
    "go",
    "graphql",
    "groovy",
    "haskell",
    "html",
    "java",
    "javascript",
    "json",
    "julia",
    "kotlin",
    "latex",
    "less",
    "lisp",
    "livescript",
    "lua",
    "makefile",
    "markdown",
    "markup",
    "matlab",
    "mermaid",
    "nix",
    "objective-c",
    "ocaml",
    "pascal",
    "perl",
    "php",
    "plain text",
    "powershell",
    "prolog",
    "protobuf",
    "python",
    "r",
    "reason",
    "ruby",
    "rust",
    "sass",
    "scala",
    "scheme",
    "scss",
    "shell",
    "sql",
    "swift",
    "typescript",
    "vb.net",
    "verilog",
    "vhdl",
    "visual basic",
    "webassembly",
    "xml",
    "yaml",
    "java/c/c++/c#",
}


def _notion_icon(icon: str | Mapping[str, Any] | None) -> JsonObject | None:
    if icon is None:
        return None
    if isinstance(icon, Mapping):
        return dict(icon)
    emoji = _clean(icon)
    return {"type": "emoji", "emoji": emoji} if emoji else None


def _notion_annotations(value: Mapping[str, Any] | None) -> JsonObject:
    if not value:
        return {}
    allowed = {"bold", "italic", "strikethrough", "underline", "code", "color"}
    return {key: item for key, item in value.items() if key in allowed}


def _text_chunks(value: str, limit: int) -> list[str]:
    if not value:
        return []
    chunks: list[str] = []
    remaining = value
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if split_at < max(1, limit // 2):
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _append_payload_size(blocks: Sequence[Mapping[str, Any]]) -> int:
    return len(
        json.dumps(
            {"children": list(blocks)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _decode_json_response(raw: bytes, *, status_code: int) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotionAPIError(
            f"Notion returned a non-JSON response (HTTP {status_code}).",
            status_code=status_code,
            code="invalid_response",
        ) from exc
    if not isinstance(payload, Mapping):
        raise NotionAPIError(
            f"Notion returned an unexpected JSON response (HTTP {status_code}).",
            status_code=status_code,
            code="invalid_response",
        )
    return payload


def _decode_error_payload(raw: bytes) -> JsonObject:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"message": raw.decode("utf-8", errors="replace")[:1_000]}
    return dict(value) if isinstance(value, Mapping) else {"message": str(value)}


def _notion_api_error_message(status_code: int, detail: str) -> str:
    if status_code == 401:
        return f"Notion rejected {NOTION_API_TOKEN_ENV}. Check or rotate the integration token. {detail}"
    if status_code == 403:
        return (
            "The Notion integration lacks access or update-content capability for this page. "
            f"Share the page with the integration and check its capabilities. {detail}"
        )
    if status_code == 404:
        return (
            "The Notion page/block was not found or is not shared with the integration. "
            f"Check the configured page ID and connection access. {detail}"
        )
    if status_code == 429:
        return f"Notion rate-limited the publish request. Retry it after the requested delay. {detail}"
    return f"Notion API request failed (HTTP {status_code}): {detail}"


def _node_text(node: Mapping[str, Any]) -> str:
    for key in ("text", "content", "value", "body", "prompt", "label"):
        value = node.get(key)
        if _has_value(value) and not isinstance(value, Mapping) and not _is_sequence(value):
            return _scalar_text(value)
    return ""


def _node_url(node: Mapping[str, Any]) -> str | None:
    value = _clean(node.get("url") or node.get("href") or node.get("source_url"))
    if value:
        _validate_url(value)
        return value
    return None


def _validate_url(value: str) -> None:
    if len(value) > NOTION_RICH_TEXT_CONTENT_LIMIT:
        raise NotionContentError(
            f"Notion URLs may not exceed {NOTION_RICH_TEXT_CONTENT_LIMIT} characters."
        )
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NotionContentError(f"Notion link must be an absolute HTTP(S) URL: {value}")


def _scalar_text(value: object) -> str:
    if isinstance(value, Mapping) or _is_sequence(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").strip()


def _humanize_key(value: str) -> str:
    return " ".join(piece for piece in value.replace("-", "_").split("_") if piece).strip().title()


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Sequence, Mapping)):
        return bool(value)
    return True


def _required_text(value: object, name: str) -> str:
    cleaned = _clean(value)
    if not cleaned:
        raise ValueError(f"{name} is required.")
    return cleaned


def _clean(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "DEFAULT_BATCH_PAYLOAD_LIMIT_BYTES",
    "NOTION_API_BASE_URL",
    "NOTION_API_TOKEN_ENV",
    "NOTION_API_VERSION",
    "NOTION_APPEND_BLOCK_LIMIT",
    "NOTION_PARENT_PAGE_ID_ENV",
    "NotionAPIError",
    "NotionClient",
    "NotionConfig",
    "NotionConfigurationError",
    "NotionContentError",
    "NotionJsonTransport",
    "NotionPublishError",
    "NotionPublishResult",
    "NotionPublisher",
    "NotionPublisherError",
    "UrllibJsonTransport",
    "batch_notion_blocks",
    "notion_content_hash",
    "notion_rich_text",
    "publish_structured_content_to_notion",
    "publish_to_notion",
    "structured_content_to_blocks",
    "structured_content_to_notion_blocks",
    "title_from_structured_content",
]
