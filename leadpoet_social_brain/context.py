from __future__ import annotations

from collections import Counter

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
            analytics_hints=summary.hints[:4],
        )

    def rebuild_style_guide_from_examples(self, *, limit: int = 12) -> ContextSection:
        examples = self.store.top_examples(platform="x", limit=limit, imitate_only=True)
        notes = self.store.notes_by_post_id([post.id for post in examples])
        pattern_counts = Counter()
        for note in notes.values():
            for value in [
                note.pattern_name,
                note.hook,
                note.structure,
                note.tone,
                note.cta,
                note.formatting,
            ]:
                if value:
                    pattern_counts[value] += 1
        reasons = [
            note.worked_reason
            for note in notes.values()
            if note.worked_reason
        ]
        pattern_line = ", ".join(
            pattern for pattern, _ in pattern_counts.most_common(6)
        ) or "No repeated manual pattern has been marked yet."
        reason_line = " ".join(reasons[:6]) or "No manual win notes have been added yet."
        content = (
            "Reusable Gavin/Leadpoet style guide derived from high-performing examples: "
            "write in plain, founder-led language; make one specific argument; avoid hype, "
            "cliches, em dashes, and generic AI phrasing. Repeated patterns to consider: "
            f"{pattern_line}. Notes from winning posts: {reason_line}"
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
        fallback = self.store.top_examples(
            platform=platform,
            limit=max_examples,
            imitate_only=True,
        )
        for post in fallback:
            if post.id not in seen:
                exact.append(post)
                seen.add(post.id)
            if len(exact) >= max_examples:
                break
        return exact
