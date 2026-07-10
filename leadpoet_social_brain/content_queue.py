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
    TopicIdea,
    TrendItem,
    utc_now,
)
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.editorial import dedupe_editorial_trends
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    active_writing_rules_for_prompt,
    build_best_writer_prompt,
    build_evaluation_prompt,
    build_strategy_prompt,
    fit_generated_text_to_limit,
    normalize_length_mode,
    parse_evaluation_result,
    parse_strategy_summary,
    resolve_length_max_chars,
    skills_with_review_rules,
)
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.platforms.x_api import XPostedTweet, XWriteClient
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider, resolve_model_routing
from leadpoet_social_brain.skills import generation_skills
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.voice import load_avoid_phrases


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
    use_patterns: bool = False,
    length_mode: str = "standard",
    topic_ideas: list[TopicIdea] | None = None,
    extra_instruction: str | None = None,
) -> PlanWeekResult:
    _ = variants_per_slot
    if posts <= 0:
        raise ValueError("--posts must be greater than 0.")
    requested_posts = posts
    get_x_adapter().validate_post_type(default_post_type)
    start_date = date.fromisoformat(week_start)
    topics = expand_topic_mix(topic_mix, posts)
    selected_topic_ideas = [idea for idea in (topic_ideas or []) if idea.status == "suggested"][:posts]
    if topic_ideas is not None:
        if not selected_topic_ideas:
            raise ValueError("No grounded topic ideas are available for Autopilot planning.")
        posts = len(selected_topic_ideas)
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
    if topic_ideas is not None and posts < requested_posts:
        warnings.append(
            f"Planned {posts} grounded topic idea(s) instead of {requested_posts}; "
            "Autopilot did not fill the remaining slots with generic fallback copy."
        )

    used_trends: set[str] = set()
    queued: list[ContentQueueItem] = []
    for index in range(posts):
        topic_idea = selected_topic_ideas[index] if index < len(selected_topic_ideas) else None
        topic = topic_idea.topic if topic_idea else topics[index % len(topics)]
        post_type = topic_idea.post_type if topic_idea else default_post_type
        referenced_trends = [
            trend
            for ref in (topic_idea.source_refs if topic_idea else [])
            if (trend := store.get_trend_item(ref)) is not None and trend.id not in used_trends
        ]
        trend = referenced_trends[0] if referenced_trends else (
            pick_trend_for_topic(trends, topic=topic, used_trend_ids=used_trends) if use_trends else None
        )
        if trend:
            used_trends.add(trend.id)
        elif use_trends:
            warnings.append(f"No unused trend left for slot {index + 1}; planned evergreen post.")
        angle = topic_idea.angle if topic_idea else (trend.leadpoet_angle if trend else evergreen_angle(topic, index))
        context_lines = ["Weekly content calendar suggestion. Make it worth posting on its own."]
        if topic_idea:
            context_lines.append(f"Why this topic is worth posting now: {topic_idea.why_now}")
        if extra_instruction and extra_instruction.strip():
            context_lines.append(f"Weekly plan instruction: {extra_instruction.strip()}")
        request = DraftRequest(
            platform="x",
            topic=topic,
            post_type=post_type,
            angle=angle,
            extra_context="\n".join(context_lines),
            length_mode=length_mode,
            use_trends=bool(trend),
            trend_ids=[trend.id] if trend else [],
            use_patterns=use_patterns,
            prompt_profile="natural",
            editorial_value_mode=topic_idea.reader_value_mode if topic_idea else None,
        )
        drafts = generator.generate(
            request,
            variants=1,
        )
        draft = drafts[0]
        quality_warnings = draft_quality_warnings(draft)
        warnings.extend(f"Slot {index + 1}: Quality warning: {warning}" for warning in quality_warnings)
        item = store.save_content_queue_item(
            ContentQueueItem(
                platform="x",
                draft_id=draft.id,
                topic_idea_id=topic_idea.id if topic_idea else None,
                text=draft.text,
                topic=topic,
                post_type=post_type,
                angle=angle,
                trend_ids=[trend.id] if trend else [],
                scheduled_at=scheduled_times[index].isoformat(),
                status="needs_review",
                reviewer_notes=queue_notes(draft.explanation, trend, quality_warnings=quality_warnings),
            )
        )
        queued.append(item)
        if topic_idea:
            store.mark_topic_idea_status(topic_idea.id, "used")
    return PlanWeekResult(queued=queued, warnings=warnings)


def create_post_x(
    store: SQLiteStore,
    generator: SocialBrainGenerator,
    *,
    instruction: str,
    topic: str = "uncategorized",
    post_type: str = "founder_insight",
    use_trends: bool = False,
    scheduled_at: str | None = None,
    extra_context: str | None = None,
    use_patterns: bool = False,
    pattern_ids: list[str] | None = None,
    trend_ids: list[str] | None = None,
    length_mode: str = "standard",
    topic_idea_id: str | None = None,
) -> CreatePostResult:
    instruction = instruction.strip()
    topic_idea = store.get_topic_idea(topic_idea_id) if topic_idea_id else None
    if topic_idea and not instruction:
        instruction = topic_idea.angle
    if not instruction:
        raise ValueError("Post idea / instruction is required.")
    topic = topic_idea.topic if topic_idea else (topic.strip() or "uncategorized")
    post_type = topic_idea.post_type if topic_idea else (post_type.strip() or "founder_insight")
    adapter = get_x_adapter()
    post_type = adapter.normalize_post_type(post_type)
    adapter.validate_post_type(post_type)

    warnings: list[str] = []
    if topic_idea_id and not topic_idea:
        warnings.append("The selected topic idea was not found; used the typed instruction instead.")
    selected_trends = [
        trend for trend_id in trend_ids or []
        if (trend := store.get_trend_item(trend_id)) is not None
    ]
    missing_trend_count = len(trend_ids or []) - len(selected_trends)
    if topic_idea:
        selected_ids = {trend.id for trend in selected_trends}
        selected_trends.extend(
            trend
            for ref in topic_idea.source_refs
            if (trend := store.get_trend_item(ref)) is not None and trend.id not in selected_ids
        )
    if missing_trend_count:
        warnings.append("Some selected trends were not found and were skipped.")
    if not selected_trends and use_trends:
        selected_trends = store.relevant_trends(topic=topic, angle=instruction, limit=3)
    if use_trends and not selected_trends:
        warnings.append("No relevant unused trends found. Created an evergreen post instead.")

    context_parts = [
        "Create one complete standalone X post for review.",
        f"User instruction: {instruction}",
    ]
    if extra_context and extra_context.strip():
        context_parts.append(f"Extra instruction: {extra_context.strip()}")
    if topic_idea:
        context_parts.append(f"Why this topic is worth posting now: {topic_idea.why_now}")
    request = DraftRequest(
        platform="x",
        topic=topic,
        post_type=post_type,
        angle=instruction,
        length_mode=length_mode,
        extra_context="\n".join(context_parts),
        use_trends=bool(selected_trends),
        trend_ids=[trend.id for trend in selected_trends],
        use_patterns=use_patterns,
        pattern_ids=pattern_ids or [],
        prompt_profile="natural",
        editorial_value_mode=topic_idea.reader_value_mode if topic_idea else None,
    )
    draft = generator.generate(request, variants=1)[0]
    warnings.extend(f"Quality warning: {warning}" for warning in draft_quality_warnings(draft))
    trend_ids = selected_trend_ids(draft, selected_trends)
    item = store.save_content_queue_item(
        ContentQueueItem(
            platform="x",
            draft_id=draft.id,
            topic_idea_id=topic_idea.id if topic_idea else None,
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
            ),
        )
    )
    if topic_idea:
        store.mark_topic_idea_status(topic_idea.id, "used")
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
) -> list[QueuedPostVersion]:
    _ = variants
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
    active_rules = active_writing_rules_for_prompt(store, skills)
    skills = skills_with_review_rules(skills, active_rules["review_rules"])
    avoid_phrases = load_avoid_phrases(store)
    model_routing = resolve_model_routing(store, provider)
    length_mode = queue_item_length_mode(store, item)
    max_chars = queue_item_max_chars(store, item)
    request = DraftRequest(
        platform=item.platform,
        topic=item.topic,
        post_type=item.post_type,
        angle=item.angle,
        length_mode=length_mode,
        max_chars=max_chars,
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
        build_strategy_prompt(
            request,
            bundle,
            trends=trends,
            patterns=[],
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
        ),
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
            active_rules=active_rules,
            strategy_summary=strategy_summary,
            max_chars=max_chars,
        ),
        variants=3,
        model=model_routing.natural_writer,
    )
    options: list[str] = []
    for raw in raw_options:
        try:
            text = fit_generated_text_to_limit(
                provider,
                raw,
                max_chars=max_chars,
                model=model_routing.natural_writer,
                repair_context="queued X revision option",
            )
        except ValueError:
            continue
        if text:
            options.append(text)
    if not options:
        raise ValueError("Provider did not return usable revision text.")
    evaluation_raw = generate_with_provider(
        provider,
        build_evaluation_prompt(
            request,
            skills=skills,
            strategy_summary=strategy_summary,
            draft_options=options,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            max_chars=max_chars,
        ),
        variants=1,
        model=model_routing.evaluator,
    )[0]
    final_text, _, _ = parse_evaluation_result(evaluation_raw, options)
    raw_versions = [final_text]
    saved: list[QueuedPostVersion] = []
    for raw in raw_versions:
        text = fit_generated_text_to_limit(
            provider,
            raw,
            max_chars=max_chars,
            model=model_routing.natural_writer,
            repair_context="queued X revision",
        )
        adapter.validate_text(text, max_chars=max_chars)
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
    author: str = "workspace",
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
    author: str = "workspace",
) -> PublishResult:
    if item.status != "approved":
        raise ValueError("Queued post must be approved before posting.")
    try:
        get_x_adapter().validate_text(item.text, max_chars=queue_item_max_chars(store, item))
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


def queue_item_source_draft(store: SQLiteStore, item: ContentQueueItem) -> GeneratedDraft | None:
    return store.get_generated_draft(item.draft_id) if item.draft_id else None


def queue_item_length_mode(store: SQLiteStore, item: ContentQueueItem) -> str:
    draft = queue_item_source_draft(store, item)
    if not draft:
        return "long" if len(item.text) > get_x_adapter().rules.default_max_chars else "standard"
    return normalize_length_mode(draft.request.length_mode)


def queue_item_max_chars(store: SQLiteStore, item: ContentQueueItem) -> int:
    draft = queue_item_source_draft(store, item)
    if not draft:
        return max(len(item.text), get_x_adapter().rules.default_max_chars)
    return resolve_length_max_chars(draft.request, get_x_adapter().rules.default_max_chars)


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
    return dedupe_editorial_trends([
        trend for trend in trends
        if (parsed := parse_trend_datetime(trend)) is not None and parsed >= cutoff
    ])


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
        f"explain one concrete {topic} mechanism and the causal step operators often miss",
        f"give readers a practical {topic} diagnostic: a symptom, likely cause, and useful test",
        f"offer one decision rule or tradeoff that helps a buyer evaluate {topic}",
        f"unpack one grounded {topic} fact and state what it proves and what it does not",
        f"share a specific {topic} operating or build lesson without forcing a product mention",
        f"give readers one sharp question to ask about {topic} and what a good answer includes",
        f"separate two {topic} ideas people blur together and show when the distinction matters",
        f"name one weak default in {topic} only if the replacement model teaches a genuinely new point",
    ]
    return defaults[index % len(defaults)]


def queue_notes(explanation: str, trend: TrendItem | None, *, quality_warnings: list[str] | None = None) -> str:
    pieces = [f"Why worth publishing: {explanation}"]
    if trend:
        pieces.append(f"Trend/source used: {trend.title}")
    if quality_warnings:
        pieces.append("Quality warnings: " + "; ".join(quality_warnings))
    return "\n".join(pieces)


def create_post_notes(
    draft: GeneratedDraft,
    *,
    instruction: str,
    trends: list[TrendItem],
) -> str:
    pieces = [
        f"Focus: {instruction}",
        f"Why worth publishing: {draft.explanation}",
    ]
    if trends:
        pieces.append("Trend/source used: " + ", ".join(trend.title for trend in trends))
    warnings = draft_quality_warnings(draft)
    if warnings:
        pieces.append("Quality warnings: " + "; ".join(warnings))
    return "\n".join(pieces)


def draft_quality_warnings(draft: GeneratedDraft) -> list[str]:
    return [format_quality_warning(warning) for warning in evaluation_warnings(draft)]


def evaluation_warnings(draft: GeneratedDraft) -> list[str]:
    snapshot = draft.context_snapshot or {}
    evaluation = snapshot.get("evaluation") if isinstance(snapshot, dict) else None
    if not isinstance(evaluation, dict):
        return []
    warnings = evaluation.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [" ".join(str(warning or "").split()) for warning in warnings if str(warning or "").strip()]


def format_quality_warning(warning: str) -> str:
    if warning == "not_post_worthy" or warning.startswith("not_post_worthy:"):
        return "no draft had a strong source bridge or post-worthy point; review closely."
    if warning.startswith("product_truthfulness:"):
        return "request did not include a concrete shipped change; final draft was reframed as a product belief."
    if warning.startswith("reframed_as_product_belief:"):
        return "reframed as a product belief from saved context."
    if warning in {"product_update_missing_concrete_change", "no_concrete_product_change_supplied"}:
        return "request did not include a concrete product change; review specificity."
    return warning.removeprefix("Quality warning:").strip()


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
    lines.append("- Avoid generic AI commentary unless it connects to a concrete saved-context point.")
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
