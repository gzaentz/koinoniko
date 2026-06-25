from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from leadpoet_social_brain.core.models import EngagementMetrics, PostNote, SocialPost
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


@dataclass(frozen=True)
class BatchImportResult:
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    topics: Counter[str] = field(default_factory=Counter)
    post_types: Counter[str] = field(default_factory=Counter)


def ingest_x_post(
    store: SQLiteStore,
    *,
    text: str,
    url: str | None,
    date: str | None,
    author: str,
    topic: str,
    post_type: str,
    metrics: EngagementMetrics | None = None,
    worked_reason: str | None = None,
    pattern_name: str | None = None,
    hook: str | None = None,
    structure: str | None = None,
    tone: str | None = None,
    cta: str | None = None,
    formatting: str | None = None,
    imitate: bool = False,
) -> SocialPost:
    adapter = get_x_adapter()
    adapter.validate_post_type(post_type)
    if not text or not text.strip():
        raise ValueError("Post text is required.")
    normalized_url = adapter.normalize_url(url)
    post = SocialPost(
        platform=adapter.rules.name,
        text=text.strip(),
        url=normalized_url,
        published_at=date,
        author=author.strip(),
        topic=topic.strip(),
        post_type=post_type.strip(),
        status="example",
    )
    saved = store.save_post(post)
    if metrics:
        store.add_metrics(saved.id, metrics)
    if any([worked_reason, pattern_name, hook, structure, tone, cta, formatting, imitate]):
        store.save_note(
            PostNote(
                post_id=saved.id,
                worked_reason=worked_reason,
                pattern_name=pattern_name,
                hook=hook,
                structure=structure,
                tone=tone,
                cta=cta,
                formatting=formatting,
                imitate=imitate,
            )
        )
    return saved


def publish_x_draft(
    store: SQLiteStore,
    *,
    draft_id: str,
    url: str,
    date: str,
    author: str,
    metrics: EngagementMetrics | None = None,
) -> SocialPost:
    draft = store.get_generated_draft(draft_id)
    if not draft:
        raise ValueError(f"No generated draft found for id {draft_id}")
    adapter = get_x_adapter()
    adapter.validate_text(draft.text, max_chars=draft.request.max_chars)
    post = store.save_post(
        SocialPost(
            platform="x",
            text=draft.text,
            url=adapter.normalize_url(url),
            published_at=date,
            author=author,
            topic=draft.request.topic,
            post_type=draft.request.post_type,
            status="published",
            source_draft_id=draft.id,
        )
    )
    store.link_post_to_draft(post.id, draft)
    if metrics:
        store.add_metrics(post.id, metrics)
    return post


def ingest_x_batch_csv(
    store: SQLiteStore,
    csv_path: str | Path,
    *,
    default_author: str = "Gavin",
) -> BatchImportResult:
    imported = 0
    skipped = 0
    errors: list[str] = []
    topics: Counter[str] = Counter()
    post_types: Counter[str] = Counter()

    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                text = _cell(row, "text")
                topic = _cell(row, "topic")
                post_type = _cell(row, "post_type")
                if not text or not topic or not post_type:
                    raise ValueError("text, topic, and post_type are required.")
                post = ingest_x_post(
                    store,
                    text=text,
                    url=_cell(row, "url") or None,
                    date=_cell(row, "date") or None,
                    author=_cell(row, "author") or default_author,
                    topic=topic,
                    post_type=post_type,
                    metrics=_metrics_from_row(row),
                    worked_reason=_cell(row, "worked_reason") or None,
                    pattern_name=_cell(row, "pattern") or None,
                    hook=_cell(row, "hook") or None,
                    structure=_cell(row, "structure") or None,
                    tone=_cell(row, "tone") or None,
                    cta=_cell(row, "cta") or None,
                    formatting=_cell(row, "formatting") or None,
                    imitate=_truthy(_cell(row, "imitate")),
                )
                imported += 1
                topics[post.topic] += 1
                post_types[post.post_type] += 1
            except Exception as exc:  # noqa: BLE001 - batch imports should collect row errors.
                skipped += 1
                errors.append(f"row {row_number}: {exc}")

    return BatchImportResult(
        imported=imported,
        skipped=skipped,
        errors=errors,
        topics=topics,
        post_types=post_types,
    )


def _metrics_from_row(row: dict[str, str | None]) -> EngagementMetrics | None:
    values = {
        "impressions": _int_cell(row, "impressions"),
        "likes": _int_cell(row, "likes"),
        "replies": _int_cell(row, "replies"),
        "reposts": _int_cell(row, "reposts"),
        "quotes": _int_cell(row, "quotes"),
        "bookmarks": _int_cell(row, "bookmarks"),
    }
    if all(value is None for value in values.values()):
        return None
    return EngagementMetrics(**values)


def _cell(row: dict[str, str | None], key: str) -> str:
    return (row.get(key) or "").strip()


def _int_cell(row: dict[str, str | None], key: str) -> int | None:
    value = _cell(row, key)
    if not value:
        return None
    return int(value.replace(",", ""))


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "t"}
