from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    DraftRequest,
    GeneratedDraft,
    PostInfluence,
    QueuedPostVersion,
    SocialPost,
    TrendItem,
    utc_now,
)
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    build_best_writer_prompt,
    build_evaluation_prompt,
    build_strategy_prompt,
    clean_generated_text,
    parse_evaluation_result,
    parse_strategy_summary,
)
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.platforms.x_api import XPostedTweet, XWriteClient
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider, resolve_model_routing
from leadpoet_social_brain.skills import generation_skills
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.voice import avoid_phrases_prompt_section, load_avoid_phrases


LOCAL_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PlanWeekResult:
    queued: list[ContentQueueItem]
    warnings: list[str]


@dataclass(frozen=True)
class CreatePostResult:
    item: ContentQueueItem
    draft: GeneratedDraft
    warnings: list[str]


@dataclass(frozen=True)
class PublishResult:
    queue_item: ContentQueueItem
    posted: XPostedTweet | None
    error: str | None = None


def plan_week_x(
    store: SQLiteStore,
    generator: SocialBrainGenerator,
    *,
    week_start: str,
    posts: int,
    use_trends: bool,
    trend_window_days: int,
    topic_mix: str | None,
    cadence: str,
    default_post_type: str,
    variants_per_slot: int,
    allow_evergreen: bool = False,
    best_version: bool = False,
    use_patterns: bool = False,
) -> PlanWeekResult:
    if posts <= 0:
        raise ValueError("--posts must be greater than 0.")
    get_x_adapter().validate_post_type(default_post_type)
    start_date = date.fromisoformat(week_start)
    topics = expand_topic_mix(topic_mix, posts)
    scheduled_times = schedule_slots(start_date, posts=posts, cadence=cadence)
    trends = recent_unused_trends(store, window_days=trend_window_days) if use_trends else []
    warnings: list[str] = []
    if use_trends and not trends and not allow_evergreen:
        raise ValueError("No recent unused trends available. Add trends or pass --allow-evergreen.")
    if use_trends and len(trends) < posts and not allow_evergreen:
        raise ValueError(
            f"Only {len(trends)} recent unused trend(s) available for {posts} post(s). "
            "Add trends, lower --posts, or pass --allow-evergreen."
        )
    if use_trends and not trends:
        warnings.append("No recent unused trends found. Planning evergreen authority posts.")

    used_trends: set[str] = set()
    queued: list[ContentQueueItem] = []
    for index in range(posts):
        topic = topics[index % len(topics)]
        trend = pick_trend_for_topic(trends, topic=topic, used_trend_ids=used_trends) if use_trends else None
        if trend:
            used_trends.add(trend.id)
        elif use_trends:
            warnings.append(f"No unused trend left for slot {index + 1}; planned evergreen post.")
        angle = trend.leadpoet_angle if trend else evergreen_angle(topic, index)
        request = DraftRequest(
            platform="x",
            topic=topic,
            post_type=default_post_type,
            angle=angle,
            extra_context="Weekly content calendar suggestion. Make it worth posting on its own.",
            use_trends=bool(trend),
            trend_ids=[trend.id] if trend else [],
            use_patterns=use_patterns,
        )
        drafts = generator.generate(
            request,
            variants=max(1, variants_per_slot),
            best_version=best_version,
        )
        draft = drafts[0]
        item = store.save_content_queue_item(
            ContentQueueItem(
                platform="x",
                draft_id=draft.id,
                text=draft.text,
                topic=topic,
                post_type=default_post_type,
                angle=angle,
                trend_ids=[trend.id] if trend else [],
                scheduled_at=scheduled_times[index].isoformat(),
                status="needs_review",
                reviewer_notes=queue_notes(draft.explanation, trend),
            )
        )
        queued.append(item)
    return PlanWeekResult(queued=queued, warnings=warnings)


def create_post_x(
    store: SQLiteStore,
    generator: SocialBrainGenerator,
    *,
    instruction: str,
    topic: str = "Leadpoet",
    post_type: str = "founder_insight",
    use_trends: bool = False,
    best_version: bool = False,
    scheduled_at: str | None = None,
    extra_context: str | None = None,
    use_patterns: bool = False,
    pattern_ids: list[str] | None = None,
    trend_ids: list[str] | None = None,
) -> CreatePostResult:
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("Post idea / instruction is required.")
    topic = topic.strip() or "Leadpoet"
    post_type = post_type.strip() or "founder_insight"
    get_x_adapter().validate_post_type(post_type)

    warnings: list[str] = []
    selected_trends = [
        trend for trend_id in trend_ids or []
        if (trend := store.get_trend_item(trend_id)) is not None
    ]
    missing_trend_count = len(trend_ids or []) - len(selected_trends)
    if missing_trend_count:
        warnings.append("Some selected trends were not found and were skipped.")
    if not selected_trends and use_trends:
        selected_trends = store.relevant_trends(topic=topic, angle=instruction, limit=3)
    if use_trends and not selected_trends:
        warnings.append("No relevant unused trends found. Created an evergreen post instead.")

    context_parts = [
        "One-off/interim Content Reviewer post. Write a complete standalone X post.",
        f"User instruction: {instruction}",
    ]
    if extra_context and extra_context.strip():
        context_parts.append(f"Extra instruction: {extra_context.strip()}")
    request = DraftRequest(
        platform="x",
        topic=topic,
        post_type=post_type,
        angle=instruction,
        extra_context="\n".join(context_parts),
        use_trends=bool(selected_trends),
        trend_ids=[trend.id for trend in selected_trends],
        use_patterns=use_patterns,
        pattern_ids=pattern_ids or [],
    )
    draft = generator.generate(request, variants=1, best_version=best_version)[0]
    trend_ids = selected_trend_ids(draft, selected_trends)
    item = store.save_content_queue_item(
        ContentQueueItem(
            platform="x",
            draft_id=draft.id,
            text=draft.text,
            topic=topic,
            post_type=post_type,
            angle=instruction,
            trend_ids=trend_ids,
            scheduled_at=(scheduled_at or "").strip(),
            status="needs_review",
            reviewer_notes=create_post_notes(
                draft,
                instruction=instruction,
                trends=[trend for trend in selected_trends if trend.id in set(trend_ids)],
                best_version=best_version,
            ),
        )
    )
    return CreatePostResult(item=item, draft=draft, warnings=warnings)


def approve_queue_item(store: SQLiteStore, queue_id: str) -> ContentQueueItem:
    item = require_queue_item(store, queue_id)
    return store.save_content_queue_item(
        ContentQueueItem(
            **{
                **item.__dict__,
                "status": "approved",
                "approved_at": utc_now(),
                "failure_reason": None,
            }
        )
    )


def revise_queued_post(
    store: SQLiteStore,
    provider: DraftProvider,
    *,
    queue_id: str,
    instruction: str,
    variants: int = 3,
    best_version: bool = False,
) -> list[QueuedPostVersion]:
    item = require_queue_item(store, queue_id)
    if not instruction.strip():
        raise ValueError("Edit instruction is required.")
    adapter = get_x_adapter()
    bundle = ContextManager(store).build_bundle(
        platform=item.platform,
        topic=item.topic,
        post_type=item.post_type,
        max_examples=4,
    )
    skills = generation_skills(platform=item.platform, post_type=item.post_type)
    avoid_phrases = load_avoid_phrases(store)
    examples = "\n\n".join(post.text for post in bundle.examples) or "No examples selected."
    context = "\n".join(
        f"- {section.kind}/{section.name}: {section.content}"
        for section in [*bundle.style_sections, *bundle.positioning_sections]
    )
    skill_text = "\n\n".join(f"## {skill.path}\n{skill.content}" for skill in skills)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    prompt = f"""Revise this queued X post for Leadpoet.

Original post:
{item.text}

User edit instruction:
{instruction.strip()}

Topic: {item.topic}
Post type: {item.post_type}
Angle: {item.angle or "none"}

Relevant Gavin/Leadpoet examples:
{examples}

Relevant context:
{context or "- No context available."}

Useful performance guidance:
{analytics or "- No analytics hints available."}

Writing and review instructions:
{skill_text}

{avoid_phrases_prompt_section(avoid_phrases)}

Return only revised post text variants. Do not explain. Do not approve or post anything."""
    model_routing = resolve_model_routing(store, provider)
    if best_version:
        request = DraftRequest(
            platform=item.platform,
            topic=item.topic,
            post_type=item.post_type,
            angle=item.angle,
            extra_context=(
                "Revise this queued post into the strongest final version. "
                f"Original post: {item.text}\nInstruction: {instruction.strip()}"
            ),
            trend_ids=item.trend_ids,
        )
        trends = [
            trend for trend_id in item.trend_ids
            if (trend := store.get_trend_item(trend_id)) is not None
        ]
        strategy_raw = generate_with_provider(
            provider,
            build_strategy_prompt(request, bundle, trends=trends, patterns=[], avoid_phrases=avoid_phrases),
            variants=1,
            model=model_routing.strategist,
        )[0]
        strategy_summary = parse_strategy_summary(strategy_raw)
        raw_options = generate_with_provider(
            provider,
            build_best_writer_prompt(
                request,
                bundle,
                skills=skills,
                trends=trends,
                patterns=[],
                avoid_phrases=avoid_phrases,
                strategy_summary=strategy_summary,
                max_chars=adapter.rules.default_max_chars,
            ),
            variants=3,
            model=model_routing.natural_writer,
        )
        options = [
            text for raw in raw_options
            if (text := clean_generated_text(raw, max_chars=adapter.rules.default_max_chars))
        ]
        if not options:
            raise ValueError("Provider did not return usable revision text.")
        evaluation_raw = generate_with_provider(
            provider,
            build_evaluation_prompt(
                request,
                skills=skills,
                strategy_summary=strategy_summary,
                draft_options=options,
                avoid_phrases=avoid_phrases,
            ),
            variants=1,
            model=model_routing.evaluator,
        )[0]
        final_text, _, _ = parse_evaluation_result(evaluation_raw, options)
        raw_versions = [final_text]
    else:
        raw_versions = generate_with_provider(
            provider,
            prompt,
            variants=max(1, min(variants, 3)),
            model=model_routing.default,
        )
    saved: list[QueuedPostVersion] = []
    for raw in raw_versions:
        text = clean_generated_text(raw, max_chars=adapter.rules.default_max_chars)
        adapter.validate_text(text)
        saved.append(
            store.save_queued_post_version(
                QueuedPostVersion(
                    queue_id=item.id,
                    text=text,
                    edit_type="llm",
                    edit_instruction=instruction.strip(),
                )
            )
        )
    return saved


def accept_queued_post_version(store: SQLiteStore, *, version_id: str) -> ContentQueueItem:
    version = store.get_queued_post_version(version_id)
    if not version:
        raise ValueError(f"No queued post version found for id {version_id}")
    item = require_queue_item(store, version.queue_id)
    store.save_queued_post_version(
        QueuedPostVersion(
            queue_id=item.id,
            text=item.text,
            edit_type="manual",
            edit_instruction="previous text before accepting revision",
        )
    )
    return store.save_content_queue_item(
        ContentQueueItem(
            **{
                **item.__dict__,
                "text": version.text,
                "status": "needs_review" if item.status == "approved" else item.status,
                "approved_at": None if item.status == "approved" else item.approved_at,
            }
        )
    )


def approve_week_x(store: SQLiteStore, *, week_start: str) -> list[ContentQueueItem]:
    start = datetime.combine(date.fromisoformat(week_start), time.min, LOCAL_TZ)
    end = start + timedelta(days=7)
    items = store.list_content_queue(
        platform="x",
        status="needs_review",
        scheduled_from=start.isoformat(),
        scheduled_to=end.isoformat(),
        limit=100,
    )
    return [approve_queue_item(store, item.id) for item in items]


def publish_due_x(
    store: SQLiteStore,
    client: XWriteClient,
    *,
    now: str | None = None,
    limit: int = 20,
    author: str = "Gavin",
) -> list[PublishResult]:
    results: list[PublishResult] = []
    now_dt = _parse_datetime(now) if now else datetime.now(timezone.utc)
    approved = store.list_content_queue(platform="x", status="approved", limit=1000)
    due = [
        item for item in approved
        if (_parse_datetime(item.scheduled_at) or datetime.max.replace(tzinfo=timezone.utc)) <= now_dt
    ][:limit]
    for item in due:
        results.append(post_queue_item_x(store, client, item=item, author=author))
    return results


def post_queue_item_x(
    store: SQLiteStore,
    client: XWriteClient,
    *,
    item: ContentQueueItem,
    author: str = "Gavin",
) -> PublishResult:
    if item.status != "approved":
        raise ValueError("Queued post must be approved before posting.")
    try:
        get_x_adapter().validate_text(item.text)
        posted = client.post_tweet(item.text)
        now = utc_now()
        post = store.save_post(
            SocialPost(
                platform="x",
                text=item.text,
                url=posted.url,
                published_at=now,
                author=author,
                topic=item.topic,
                post_type=item.post_type,
                status="published",
                source_draft_id=item.draft_id,
            )
        )
        preserve_queue_influences(store, post.id, item)
        saved_item = store.save_content_queue_item(
            ContentQueueItem(
                **{
                    **item.__dict__,
                    "status": "posted",
                    "posted_at": now,
                    "x_post_id": posted.id,
                    "x_url": posted.url,
                    "failure_reason": None,
                }
            )
        )
        return PublishResult(queue_item=saved_item, posted=posted)
    except Exception as exc:  # noqa: BLE001 - queue posting should record failure.
        saved_item = store.save_content_queue_item(
            ContentQueueItem(
                **{
                    **item.__dict__,
                    "status": "failed",
                    "failure_reason": str(exc),
                }
            )
        )
        return PublishResult(queue_item=saved_item, posted=None, error=str(exc))


def preserve_queue_influences(store: SQLiteStore, post_id: str, item: ContentQueueItem) -> None:
    if item.draft_id:
        draft = store.get_generated_draft(item.draft_id)
        if draft:
            store.link_post_to_draft(post_id, draft)
    influences: list[PostInfluence] = []
    for trend_id in item.trend_ids:
        trend = store.get_trend_item(trend_id)
        if not trend:
            continue
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=item.draft_id,
                influence_type="trend",
                category=trend.source_type,
                name=trend.title,
                ref_id=trend.id,
                metadata={
                    "topic": trend.topic,
                    "urgency": trend.urgency,
                    "source": "content_queue.trend_ids",
                },
            )
        )
        store.mark_trend_status(trend.id, "used")
    store.save_post_influences(influences)


def require_queue_item(store: SQLiteStore, queue_id: str) -> ContentQueueItem:
    item = store.get_content_queue_item(queue_id)
    if not item:
        raise ValueError(f"No queued post found for id {queue_id}")
    return item


def expand_topic_mix(topic_mix: str | None, posts: int) -> list[str]:
    if not topic_mix:
        defaults = [
            "sales LLMs",
            "sales LLMs",
            "Bittensor",
            "sales LLMs",
            "product",
            "sales LLMs",
            "Bittensor",
        ]
        if posts <= len(defaults):
            return defaults[:posts]
        return [defaults[index % len(defaults)] for index in range(posts)]
    topics: list[str] = []
    for raw_part in topic_mix.split(","):
        part = raw_part.strip()
        if not part:
            continue
        topic, separator, count_text = part.partition(":")
        if not separator:
            raise ValueError("--topic-mix entries must look like topic:count.")
        count = int(count_text.strip())
        topics.extend([topic.strip()] * count)
    if not topics:
        raise ValueError("--topic-mix did not include any topics.")
    while len(topics) < posts:
        topics.extend(topics[: posts - len(topics)])
    return topics[:posts]


def schedule_slots(start_date: date, *, posts: int, cadence: str) -> list[datetime]:
    slots: list[datetime] = []
    day_offset = 0
    while len(slots) < posts:
        slot_date = start_date + timedelta(days=day_offset)
        day_offset += 1
        if cadence == "weekday_mornings" and slot_date.weekday() >= 5:
            continue
        if cadence not in {"daily_mornings", "weekday_mornings"}:
            raise ValueError("Unsupported --cadence. Use daily_mornings or weekday_mornings.")
        slots.append(datetime.combine(slot_date, time(10, 0), LOCAL_TZ))
    return slots


def recent_unused_trends(store: SQLiteStore, *, window_days: int) -> list[TrendItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    trends = store.list_trends(status="unused", limit=1000)
    return [
        trend for trend in trends
        if (parsed := parse_trend_datetime(trend)) is not None and parsed >= cutoff
    ]


def parse_trend_datetime(trend: TrendItem) -> datetime | None:
    for value in [trend.date, trend.created_at]:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def pick_trend_for_topic(
    trends: list[TrendItem],
    *,
    topic: str,
    used_trend_ids: set[str],
) -> TrendItem | None:
    available = [trend for trend in trends if trend.id not in used_trend_ids]
    if not available:
        return None
    lowered_topic = topic.lower()
    for trend in available:
        haystack = " ".join([trend.topic, trend.title, trend.summary, trend.leadpoet_angle]).lower()
        if lowered_topic in haystack:
            return trend
    return available[0]


def evergreen_angle(topic: str, index: int) -> str:
    defaults = [
        f"make one sharp founder-led point about {topic}",
        f"explain the weak default in {topic} and the better Leadpoet view",
        f"turn a practical {topic} lesson into an authority-building post",
    ]
    return defaults[index % len(defaults)]


def queue_notes(explanation: str, trend: TrendItem | None) -> str:
    pieces = [f"Why worth publishing: {explanation}"]
    if trend:
        pieces.append(f"Trend/source used: {trend.title}")
    return "\n".join(pieces)


def create_post_notes(
    draft: GeneratedDraft,
    *,
    instruction: str,
    trends: list[TrendItem],
    best_version: bool,
) -> str:
    pieces = [
        f"Focus: {instruction}",
        f"Why worth publishing: {draft.explanation}",
        f"Quality mode: {'Best Version' if best_version else 'Standard'}",
    ]
    if trends:
        pieces.append("Trend/source used: " + ", ".join(trend.title for trend in trends))
    return "\n".join(pieces)


def selected_trend_ids(draft: GeneratedDraft, fallback: list[TrendItem]) -> list[str]:
    snapshot = draft.context_snapshot or {}
    selected = snapshot.get("selected_trends") if isinstance(snapshot, dict) else []
    ids = [
        str(trend.get("id"))
        for trend in selected or []
        if isinstance(trend, dict) and trend.get("id")
    ]
    return ids or [trend.id for trend in fallback]


def weekly_report_text(store: SQLiteStore, *, since_days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    posts = [
        post for post in store.list_posts(platform="x", status="published", limit=1000)
        if (_parse_datetime(post.published_at) or _parse_datetime(post.created_at) or cutoff) >= cutoff
    ]
    metrics = store.latest_metrics_by_post_id([post.id for post in posts])
    ranked = sorted(
        posts,
        key=lambda post: metrics.get(post.id).total_engagements() if metrics.get(post.id) else 0,
        reverse=True,
    )
    topics = Counter(post.topic for post in posts)
    post_types = Counter(post.post_type for post in posts)
    lines = [f"Weekly X report: last {since_days} days", ""]
    lines.append("Top posts:")
    for post in ranked[:5]:
        metric = metrics.get(post.id)
        score = metric.total_engagements() if metric else 0
        lines.append(
            f"- {post.id}: {score} engagements | {post.topic} | "
            f"{post.text[:120].replace(chr(10), ' ')}"
        )
    lines.append("")
    lines.append("Weakest posts:")
    for post in list(reversed(ranked[-5:])):
        metric = metrics.get(post.id)
        score = metric.total_engagements() if metric else 0
        lines.append(
            f"- {post.id}: {score} engagements | {post.topic} | "
            f"{post.text[:120].replace(chr(10), ' ')}"
        )
    lines.append("")
    lines.append("Best topics:")
    lines.extend(f"- {topic}: {count} post(s)" for topic, count in topics.most_common(5))
    lines.append("")
    lines.append("Best post types:")
    lines.extend(f"- {post_type}: {count} post(s)" for post_type, count in post_types.most_common(5))
    lines.append("")
    lines.append("Next week:")
    if topics:
        top_topics = ", ".join(topic for topic, _ in topics.most_common(3))
        lines.append(f"- Do more of the topics with signal: {top_topics}.")
    lines.append("- Keep using specific examples, buyer timing, and plain founder language.")
    lines.append("- Avoid generic AI commentary unless it connects to a concrete Leadpoet point.")
    return "\n".join(lines)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
