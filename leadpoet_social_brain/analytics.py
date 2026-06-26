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
    by_pattern = _group_named_pattern_performance(posts_by_id, metrics_by_post_id, influences)
    by_hook = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="hook")
    by_structure = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="structure")
    by_tone = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="tone")
    by_cta = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="cta")
    by_formatting = _group_influence_performance(posts_by_id, metrics_by_post_id, influences, category="formatting")
    by_trend_topic = _group_trend_performance(posts_by_id, metrics_by_post_id, influences, metadata_key="topic")
    by_trend_source_type = _group_trend_performance(posts_by_id, metrics_by_post_id, influences, category_field=True)
    by_trend_urgency = _group_trend_performance(posts_by_id, metrics_by_post_id, influences, metadata_key="urgency")
    by_used_trend = _group_trend_performance(posts_by_id, metrics_by_post_id, influences, use_name=True)

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
        by_trend_topic=by_trend_topic,
        by_trend_source_type=by_trend_source_type,
        by_trend_urgency=by_trend_urgency,
        by_used_trend=by_used_trend,
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


def _group_named_pattern_performance(
    posts_by_id: dict[str, SocialPost],
    metrics_by_post_id: dict[str, EngagementMetrics],
    influences: list[PostInfluence],
) -> list[PerformanceRow]:
    selected = [
        influence for influence in influences
        if influence.influence_type == "pattern"
        and (
            influence.metadata.get("source") == "post_notes.pattern_name"
            or (
                influence.metadata.get("source") == "draft.selected_patterns"
                and influence.metadata.get("source_field") == "pattern"
            )
            or influence.metadata.get("source") is None
        )
    ]
    return _group_influence_performance(posts_by_id, metrics_by_post_id, selected)


def _group_trend_performance(
    posts_by_id: dict[str, SocialPost],
    metrics_by_post_id: dict[str, EngagementMetrics],
    influences: list[PostInfluence],
    *,
    metadata_key: str | None = None,
    category_field: bool = False,
    use_name: bool = False,
) -> list[PerformanceRow]:
    selected: dict[str, dict[str, SocialPost]] = defaultdict(dict)
    for influence in influences:
        if influence.influence_type != "trend":
            continue
        if use_name:
            key = influence.name
        elif category_field:
            key = influence.category or "unknown"
        else:
            key = str(influence.metadata.get(metadata_key or "") or "").strip()
        post = posts_by_id.get(influence.post_id)
        if key and post:
            selected[key][post.id] = post
    rows: list[PerformanceRow] = []
    for key, posts_for_key in selected.items():
        rows.extend(
            _group_performance(
                list(posts_for_key.values()),
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
    def sort_key(post: SocialPost) -> tuple[float, int, str]:
        metrics = metrics_by_post_id.get(post.id)
        return (
            metrics.engagement_rate if metrics and metrics.engagement_rate is not None else 0.0,
            metrics.total_engagements() if metrics else 0,
            post.created_at,
        )

    return sorted(
        posts,
        key=sort_key,
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
    if _enough_signal(by_post_type) and _enough_signal(by_topic):
        hints.append(
            "Early signal: posts about "
            f"{by_topic[0].key} in a {by_post_type[0].key.replace('_', ' ')} format are earning stronger engagement."
        )
    if _enough_signal(by_structure):
        hints.append(
            "Posts that perform well tend to use this shape: "
            f"{_humanize_label(by_structure[0].key)}."
        )
    elif _enough_signal(by_pattern):
        hints.append(
            "Posts that perform well tend to make one clear move: "
            f"{_humanize_label(by_pattern[0].key)}."
        )
    if _enough_signal(by_hook) and _enough_signal(by_tone):
        hints.append(
            "The stronger posts usually open with a concrete observation and keep the language plain."
        )
    return hints


def _enough_signal(rows: list[PerformanceRow]) -> bool:
    return bool(rows and rows[0].count >= 2)


def _humanize_label(label: str) -> str:
    translations = {
        "enemy_then_better_model": "name the weak default, then give a sharper replacement model",
        "claim_then_list": "make a plain claim, then show the concrete pieces behind it",
        "specific_observation": "start from a specific market or product observation",
        "plain_english_technical": "explain the technical point in plain English",
        "soft_inbound_cta": "use a low-friction invitation only when it fits",
        "short_paragraphs": "use short paragraphs that make the point easy to scan",
    }
    return translations.get(label, label.replace("_", " "))
