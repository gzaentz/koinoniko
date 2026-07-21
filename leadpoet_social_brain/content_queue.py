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
from leadpoet_social_brain.editorial import dedupe_editorial_trends
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    normalize_length_mode,
    prompt_safe_automated_label,
    prompt_untrusted_text,
    resolve_length_max_chars,
    trend_specific_generation_angle,
)
from leadpoet_social_brain.originality import canonical_copy_text
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.platforms.x_api import XPostedTweet, XWriteClient
from leadpoet_social_brain.providers import DraftProvider
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.topic_scout import prioritize_topic_ideas_for_schedule


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
    continue_on_generation_error: bool = False,
    occupied_schedule_dates: set[date] | None = None,
    generation_attempt_limit: int | None = None,
    existing_topic_ideas: list[TopicIdea] | None = None,
) -> PlanWeekResult:
    _ = variants_per_slot
    if posts <= 0:
        raise ValueError("--posts must be greater than 0.")
    requested_posts = posts
    get_x_adapter().validate_post_type(default_post_type)
    start_date = date.fromisoformat(week_start)
    topics = expand_topic_mix(topic_mix, posts)
    candidate_limit = posts
    if continue_on_generation_error and generation_attempt_limit is not None:
        candidate_limit = max(posts, int(generation_attempt_limit))
    available_topic_ideas = [
        idea for idea in (topic_ideas or []) if idea.status == "suggested"
    ]
    selected_topic_ideas = prioritize_topic_ideas_for_schedule(
        available_topic_ideas,
        slots=posts,
        existing_ideas=existing_topic_ideas,
    )[:candidate_limit]
    if topic_ideas is not None:
        if not selected_topic_ideas:
            raise ValueError("No grounded topic ideas are available for Autopilot planning.")
        posts = min(posts, len(selected_topic_ideas))
    scheduled_times = schedule_slots(
        start_date,
        posts=posts,
        cadence=cadence,
        occupied_dates=occupied_schedule_dates,
    )
    trends = recent_unused_trends(store, window_days=trend_window_days) if use_trends else []
    warnings: list[str] = []
    if use_trends and topic_ideas is None and not trends and not allow_evergreen:
        raise ValueError("No recent unused trends available. Add trends or pass --allow-evergreen.")
    if use_trends and topic_ideas is None and len(trends) < posts and not allow_evergreen:
        raise ValueError(
            f"Only {len(trends)} recent unused trend(s) available for {posts} post(s). "
            "Add trends, lower --posts, or pass --allow-evergreen."
        )
    if use_trends and topic_ideas is None and not trends:
        warnings.append("No recent unused trends found. Planning evergreen authority posts.")
    if topic_ideas is not None and posts < requested_posts:
        warnings.append(
            f"Planned {posts} grounded topic idea(s) instead of {requested_posts}; "
            "Autopilot did not fill the remaining slots with generic fallback copy."
        )

    used_trends: set[str] = set()
    queued: list[ContentQueueItem] = []
    candidate_count = len(selected_topic_ideas) if topic_ideas is not None else posts
    for index in range(candidate_count):
        if len(queued) >= posts:
            break
        topic_idea = selected_topic_ideas[index] if index < len(selected_topic_ideas) else None
        topic = (
            prompt_safe_automated_label(
                topic_idea.topic,
                fallback="sales AI",
                max_chars=100,
            )
            if topic_idea
            else topics[index % len(topics)]
        )
        post_type = topic_idea.post_type if topic_idea else default_post_type
        referenced_trends = [
            trend
            for ref in (topic_idea.source_refs if topic_idea else [])
            if (trend := store.get_trend_item(ref)) is not None and trend.id not in used_trends
        ]
        trend = referenced_trends[0] if referenced_trends else (
            None
            if topic_idea
            else (pick_trend_for_topic(trends, topic=topic, used_trend_ids=used_trends) if use_trends else None)
        )
        if trend:
            used_trends.add(trend.id)
        elif use_trends and not topic_idea:
            warnings.append(f"No unused trend left for slot {index + 1}; planned evergreen post.")
        angle = (
            prompt_untrusted_text(topic_idea.angle, max_chars=500)
            if topic_idea
            else (trend_specific_generation_angle(trend) if trend else evergreen_angle(topic, index))
        )
        request = DraftRequest(
            platform="x",
            topic=topic,
            post_type=post_type,
            angle=angle,
            extra_context=extra_instruction.strip() if extra_instruction and extra_instruction.strip() else None,
            angle_is_factual_evidence=False,
            extra_context_is_factual_evidence=False,
            length_mode=length_mode,
            use_trends=bool(trend),
            trend_ids=[trend.id] if trend else [],
            use_patterns=use_patterns,
            prompt_profile="natural",
            editorial_value_mode=topic_idea.reader_value_mode if topic_idea else None,
            source_evidence=list(topic_idea.evidence_basis) if topic_idea else [],
            source_refs=list(topic_idea.source_refs) if topic_idea else [],
        )
        try:
            drafts = generator.generate(
                request,
                variants=1,
                recent_output_texts=[item.text for item in queued],
            )
        except Exception as exc:  # noqa: BLE001 - unattended planning should preserve other valid slots.
            if not continue_on_generation_error:
                raise
            warnings.append(
                f"Slot {index + 1}: skipped after generation or quality validation failed: {exc}"
            )
            continue
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
                scheduled_at=scheduled_times[len(queued)].isoformat(),
                status="needs_review",
                reviewer_notes=queue_notes(draft.explanation, trend, quality_warnings=quality_warnings),
            )
        )
        queued.append(item)
        if topic_idea:
            store.mark_topic_idea_status(topic_idea.id, "used")
    if continue_on_generation_error and not queued:
        raise ValueError("Autopilot did not produce any review-ready posts; every slot failed validation.")
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
    if not selected_trends and use_trends and not topic_idea:
        selected_trends = store.relevant_trends(topic=topic, angle=instruction, limit=3)
    if use_trends and not selected_trends:
        warnings.append("No relevant unused trends found. Created an evergreen post instead.")

    request = DraftRequest(
        platform="x",
        topic=topic,
        post_type=post_type,
        angle=instruction,
        length_mode=length_mode,
        extra_context=extra_context.strip() if extra_context and extra_context.strip() else None,
        use_trends=bool(selected_trends),
        trend_ids=[trend.id for trend in selected_trends],
        use_patterns=use_patterns,
        pattern_ids=pattern_ids or [],
        prompt_profile="natural",
        editorial_value_mode=topic_idea.reader_value_mode if topic_idea else None,
        source_evidence=list(topic_idea.evidence_basis) if topic_idea else [],
        source_refs=list(topic_idea.source_refs) if topic_idea else [],
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
    if item.status in {"posting", "posted"}:
        raise ValueError("Posted or posting queue items cannot be approved again.")
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


def resolve_queue_edit_state(
    item: ContentQueueItem,
    *,
    text: str,
    topic: str,
    post_type: str,
    angle: str | None,
    requested_status: str,
) -> tuple[str, str | None, bool]:
    """Apply approval safety consistently across queue-edit entry points.

    Approval belongs to the exact copy that was reviewed. A material edit to an
    approved item therefore reopens review even when a caller also submits an
    ``approved`` status. Published and in-flight copy is immutable because an
    edit there could diverge local state from what X received.
    """

    material_changed = any(
        [
            text != item.text,
            topic != item.topic,
            post_type != item.post_type,
            angle != item.angle,
        ]
    )
    if material_changed and item.status in {"posting", "posted"}:
        raise ValueError("Posted or posting queue items cannot be materially edited.")
    if material_changed and (item.status == "approved" or requested_status == "approved"):
        return "needs_review", None, True

    if requested_status == "approved":
        approved_at = item.approved_at or utc_now()
    else:
        approved_at = None
    return requested_status, approved_at, material_changed


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
    length_mode = queue_item_length_mode(store, item)
    max_chars = queue_item_max_chars(store, item)
    source_draft = store.get_generated_draft(item.draft_id) if item.draft_id else None
    source_request = source_draft.request if source_draft else None
    request = DraftRequest(
        platform=item.platform,
        topic=item.topic,
        post_type=item.post_type,
        angle=item.angle or (source_request.angle if source_request else None) or instruction.strip(),
        audience=source_request.audience if source_request else None,
        tone=source_request.tone if source_request else None,
        call_to_action=source_request.call_to_action if source_request else None,
        length_mode=length_mode,
        max_chars=max_chars,
        # Only the original request/context remains factual evidence.  The current
        # prose is an editing target, not proof that its existing claims are true.
        extra_context=source_request.extra_context if source_request else None,
        angle_is_factual_evidence=(
            source_request.angle_is_factual_evidence if source_request else True
        ),
        extra_context_is_factual_evidence=(
            source_request.extra_context_is_factual_evidence if source_request else True
        ),
        use_trends=bool(item.trend_ids),
        trend_ids=item.trend_ids,
        use_patterns=bool(source_request and source_request.pattern_ids),
        pattern_ids=list(source_request.pattern_ids) if source_request else [],
        prompt_profile="natural",
        editorial_value_mode=source_request.editorial_value_mode if source_request else None,
        source_evidence=list(source_request.source_evidence) if source_request else [],
        source_refs=list(source_request.source_refs) if source_request else [],
        revision_source_text=item.text,
        revision_instruction=instruction.strip(),
        revision_source_queue_id=item.id,
        revision_source_draft_id=item.draft_id,
    )
    draft = SocialBrainGenerator(store, provider=provider).generate(
        request,
        variants=1,
        save=False,
    )[0]
    adapter.validate_text(draft.text, max_chars=max_chars)
    if " ".join(draft.text.split()) == " ".join(item.text.split()):
        raise ValueError("Revision did not change the queued post; no version was saved.")
    draft = store.save_generated_draft(draft)
    return [
        store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=item.id,
                text=draft.text,
                edit_type="llm",
                edit_instruction=instruction.strip(),
                source_draft_id=draft.id,
            )
        )
    ]


def accept_queued_post_version(store: SQLiteStore, *, version_id: str) -> ContentQueueItem:
    version = store.get_queued_post_version(version_id)
    if not version:
        raise ValueError(f"No queued post version found for id {version_id}")
    item = require_queue_item(store, version.queue_id)
    store.save_queued_post_version(
        QueuedPostVersion(
            queue_id=item.id,
            text=item.text,
            edit_type="superseded",
            edit_instruction="previous text before accepting revision",
            source_draft_id=item.draft_id,
        )
    )
    saved = store.save_content_queue_item(
        ContentQueueItem(
            **{
                **item.__dict__,
                "text": version.text,
                "draft_id": version.source_draft_id or item.draft_id,
                "status": "needs_review" if item.status == "approved" else item.status,
                "approved_at": None if item.status == "approved" else item.approved_at,
            }
        )
    )
    store.mark_queued_post_version_accepted(version.id)
    return saved


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
    current = require_queue_item(store, item.id)
    if current.status == "posted" and current.x_post_id:
        return PublishResult(queue_item=current, posted=None, error=None)
    if current.status != "approved":
        raise ValueError("Queued post must be approved before posting.")
    current_copy_key = canonical_copy_text(current.text)
    duplicate = next(
        (
            post
            for post in store.list_posts(
                platform=current.platform,
                status="published",
                limit=None,
            )
            if current_copy_key and canonical_copy_text(post.text) == current_copy_key
        ),
        None,
    )
    if duplicate is not None:
        raise ValueError(
            "Queued post duplicates copy that is already published; edit it and approve the new text before posting."
        )
    claimed = store.claim_content_queue_item_for_posting(current.id)
    if claimed is None:
        current = require_queue_item(store, current.id)
        if current.status == "posted" and current.x_post_id:
            return PublishResult(queue_item=current, posted=None, error=None)
        raise ValueError("Queued post is already being posted or is no longer approved.")
    item = claimed
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


def schedule_slots(
    start_date: date,
    *,
    posts: int,
    cadence: str,
    occupied_dates: set[date] | None = None,
) -> list[datetime]:
    slots: list[datetime] = []
    occupied_dates = occupied_dates or set()
    day_offset = 0
    while len(slots) < posts:
        slot_date = start_date + timedelta(days=day_offset)
        day_offset += 1
        if cadence == "weekday_mornings" and slot_date.weekday() >= 5:
            continue
        if cadence not in {"daily_mornings", "weekday_mornings"}:
            raise ValueError("Unsupported --cadence. Use daily_mornings or weekday_mornings.")
        if slot_date in occupied_dates:
            continue
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
