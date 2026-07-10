from __future__ import annotations

from dataclasses import dataclass
import math
import re

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.feedback import accepted_revision_preference_hints
from leadpoet_social_brain.core.models import (
    ContextBundle,
    ContextSection,
    EngagementMetrics,
    PostNote,
    SocialPost,
    VoiceExampleCandidate,
)
from leadpoet_social_brain.editorial import editorial_theme_fingerprints
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


STYLE_KINDS = {"style_guide"}
POSITIONING_KINDS = {
    "writer",
    "product",
    "audience",
    "positioning",
    "proof",
    "guardrail",
    "bittensor",
    "sales_llm_vision",
    "priority",
}


# A measured post has to earn its rank with enough exposure to be credible. The
# posterior lower bound stops a handful of engagements on a tiny denominator
# from outranking a repeatable result, without imposing a brittle hard minimum.
VOICE_PERFORMANCE_PRIOR_IMPRESSIONS = 800.0
VOICE_PERFORMANCE_LOWER_BOUND_Z = 1.28155
VOICE_SELF_CONTAINED_BONUS = 0.0015
VOICE_REPEATED_THESIS_PENALTY = 0.004
VOICE_REPEATED_STRUCTURE_PENALTY = 0.0015


@dataclass(frozen=True)
class _RankedVoiceCandidate:
    candidate: VoiceExampleCandidate
    relevance: int
    performance_score: float
    base_score: float
    self_contained: bool
    impressions: int
    total_engagements: int
    themes: frozenset[str]
    structure: str
    recency: str


class ContextManager:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def build_bundle(
        self,
        *,
        platform: str,
        topic: str,
        post_type: str,
        max_examples: int = 4,
        max_patterns: int = 6,
    ) -> ContextBundle:
        style_sections = self.store.list_context_sections(kinds=STYLE_KINDS)
        positioning_sections = self._select_positioning_sections(topic=topic, post_type=post_type)
        patterns = self.store.list_patterns(limit=max_patterns)
        examples = self._select_examples(
            platform=platform,
            topic=topic,
            post_type=post_type,
            max_examples=max_examples,
        )
        notes = self.store.notes_by_post_id([post.id for post in examples])
        metrics = self.store.latest_metrics_by_post_id([post.id for post in examples])
        summary = build_analytics_summary(self.store)
        feedback_hints = accepted_revision_preference_hints(self.store)
        return ContextBundle(
            platform=platform,
            topic=topic,
            post_type=post_type,
            workspace_id=self.store.workspace_id,
            style_sections=style_sections,
            positioning_sections=positioning_sections,
            patterns=patterns,
            examples=examples,
            notes_by_post_id=notes,
            metrics_by_post_id=metrics,
            analytics_hints=[*feedback_hints, *summary.hints][:4],
        )

    def rebuild_style_guide_from_examples(self, *, limit: int = 12) -> ContextSection:
        examples = self.store.top_examples(platform="x", limit=limit, imitate_only=True)
        notes = self.store.notes_by_post_id([post.id for post in examples])
        reasons = [note.worked_reason for note in notes.values() if note.worked_reason]
        example_shapes = _writing_shapes_from_notes(notes.values())
        content = "\n".join(
            [
                "Voice Profile",
                "",
                "Recurring voice traits:",
                "- Plain, direct, and specific.",
                "- Founder-led without sounding like a brand account.",
                "- Practical enough that a buyer or investor can see the business point.",
                "",
                "Common natural post shapes:",
                *[f"- {shape}" for shape in example_shapes[:4]],
                "",
                "What strong posts tend to do:",
                *[f"- {reason}" for reason in reasons[:5]],
                "",
                "Avoid:",
                "- Generic AI phrasing, launch theater, and vague market commentary.",
                "- Forced CTAs when the post already makes the business point.",
                "- Tag language or internal labels in the writing itself.",
                "- Reusing an example's thesis or broken-default frame just because its cadence is strong.",
                "",
                "Reusable writing principles:",
                "- Start from a concrete observation, mechanism, question, example, or weak default.",
                "- Use a sharp contrast only when the idea genuinely depends on one.",
                "- Use plain language even when the idea is technical.",
                "- Keep positioning optional; a useful post can teach without mentioning the product.",
                "- Leave the reader with a mechanism, diagnostic, decision rule, or specific point of view, not a slogan.",
            ]
        )
        section = ContextSection(
            kind="style_guide",
            name="learned_from_high_performers",
            content=content,
            weight=2.5,
        )
        return self.store.save_context_section(section)

    def _select_positioning_sections(self, *, topic: str, post_type: str) -> list[ContextSection]:
        sections = self.store.list_context_sections(kinds=POSITIONING_KINDS)
        lowered_topic = topic.lower()
        selected: list[ContextSection] = []
        for section in sections:
            haystack = f"{section.kind} {section.name} {section.content}".lower()
            if (
                lowered_topic in haystack
                or post_type in haystack
                or section.kind in {"writer", "product", "positioning", "audience", "proof", "priority", "guardrail"}
            ):
                selected.append(section)
        return selected[:8]

    def _select_examples(
        self,
        *,
        platform: str,
        topic: str,
        post_type: str,
        max_examples: int,
    ):
        if max_examples <= 0:
            return []

        selected: list[SocialPost] = []
        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        explicit_candidate_limit = max(12, max_examples * 4)

        def add(posts) -> bool:
            for post in posts:
                text_key = _exact_text_key(post.text)
                if post.id in seen_ids or text_key in seen_texts:
                    continue
                selected.append(post)
                seen_ids.add(post.id)
                seen_texts.add(text_key)
                if len(selected) >= max_examples:
                    return True
            return False

        explicit_pools = [
            self.store.top_examples(
                platform=platform,
                topic=topic,
                post_type=post_type,
                limit=explicit_candidate_limit,
                imitate_only=True,
            ),
            self.store.top_examples(
                platform=platform,
                post_type=post_type,
                limit=explicit_candidate_limit,
                imitate_only=True,
            ),
            self.store.top_examples(
                platform=platform,
                limit=explicit_candidate_limit,
                imitate_only=True,
            ),
        ]
        for pool in explicit_pools:
            if add(pool):
                return selected

        # Autopilot fallback: authored posts still carry far more voice signal than a
        # generic style prompt. Explicit Voice Examples always win; high-performing
        # relevant posts only fill uncovered slots. This path intentionally uses a
        # separate candidate read model so confidence-aware ranking cannot change
        # legacy analytics or Topic Scout ordering.
        candidates = self.store.list_authored_voice_candidates(platform=platform)
        prior_rate = _workspace_engagement_prior(candidates)
        ranked = [
            _rank_voice_candidate(
                candidate,
                requested_topic=topic,
                requested_post_type=post_type,
                prior_rate=prior_rate,
            )
            for candidate in candidates
            if candidate.post.id not in seen_ids
            and _exact_text_key(candidate.post.text) not in seen_texts
        ]

        explicit_notes = self.store.notes_by_post_id(seen_ids) if seen_ids else {}
        used_themes = {
            theme
            for post in selected
            for theme in editorial_theme_fingerprints(post.text)
        }
        used_structures = {
            structure
            for post in selected
            if (structure := _voice_structure(post.text, explicit_notes.get(post.id)))
        }

        # Relevance is a hard boundary: diversity can choose among equally relevant
        # examples, but never trades an exact request match for an unrelated post.
        for relevance in range(4):
            pool = [item for item in ranked if item.relevance == relevance]
            while pool and len(selected) < max_examples:
                winner = max(
                    pool,
                    key=lambda item: _voice_selection_key(
                        item,
                        used_themes=used_themes,
                        used_structures=used_structures,
                    ),
                )
                post = winner.candidate.post
                selected.append(post)
                seen_ids.add(post.id)
                seen_texts.add(_exact_text_key(post.text))
                used_themes.update(winner.themes)
                if winner.structure:
                    used_structures.add(winner.structure)
                pool.remove(winner)
            if len(selected) >= max_examples:
                break
        return selected


def _workspace_engagement_prior(candidates: list[VoiceExampleCandidate]) -> float:
    weighted_engagements = 0.0
    total_impressions = 0
    for candidate in candidates:
        rate = _measured_engagement_rate(candidate.metrics)
        impressions = _metric_impressions(candidate.metrics)
        if rate is None or impressions <= 0:
            continue
        weighted_engagements += rate * impressions
        total_impressions += impressions
    if total_impressions <= 0:
        return 0.02
    return min(1.0, max(0.0, weighted_engagements / total_impressions))


def _rank_voice_candidate(
    candidate: VoiceExampleCandidate,
    *,
    requested_topic: str,
    requested_post_type: str,
    prior_rate: float,
) -> _RankedVoiceCandidate:
    post = candidate.post
    impressions = _metric_impressions(candidate.metrics)
    rate = _measured_engagement_rate(candidate.metrics)
    observed_rate = prior_rate if rate is None else rate
    posterior_observations = VOICE_PERFORMANCE_PRIOR_IMPRESSIONS + impressions
    posterior_rate = (
        (prior_rate * VOICE_PERFORMANCE_PRIOR_IMPRESSIONS)
        + (observed_rate * impressions)
    ) / posterior_observations
    standard_error = math.sqrt(
        max(0.0, posterior_rate * (1.0 - posterior_rate))
        / (posterior_observations + 1.0)
    )
    performance_score = max(
        0.0,
        posterior_rate - (VOICE_PERFORMANCE_LOWER_BOUND_Z * standard_error),
    )
    self_contained = _is_self_contained(post)
    self_contained_bonus = VOICE_SELF_CONTAINED_BONUS if self_contained else 0.0
    exact_topic = _topic_matches(post.topic, requested_topic)
    exact_type = post.post_type.casefold() == requested_post_type.casefold()
    if exact_topic and exact_type:
        relevance = 0
    elif exact_type:
        relevance = 1
    elif exact_topic:
        relevance = 2
    else:
        relevance = 3
    return _RankedVoiceCandidate(
        candidate=candidate,
        relevance=relevance,
        performance_score=performance_score,
        base_score=performance_score + self_contained_bonus,
        self_contained=self_contained,
        impressions=impressions,
        total_engagements=candidate.metrics.total_engagements() if candidate.metrics else 0,
        themes=frozenset(editorial_theme_fingerprints(post.text)),
        structure=_voice_structure(post.text, candidate.note),
        recency=post.published_at or post.created_at,
    )


def _voice_selection_key(
    candidate: _RankedVoiceCandidate,
    *,
    used_themes: set[str],
    used_structures: set[str],
) -> tuple[bool, float, float, int, int, str, str]:
    repeated_thesis = bool(candidate.themes and candidate.themes.intersection(used_themes))
    repeated_structure = bool(candidate.structure and candidate.structure in used_structures)
    diversity_adjusted_score = candidate.base_score
    if repeated_thesis:
        diversity_adjusted_score -= VOICE_REPEATED_THESIS_PENALTY
    if repeated_structure:
        diversity_adjusted_score -= VOICE_REPEATED_STRUCTURE_PENALTY
    return (
        candidate.self_contained,
        diversity_adjusted_score,
        candidate.performance_score,
        candidate.impressions,
        candidate.total_engagements,
        candidate.recency,
        candidate.candidate.post.id,
    )


def _metric_impressions(metrics: EngagementMetrics | None) -> int:
    if not metrics or metrics.impressions is None:
        return 0
    return max(0, int(metrics.impressions))


def _measured_engagement_rate(metrics: EngagementMetrics | None) -> float | None:
    impressions = _metric_impressions(metrics)
    if not metrics or impressions <= 0:
        return None
    rate = metrics.engagement_rate
    if rate is None:
        rate = metrics.total_engagements() / impressions
    return min(1.0, max(0.0, float(rate)))


def _topic_matches(candidate_topic: str, requested_topic: str) -> bool:
    candidate = " ".join(candidate_topic.casefold().split())
    requested = " ".join(requested_topic.casefold().split())
    return bool(requested and (candidate == requested or requested in candidate))


def _exact_text_key(text: str) -> str:
    return " ".join(text.casefold().split())


def _is_self_contained(post: SocialPost) -> bool:
    text = post.text.strip()
    if re.search(r"https?://\S+|\b(?:link|prompt|template|vault)\s+(?:is\s+)?(?:below|in bio)\b|\(below\)", text, flags=re.I):
        return False
    without_links = re.sub(r"https?://\S+", " ", text, flags=re.I)
    if len(re.findall(r"\b[\w'-]+\b", without_links)) < 8:
        return False
    if post.post_type.casefold() in {"quote_tweet", "reply"}:
        return False
    if re.search(r"(?:[,;:]|[|/\\+\-]|[┃┣┗┳])\s*$", text):
        return False
    return not bool(
        re.match(
            r"^(?:@\w+\s+|exactly[.!,:]?\s|agreed[.!,:]?\s|well said[.!,:]?\s|"
            r"this right here\b|this is (?:it|why)\b|(?:this|that)[.!?]\s)",
            text,
            flags=re.I,
        )
    )


def _voice_structure(text: str, note: PostNote | None) -> str:
    if note and note.structure and note.structure.strip():
        return " ".join(note.structure.casefold().replace("_", " ").split())
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", text):
        return "list"
    if "?" in (paragraphs[0] if paragraphs else text)[:160]:
        return "question led"
    if re.search(r"\b(?:but|instead|not .{0,30} but|rather than)\b", text, flags=re.I):
        return "contrast"
    if len(paragraphs) >= 3:
        return "multi paragraph"
    if len(lines) >= 3:
        return "short lines"
    return "single paragraph"


def _writing_shapes_from_notes(notes) -> list[str]:
    shapes: list[str] = []
    for note in notes:
        if note.pattern_name == "enemy_then_better_model" or note.structure == "enemy_then_better_model":
            shapes.append("Name the weak default, then offer the sharper replacement model.")
        if note.structure == "claim_then_list":
            shapes.append("Make a plain claim, then list the concrete parts that make it true.")
        if note.hook == "specific_observation":
            shapes.append("Open with something observed in the market or product, then explain the implication.")
        if note.cta and note.cta != "none":
            shapes.append("Use a soft invitation only when it naturally follows from the point.")
    if not shapes:
        shapes.append("Make one clear product or market point in plain language.")
    deduped: list[str] = []
    for shape in shapes:
        if shape not in deduped:
            deduped.append(shape)
    return deduped
