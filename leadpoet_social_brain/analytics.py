from __future__ import annotations

from collections import defaultdict

from leadpoet_social_brain.core.models import AnalyticsSummary, EngagementMetrics, PerformanceRow, PostInfluence, SocialPost
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


def build_analytics_summary(store: SQLiteStore) -> AnalyticsSummary:
    posts = store.list_posts(platform="x", limit=1000)
    metrics_by_post_id = store.latest_metrics_by_post_id([post.id for post in posts])
    influences = store.list_post_influences([post.id for post in posts])
    posts_by_id = {post.id: post for post in posts}

    by_post_type = _group_performance(posts, metrics_by_post_id, key_fn=lambda post: post.post_type)
    by_topic = _group_performance(posts, metrics_by_post_id, key_fn=lambda post: post.topic)
    by_pattern = _group_influence_performance(posts_by_id, metrics_by_post_id, influences)
    by_hook = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="hook")
    by_structure = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="structure")
    by_tone = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="tone")
    by_cta = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="cta")
    by_formatting = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="formatting")

    best_examples = _best_posts(posts, metrics_by_post_id)[:5]
    hints = _build_hints(by_post_type, by_topic, by_pattern, by_hook, by_structure, by_tone)
    return AnalyticsSummary(
        by_post_type=by_post_type,
        by_topic=by_topic,
        by_pattern=by_pattern,
        by_hook=by_hook,
        by_structure=by_structure,
        by_tone=by_tone,
        by_cta=by_cta,
        by_formatting=by_formatting,
        best_examples=best_examples,
        hints=hints,
    )


def _group_performance(posts, metrics_by_post_id, *, key_fn) -> list[PerformanceRow]:
    grouped: dict[str, list[tuple[SocialPost, EngagementMetrics | None]]] = defaultdict(list)
    for post in posts:
        grouped[key_fn(post)].append((post, metrics_by_post_id.get(post.id)))

    rows: list[PerformanceRow] = []
    for key, values in grouped.items():
        rates = [
            metrics.engagement_rate
            for _, metrics in values
            if metrics and metrics.engagement_rate is not None
        ]
        totals = [
            metrics.total_engagements()
            for _, metrics in values
            if metrics is not None
        ]
        impressions = [metrics.impressions for _, metrics in values if metrics and metrics.impressions is not None]
        bookmarks = [metrics.bookmarks or 0 for _, metrics in values if metrics is not None]
        replies = [metrics.replies or 0 for _, metrics in values if metrics is not None]
        reposts = [metrics.reposts or 0 for _, metrics in values if metrics is not None]
        top_post = _best_posts(
            [post for post, _ in values],
            {post.id: metrics for post, metrics in values if metrics is not None},
        )
        rows.append(
            PerformanceRow(
                key=key,
                count=len(values),
                avg_engagement_rate=(sum(rates) / len(rates)) if rates else None,
                avg_total_engagements=(sum(totals) / len(totals)) if totals else 0.0,
                avg_impressions=(sum(impressions) / len(impressions)) if impressions else 0.0,
                avg_bookmarks=(sum(bookmarks) / len(bookmarks)) if bookmarks else 0.0,
                avg_replies=(sum(replies) / len(replies)) if replies else 0.0,
                avg_reposts=(sum(reposts) / len(reposts)) if reposts else 0.0,
                top_post_id=top_post[0].id if top_post else None,
            )
        )
    return sorted(
        rows,
        key=lambda row: (
            row.avg_engagement_rate or 0.0,
            row.avg_total_engagements,
            row.count,
        ),
        reverse=True,
    )


def _group_influence_performance(
    posts_by_id: dict[str, SocialPost],
    metrics_by_post_id: dict[str, EngagementMetrics],
    influences: list[PostInfluence],
    *,
    category: str | None = None,
) -> list[PerformanceRow]:
    selected: dict[str, dict[str, SocialPost]] = defaultdict(dict)
    for influence in influences:
        if influence.influence_type != "pattern":
            continue
        if category is not None and influence.category != category:
            continue
        post = posts_by_id.get(influence.post_id)
        if post:
            selected[influence.name][post.id] = post
    grouped_posts = [
        (key, list(posts_by_id_for_key.values()))
        for key, posts_by_id_for_key in selected.items()
    ]
    rows: list[PerformanceRow] = []
    for key, posts_for_key in grouped_posts:
        rows.extend(
            _group_performance(
                posts_for_key,
                metrics_by_post_id,
                key_fn=lambda _post, key=key: key,
            )
        )
    return sorted(
        rows,
        key=lambda row: (
            row.avg_engagement_rate or 0.0,
            row.avg_bookmarks,
            row.avg_replies,
            row.avg_reposts,
            row.avg_impressions,
        ),
        reverse=True,
    )


def _best_posts(posts: list[SocialPost], metrics_by_post_id: dict[str, EngagementMetrics]) -> list[SocialPost]:
    return sorted(
        posts,
        key=lambda post: (
            metrics_by_post_id.get(post.id).engagement_rate if metrics_by_post_id.get(post.id) else 0.0,
            metrics_by_post_id.get(post.id).total_engagements() if metrics_by_post_id.get(post.id) else 0,
            post.created_at,
        ),
        reverse=True,
    )


def _build_hints(
    by_post_type: list[PerformanceRow],
    by_topic: list[PerformanceRow],
    by_pattern: list[PerformanceRow],
    by_hook: list[PerformanceRow],
    by_structure: list[PerformanceRow],
    by_tone: list[PerformanceRow],
) -> list[str]:
    hints: list[str] = []
    if by_post_type:
        hints.append(f"Best post type so far: {by_post_type[0].key}")
    if by_topic:
        hints.append(f"Best topic so far: {by_topic[0].key}")
    if by_pattern:
        hints.append(f"Best pattern so far: {by_pattern[0].key}")
    if by_hook:
        hints.append(f"Best hook so far: {by_hook[0].key}")
    if by_structure:
        hints.append(f"Best structure so far: {by_structure[0].key}")
    if by_tone:
        hints.append(f"Best tone so far: {by_tone[0].key}")
    if not hints:
        hints.append("No performance history yet. Use manual notes and imitation flags for the first few drafts.")
    return hints
