from __future__ import annotations

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.core.models import ContextBundle, ContextSection
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


STYLE_KINDS = {"style_guide"}
POSITIONING_KINDS = {
    "product",
    "audience",
    "positioning",
    "bittensor",
    "sales_llm_vision",
    "priority",
}


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
        return ContextBundle(
            platform=platform,
            topic=topic,
            post_type=post_type,
            style_sections=style_sections,
            positioning_sections=positioning_sections,
            patterns=patterns,
            examples=examples,
            notes_by_post_id=notes,
            metrics_by_post_id=metrics,
            analytics_hints=summary.hints[:4],
        )

    def rebuild_style_guide_from_examples(self, *, limit: int = 12) -> ContextSection:
        examples = self.store.top_examples(platform="x", limit=limit, imitate_only=True)
        notes = self.store.notes_by_post_id([post.id for post in examples])
        reasons = [note.worked_reason for note in notes.values() if note.worked_reason]
        example_shapes = _writing_shapes_from_notes(notes.values())
        content = "\n".join(
            [
                "Observed Gavin/Leadpoet Writing Guide",
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
                "",
                "Reusable writing principles:",
                "- Start from a concrete observation or weak default.",
                "- Make one sharp contrast or replacement model.",
                "- Use plain language even when the idea is technical.",
                "- Tie product beliefs to buyer pain, timing, or trust.",
                "- Leave the reader with a specific point of view, not a slogan.",
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
                or section.kind in {"product", "positioning", "audience", "priority"}
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
        exact = self.store.top_examples(
            platform=platform,
            topic=topic,
            post_type=post_type,
            limit=max_examples,
            imitate_only=True,
        )
        if len(exact) >= max_examples:
            return exact
        broader = self.store.top_examples(
            platform=platform,
            post_type=post_type,
            limit=max_examples,
            imitate_only=True,
        )
        seen = {post.id for post in exact}
        for post in broader:
            if post.id not in seen:
                exact.append(post)
                seen.add(post.id)
            if len(exact) >= max_examples:
                return exact
        broad_examples = self.store.top_examples(
            platform=platform,
            limit=max_examples,
            imitate_only=True,
        )
        for post in broad_examples:
            if post.id not in seen:
                exact.append(post)
                seen.add(post.id)
            if len(exact) >= max_examples:
                break
        return exact


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
