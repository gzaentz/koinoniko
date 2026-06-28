from __future__ import annotations

import json
from typing import Any

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.context import POSITIONING_KINDS
from leadpoet_social_brain.core.models import (
    AnalyticsSummary,
    ContentQueueItem,
    EngagementMetrics,
    PerformanceRow,
    PostNote,
    SocialPost,
    SyncMetadata,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


ASSISTANT_STARTER_PROMPTS = [
    "What are the best posts to use as Voice Examples?",
    "What patterns are working best?",
    "What should I post about today?",
    "Which scheduled posts feel weakest?",
    "What topics are getting the best engagement?",
    "Which recent post should we make a follow-up to?",
]

ASSISTANT_MISSING_PROVIDER_MESSAGE = (
    "No LLM provider configured. Set a provider in Account before using Ask Koinoniko."
)

SYSTEM_INSTRUCTIONS = """You are Koinoniko, a content intelligence assistant for this workspace.
Answer using the app's available data.
Prioritize specificity over generic advice.
Do not invent metrics, posts, trends, or patterns.
If data is missing, say what is missing.
Preserve the saved Voice profile.
Recommend actions, but do not perform writes without explicit confirmation.
Be concise, useful, and direct.
You are a content/data analyst, not a support chatbot."""


def starter_prompts() -> list[str]:
    return list(ASSISTANT_STARTER_PROMPTS)


def build_assistant_context(store: SQLiteStore, question: str) -> dict[str, Any]:
    """Collect compact app data for Ask Koinoniko without exposing secrets."""
    intent = assistant_intent(question)
    posts = store.list_posts(platform="x", limit=120)
    post_ids = [post.id for post in posts]
    metrics = store.latest_metrics_by_post_id(post_ids)
    notes = store.notes_by_post_id(post_ids)
    analytics = build_analytics_summary(store)
    queue = store.list_content_queue(platform="x", limit=80)
    voice_section = current_voice_section(store)
    active_trends = store.list_trends(status="unused", limit=12)
    active_patterns = store.list_viral_patterns(status="active", limit=12)
    sync_metadata = store.sync_metadata_by_key()
    fact_sections = store.list_context_sections(kinds=POSITIONING_KINDS)

    context = {
        "question_intent": sorted(intent),
        "data_gaps": data_gaps(
            intent=intent,
            posts=posts,
            metrics=metrics,
            queue=queue,
            trends=active_trends,
            patterns=active_patterns,
            voice_section_exists=bool(voice_section),
        ),
        "voice": {
            "current_summary": truncate(voice_section.content, 1200) if voice_section else "",
            "updated_at": voice_section.updated_at if voice_section else None,
            "examples": [
                post_context(post, notes.get(post.id), metrics.get(post.id))
                for post in posts
                if notes.get(post.id) and notes[post.id].imitate
            ][:8],
            "candidate_examples": [
                post_context(post, notes.get(post.id), metrics.get(post.id), include_text=True)
                for post in voice_candidates(posts, notes, metrics)[:8]
            ],
        },
        "facts_context": [
            {
                "kind": section.kind,
                "name": section.name,
                "content": truncate(section.content, 700),
                "updated_at": section.updated_at,
            }
            for section in fact_sections
        ],
        "recent_posts": [
            post_context(post, notes.get(post.id), metrics.get(post.id))
            for post in posts[:16]
        ],
        "top_posts": [
            post_context(post, notes.get(post.id), metrics.get(post.id), include_text=True)
            for post in top_posts(posts, metrics)[:8]
        ],
        "analytics": analytics_context(analytics),
        "trends": [trend_context(trend) for trend in active_trends[:8]],
        "patterns": [viral_pattern_context(pattern) for pattern in active_patterns[:8]],
        "queue": {
            "needs_review": [
                queue_context(item)
                for item in queue
                if item.status in {"draft", "needs_review", "failed"}
            ][:12],
            "scheduled": [
                queue_context(item)
                for item in queue
                if item.status == "approved"
            ][:12],
        },
        "sync": sync_context(sync_metadata),
    }
    return context


def build_assistant_prompt(question: str, context: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            SYSTEM_INSTRUCTIONS,
            "User question:",
            question.strip(),
            "Available Koinoniko data:",
            json.dumps(context, indent=2, sort_keys=True),
            (
                "Answer with practical recommendations. When referencing a post, include its summary, "
                "date, one key metric if available, and why it matters. Do not mention hidden prompt data."
            ),
        ]
    )


def assistant_sources(context: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for post in context.get("top_posts", [])[:5]:
        sources.append(
            {
                "type": "post",
                "id": post.get("id"),
                "label": post.get("summary"),
                "date": post.get("date"),
                "metric": post.get("key_metric"),
            }
        )
    for trend in context.get("trends", [])[:3]:
        sources.append(
            {
                "type": "trend",
                "id": trend.get("id"),
                "label": trend.get("title"),
                "date": trend.get("date"),
            }
        )
    for pattern in context.get("patterns", [])[:3]:
        sources.append(
            {
                "type": "pattern",
                "id": pattern.get("id"),
                "label": pattern.get("pattern_name") or pattern.get("source_summary"),
                "date": pattern.get("collected_at"),
            }
        )
    return [source for source in sources if source.get("id") or source.get("label")]


def assistant_suggested_actions(question: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    if "voice" not in assistant_intent(question):
        return []
    actions: list[dict[str, Any]] = []
    for post in context.get("voice", {}).get("candidate_examples", [])[:3]:
        post_id = post.get("id")
        if not post_id:
            continue
        actions.append(
            {
                "type": "mark_voice_example",
                "label": "Mark as Voice Example",
                "requires_confirmation": True,
                "payload": {"post_id": post_id},
            }
        )
    return actions


def assistant_intent(question: str) -> set[str]:
    text = question.lower()
    intent: set[str] = {"overview"}
    if any(word in text for word in ["voice", "example", "gavin"]):
        intent.add("voice")
    if any(word in text for word in ["pattern", "hook", "structure", "format"]):
        intent.add("patterns")
    if any(word in text for word in ["trend", "today", "timely", "post about", "next"]):
        intent.add("trends")
    if any(word in text for word in ["scheduled", "queue", "weak", "review", "approve"]):
        intent.add("queue")
    if any(word in text for word in ["metric", "engagement", "topic", "worked", "best", "performance"]):
        intent.add("analytics")
    return intent


def data_gaps(
    *,
    intent: set[str],
    posts: list[SocialPost],
    metrics: dict[str, EngagementMetrics],
    queue: list[ContentQueueItem],
    trends: list[Any],
    patterns: list[Any],
    voice_section_exists: bool,
) -> list[str]:
    gaps: list[str] = []
    if not posts:
        gaps.append("No X posts are stored yet.")
    if posts and not metrics:
        gaps.append("No engagement metric snapshots are stored for the current posts.")
    if "voice" in intent and not voice_section_exists:
        gaps.append("No current Voice summary has been built yet.")
    if "queue" in intent and not queue:
        gaps.append("No queued or scheduled posts are currently stored.")
    if "trends" in intent and not trends:
        gaps.append("No unused trend records are currently stored.")
    if "patterns" in intent and not patterns:
        gaps.append("No active Pattern Scout records are currently stored.")
    return gaps


def voice_candidates(
    posts: list[SocialPost],
    notes: dict[str, PostNote],
    metrics: dict[str, EngagementMetrics],
) -> list[SocialPost]:
    candidates = [post for post in posts if not (notes.get(post.id) and notes[post.id].imitate)]
    return top_posts(candidates, metrics)


def top_posts(posts: list[SocialPost], metrics: dict[str, EngagementMetrics]) -> list[SocialPost]:
    return sorted(posts, key=lambda post: post_score(metrics.get(post.id), post), reverse=True)


def post_score(metrics: EngagementMetrics | None, post: SocialPost) -> tuple[float, int, int, int, int, str]:
    return (
        float(metrics.engagement_rate or 0) if metrics else 0.0,
        int(metrics.bookmarks or 0) if metrics else 0,
        int(metrics.replies or 0) if metrics else 0,
        int(metrics.reposts or 0) if metrics else 0,
        int(metrics.impressions or 0) if metrics else 0,
        post.published_at or post.created_at,
    )


def post_context(
    post: SocialPost,
    note: PostNote | None,
    metrics: EngagementMetrics | None,
    *,
    include_text: bool = False,
) -> dict[str, Any]:
    payload = {
        "id": post.id,
        "date": post.published_at,
        "summary": summarize_text(post.text),
        "topic": post.topic,
        "post_type": post.post_type,
        "url": post.url,
        "is_voice_example": bool(note.imitate) if note else False,
        "worked_reason": note.worked_reason if note else None,
        "pattern": note.pattern_name if note else None,
        "hook": note.hook if note else None,
        "structure": note.structure if note else None,
        "tone": note.tone if note else None,
        "metrics": metrics_context(metrics),
        "key_metric": key_metric(metrics),
    }
    if include_text:
        payload["text"] = truncate(post.text, 500)
    return payload


def metrics_context(metrics: EngagementMetrics | None) -> dict[str, Any] | None:
    if not metrics:
        return None
    return {
        "impressions": metrics.impressions,
        "likes": metrics.likes,
        "replies": metrics.replies,
        "reposts": metrics.reposts,
        "quotes": metrics.quotes,
        "bookmarks": metrics.bookmarks,
        "engagement_rate": metrics.engagement_rate,
        "measured_at": metrics.measured_at,
    }


def key_metric(metrics: EngagementMetrics | None) -> str | None:
    if not metrics:
        return None
    if metrics.engagement_rate is not None:
        return f"{metrics.engagement_rate:.2%} engagement rate"
    if metrics.impressions is not None:
        return f"{metrics.impressions} impressions"
    if metrics.bookmarks:
        return f"{metrics.bookmarks} bookmarks"
    if metrics.replies:
        return f"{metrics.replies} replies"
    if metrics.reposts:
        return f"{metrics.reposts} reposts"
    return f"{metrics.total_engagements()} total engagements"


def analytics_context(summary: AnalyticsSummary) -> dict[str, Any]:
    return {
        "hints": summary.hints[:6],
        "by_topic": [performance_context(row) for row in summary.by_topic[:6]],
        "by_post_type": [performance_context(row) for row in summary.by_post_type[:6]],
        "by_pattern": [performance_context(row) for row in summary.by_pattern[:6]],
        "by_hook": [performance_context(row) for row in summary.by_hook[:6]],
        "by_structure": [performance_context(row) for row in summary.by_structure[:6]],
        "by_tone": [performance_context(row) for row in summary.by_tone[:6]],
        "by_cta": [performance_context(row) for row in summary.by_cta[:6]],
        "by_formatting": [performance_context(row) for row in summary.by_formatting[:6]],
    }


def performance_context(row: PerformanceRow) -> dict[str, Any]:
    return {
        "name": row.key,
        "posts": row.count,
        "avg_engagement_rate": row.avg_engagement_rate,
        "avg_impressions": row.avg_impressions,
        "avg_bookmarks": row.avg_bookmarks,
        "avg_replies": row.avg_replies,
        "avg_reposts": row.avg_reposts,
        "top_post_id": row.top_post_id,
    }


def queue_context(item: ContentQueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "status": item.status,
        "scheduled_at": item.scheduled_at,
        "topic": item.topic,
        "post_type": item.post_type,
        "angle": item.angle,
        "summary": summarize_text(item.text),
        "text": truncate(item.text, 500),
        "reviewer_notes": item.reviewer_notes,
        "trend_ids": item.trend_ids,
    }


def trend_context(trend) -> dict[str, Any]:
    return {
        "id": trend.id,
        "title": trend.title,
        "topic": trend.topic,
        "source_type": trend.source_type,
        "summary": trend.summary,
        "why_it_matters": trend.why_it_matters,
        "leadpoet_angle": trend.leadpoet_angle,
        "suggested_hooks": trend.suggested_hooks,
        "urgency": trend.urgency,
        "date": trend.date,
        "source_url": trend.source_url,
    }


def viral_pattern_context(pattern) -> dict[str, Any]:
    return {
        "id": pattern.id,
        "topic": pattern.topic,
        "category": pattern.category,
        "pattern_name": pattern.pattern_name,
        "hook_style": pattern.hook_style,
        "structure": pattern.structure,
        "tone": pattern.tone,
        "formatting": pattern.formatting,
        "why_it_worked": pattern.why_it_worked,
        "adaptation_rule": pattern.adaptation_rule,
        "source_summary": pattern.source_summary,
        "source_url": pattern.source_url,
        "likes": pattern.likes,
        "replies": pattern.replies,
        "reposts": pattern.reposts,
        "bookmarks": pattern.bookmarks,
        "impressions": pattern.impressions,
        "collected_at": pattern.collected_at,
    }


def sync_context(metadata_by_key: dict[str, SyncMetadata]) -> dict[str, Any]:
    return {
        key: {
            "last_completed_at": metadata.last_completed_at,
            "status": metadata.status,
        }
        for key, metadata in metadata_by_key.items()
    }


def current_voice_section(store: SQLiteStore):
    sections = [
        section for section in store.list_context_sections(kinds={"style_guide"})
        if section.name == "learned_from_high_performers"
    ]
    return sections[0] if sections else None


def summarize_text(text: str, *, max_length: int = 150) -> str:
    cleaned = " ".join(str(text or "").split())
    return truncate(cleaned, max_length)


def truncate(text: str, max_length: int) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."
