from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from leadpoet_social_brain.context import POSITIONING_KINDS
from leadpoet_social_brain.core.models import TopicIdea
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider, resolve_model_routing
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


TOPIC_SOURCE_TYPES = {
    "evergreen",
    "trend",
    "performance",
    "product",
    "founder",
    "follow_up",
}


@dataclass(frozen=True)
class TopicRefreshResult:
    ideas: list[TopicIdea]
    warnings: list[str]


def refresh_topic_ideas(
    store: SQLiteStore,
    provider: DraftProvider,
    *,
    limit: int = 10,
) -> TopicRefreshResult:
    requested = max(3, min(int(limit), 20))
    prompt = build_topic_ideas_prompt(store, limit=requested)
    raw = generate_with_provider(
        provider,
        prompt,
        variants=1,
        model=resolve_model_routing(store, provider).strategist,
    )[0]
    parsed = parse_topic_ideas(raw, limit=requested)
    if not parsed:
        raise ValueError("Topic Scout did not return any usable topic ideas.")

    for existing in store.list_topic_ideas(status="suggested", limit=200):
        store.mark_topic_idea_status(existing.id, "superseded")

    saved: list[TopicIdea] = []
    seen: set[str] = set()
    for idea in parsed:
        key = topic_idea_key(idea)
        if not key or key in seen:
            continue
        seen.add(key)
        saved.append(store.save_topic_idea(idea))

    warnings: list[str] = []
    if len(saved) < requested:
        warnings.append(f"Topic Scout returned {len(saved)} distinct idea(s) for a target of {requested}.")
    return TopicRefreshResult(ideas=saved, warnings=warnings)


def build_topic_ideas_prompt(store: SQLiteStore, *, limit: int) -> str:
    facts = [
        {
            "kind": section.kind,
            "name": section.name,
            "content": compact(section.content, 900),
        }
        for section in store.list_context_sections(kinds=POSITIONING_KINDS)[:8]
    ]
    posts = store.top_examples(platform="x", limit=10, imitate_only=False)
    post_metrics = store.latest_metrics_by_post_id([post.id for post in posts])
    best_posts = [
        {
            "id": post.id,
            "topic": post.topic,
            "post_type": post.post_type,
            "text": compact(post.text, 500),
            "likes": (post_metrics.get(post.id).likes if post_metrics.get(post.id) else None),
            "replies": (post_metrics.get(post.id).replies if post_metrics.get(post.id) else None),
            "bookmarks": (post_metrics.get(post.id).bookmarks if post_metrics.get(post.id) else None),
        }
        for post in posts
    ]
    trends = [
        {
            "id": trend.id,
            "topic": trend.topic,
            "title": trend.title,
            "summary": compact(trend.summary, 300),
            "why_it_matters": compact(trend.why_it_matters, 240),
            "saved_angle": compact(trend.leadpoet_angle, 260),
        }
        for trend in store.list_trends(status="unused", limit=12)
    ]
    recent_queue = [
        {
            "topic": item.topic,
            "post_type": item.post_type,
            "angle": compact(item.angle or item.text, 240),
            "status": item.status,
        }
        for item in store.list_content_queue(platform="x", limit=30)
    ]
    recent_topics = [
        {"title": idea.title, "angle": idea.angle, "status": idea.status}
        for idea in store.list_topic_ideas(status=None, limit=40)
    ]
    context = {
        "facts_and_positioning": facts,
        "best_authored_posts": best_posts,
        "unused_trends": trends,
        "recent_queue": recent_queue,
        "recent_topic_ideas": recent_topics,
    }
    return f"""Create {limit} specific X post topics for this workspace.

Goal: hand the writer a self-contained premise so the user does not have to invent the strategy or angle.

Success criteria:
- Every idea has one debatable thesis or useful mechanism, not a broad category label.
- The angle is detailed enough to become a post without a follow-up question.
- Ideas come from the available evidence and do not invent product claims, customer stories, metrics, or founder experiences.
- Avoid repeating recent queued posts or recent topic ideas.
- At most one third of the ideas should be trend reactions. Include a useful mix of founder POV, buyer/problem mechanics, product beliefs, follow-ups to strong authored posts, and evergreen market takes.
- Prefer sharp, ownable ideas over safe content-calendar filler.

Available workspace evidence:
{json.dumps(context, indent=2, sort_keys=True)}

Return JSON only with this shape:
{{"topics":[{{"title":"short editorial title","topic":"category","angle":"specific thesis and direction for the writer","why_now":"why this is worth posting now","source_type":"evergreen|trend|performance|product|founder|follow_up","source_refs":["source ids when applicable"],"post_type":"founder_insight","priority":85}}]}}

Use valid workspace post types. Priority is 0-100. Do not write the posts."""


def parse_topic_ideas(raw: str, *, limit: int = 20) -> list[TopicIdea]:
    payload = json.loads(json_payload_text(raw))
    rows: Any = payload.get("topics") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Topic Scout response must contain a topics array.")
    adapter = get_x_adapter()
    ideas: list[TopicIdea] = []
    for row in rows[: max(1, limit * 2)]:
        if not isinstance(row, dict):
            continue
        title = clean(row.get("title"))
        angle = clean(row.get("angle"))
        why_now = clean(row.get("why_now"))
        if not title or not angle or not why_now:
            continue
        post_type = adapter.normalize_post_type(clean(row.get("post_type")) or "founder_insight")
        try:
            adapter.validate_post_type(post_type)
        except ValueError:
            post_type = "founder_insight"
        source_type = clean(row.get("source_type")).lower().replace("-", "_") or "evergreen"
        if source_type not in TOPIC_SOURCE_TYPES:
            source_type = "evergreen"
        refs = row.get("source_refs")
        source_refs = [clean(value) for value in refs if clean(value)] if isinstance(refs, list) else []
        try:
            priority = int(row.get("priority") or 50)
        except (TypeError, ValueError):
            priority = 50
        ideas.append(
            TopicIdea(
                title=title,
                topic=clean(row.get("topic")) or "company/product",
                angle=angle,
                why_now=why_now,
                source_type=source_type,
                source_refs=source_refs[:8],
                post_type=post_type,
                priority=max(0, min(100, priority)),
            )
        )
        if len(ideas) >= limit:
            break
    return ideas


def json_payload_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        return text
    object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if object_match:
        return object_match.group(0)
    array_match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if array_match:
        return array_match.group(0)
    raise ValueError("Topic Scout provider did not return JSON.")


def topic_idea_key(idea: TopicIdea) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", f"{idea.title} {idea.angle}".lower()))


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def compact(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
