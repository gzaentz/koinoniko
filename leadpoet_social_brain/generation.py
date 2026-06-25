from __future__ import annotations

import re

from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContextBundle, DraftRequest, GeneratedDraft, TrendItem
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.providers import DraftProvider
from leadpoet_social_brain.skills import SkillAsset, generation_skills
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


MISSING_PROVIDER_MESSAGE = (
    "No LLM provider configured. Set SOCIAL_BRAIN_PROVIDER=module.path:create_provider to generate drafts."
)


class SocialBrainGenerator:
    def __init__(self, store: SQLiteStore, provider: DraftProvider | None = None):
        self.store = store
        self.provider = provider
        self.context_manager = ContextManager(store)

    def generate(self, request: DraftRequest, *, variants: int = 3, save: bool = True) -> list[GeneratedDraft]:
        adapter = self._adapter_for(request.platform)
        if hasattr(adapter, "validate_post_type"):
            adapter.validate_post_type(request.post_type)
        max_chars = request.max_chars or adapter.rules.default_max_chars
        bundle = self.context_manager.build_bundle(
            platform=request.platform,
            topic=request.topic,
            post_type=request.post_type,
        )
        trends = self._trends_for(request)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        prompt = build_generation_prompt(request, bundle, skills=skills, trends=trends, max_chars=max_chars)
        if not self.provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        provider_name = provider_name_for(self.provider)
        raw_texts = self.provider.generate(prompt, variants=variants)
        drafts: list[GeneratedDraft] = []
        for raw_text in raw_texts:
            text = clean_generated_text(raw_text, max_chars=max_chars)
            adapter.validate_text(text, max_chars=max_chars)
            quality_notes = build_quality_notes(
                request,
                bundle,
                trends=trends,
            )
            draft = GeneratedDraft(
                text=text,
                request=request,
                explanation=explain_influence(bundle, trends=trends),
                influences=build_influence_labels(bundle, trends=trends),
                context_snapshot=build_context_snapshot(
                    request,
                    bundle,
                    skills=skills,
                    trends=trends,
                    quality_notes=quality_notes,
                    provider_name=provider_name,
                ),
            )
            if save:
                self.store.save_generated_draft(draft)
            drafts.append(draft)
        return drafts

    def build_prompt(self, request: DraftRequest) -> str:
        adapter = self._adapter_for(request.platform)
        max_chars = request.max_chars or adapter.rules.default_max_chars
        bundle = self.context_manager.build_bundle(
            platform=request.platform,
            topic=request.topic,
            post_type=request.post_type,
        )
        trends = self._trends_for(request)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        return build_generation_prompt(request, bundle, skills=skills, trends=trends, max_chars=max_chars)

    def _adapter_for(self, platform: str):
        if platform == "x":
            return get_x_adapter()
        raise ValueError(f"Unsupported platform '{platform}'. Add a platform adapter first.")

    def _trends_for(self, request: DraftRequest) -> list[TrendItem]:
        if not request.use_trends:
            return []
        return self.store.relevant_trends(topic=request.topic, angle=request.angle, limit=3)


def build_generation_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    max_chars: int,
) -> str:
    skill_text = "\n\n".join(
        f"## {skill.path}\n{skill.content}"
        for skill in skills
    )
    observed_style_sections = [
        section for section in bundle.style_sections
        if section.name != "gavin_founder_voice_v0"
    ]
    style = "\n".join(f"- {section.name}: {section.content}" for section in observed_style_sections)
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    if not examples_text:
        examples_text = "No examples yet. Lean on Leadpoet context and write naturally."
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    analytics_section = f"\n\nUseful performance guidance:\n{analytics}" if analytics else ""
    trend_section = f"\n\nTimely market context:\n{trends_text}" if trends_text else ""
    return f"""Write {request.platform.upper()} posts for Leadpoet.

Study these Gavin/Leadpoet examples first:
{examples_text}

Leadpoet context:
{context or "- No positioning context yet."}

Observed style guide:
{style or "- No style guide yet."}
{trend_section}
{analytics_section}

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "choose the strongest specific angle"}
- Audience: {request.audience or "Leadpoet's current audience"}
- Tone: {request.tone or "plain, sharp, useful"}
- CTA: {request.call_to_action or "only include if it feels natural"}
- Extra context: {request.extra_context or "none"}
- Character limit: {max_chars}

Writing and review instructions:
{skill_text or "- No skill files found. Use retrieved context and keep the post natural."}

Return only draft post text variants, no commentary. Do not write a bland news summary. Make the post timely, specific, and founder-led."""


def build_context_snapshot(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    quality_notes: str,
    provider_name: str,
) -> dict[str, object]:
    examples = []
    for post in bundle.examples:
        note = bundle.notes_by_post_id.get(post.id)
        examples.append(
            {
                "id": post.id,
                "topic": post.topic,
                "post_type": post.post_type,
                "author": post.author,
                "url": post.url,
                "text_preview": post.text[:240],
                "worked_reason": note.worked_reason if note else None,
                "pattern_name": note.pattern_name if note else None,
                "hook": note.hook if note else None,
                "structure": note.structure if note else None,
                "tone": note.tone if note else None,
                "cta": note.cta if note else None,
                "formatting": note.formatting if note else None,
                "performance": _performance_summary(bundle.metrics_by_post_id.get(post.id)),
            }
        )
    return {
        "request": request.__dict__,
        "generation_mode": "provider",
        "provider_name": provider_name,
        "quality_notes": quality_notes,
        "skills": [
            {
                "path": skill.path,
                "sha256": skill.sha256,
            }
            for skill in skills
        ],
        "selected_examples": examples,
        "selected_patterns": _selected_pattern_refs(bundle),
        "selected_context_sections": [
            {
                "id": section.id,
                "kind": section.kind,
                "name": section.name,
                "weight": section.weight,
            }
            for section in [*bundle.style_sections, *bundle.positioning_sections]
        ],
        "analytics_hints": bundle.analytics_hints,
        "selected_trends": [
            {
                "id": trend.id,
                "title": trend.title,
                "source_type": trend.source_type,
                "topic": trend.topic,
                "urgency": trend.urgency,
                "leadpoet_angle": trend.leadpoet_angle,
            }
            for trend in trends
        ],
    }


def provider_name_for(provider: DraftProvider) -> str:
    provider_class = provider.__class__
    return f"{provider_class.__module__}.{provider_class.__name__}"


def build_influence_labels(bundle: ContextBundle, *, trends: list[TrendItem]) -> list[str]:
    labels: list[str] = []
    labels.extend(f"style:{section.name}" for section in bundle.style_sections)
    labels.extend(f"context:{section.name}" for section in bundle.positioning_sections)
    labels.extend(f"example:{post.id}" for post in bundle.examples)
    labels.extend(f"pattern:{pattern['name']}" for pattern in _selected_pattern_refs(bundle))
    labels.extend(f"analytics:{hint}" for hint in bundle.analytics_hints)
    labels.extend(f"trend:{trend.id}" for trend in trends)
    return labels


def _selected_pattern_refs(bundle: ContextBundle) -> list[dict[str, object]]:
    known = {pattern.name: pattern for pattern in bundle.patterns}
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for post in bundle.examples:
        note = bundle.notes_by_post_id.get(post.id)
        if not note:
            continue
        values = [
            ("pattern", note.pattern_name),
            ("hook", note.hook),
            ("structure", note.structure),
            ("tone", note.tone),
            ("cta", note.cta),
            ("formatting", note.formatting),
        ]
        for category, raw_name in values:
            name = _compact(raw_name or "")
            if not name:
                continue
            pattern = known.get(name)
            ref_category = pattern.category if pattern and category == "pattern" else category
            key = (ref_category, name)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "id": pattern.id if pattern else None,
                    "name": name,
                    "category": ref_category,
                    "description": pattern.description if pattern else None,
                    "source_example_id": post.id,
                    "source_field": category,
                }
            )
    return refs


def clean_generated_text(text: str, *, max_chars: int) -> str:
    cleaned = text.replace("—", "-").replace("–", "-")
    cleaned = re.sub(r"\b(game-changing|revolutionary|unlock|delve into|in today's world)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    trimmed = cleaned[: max_chars - 1].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0].rstrip()
    return f"{trimmed}."


def explain_influence(bundle: ContextBundle, *, trends: list[TrendItem]) -> str:
    parts: list[str] = []
    if bundle.examples:
        parts.append(f"{len(bundle.examples)} high-performing X example(s)")
    if _selected_pattern_refs(bundle):
        parts.append("patterns observed in selected examples")
    if bundle.positioning_sections:
        kinds = sorted({section.kind for section in bundle.positioning_sections})
        parts.append(f"Leadpoet context: {', '.join(kinds)}")
    if bundle.analytics_hints:
        parts.append("recent performance hints")
    if trends:
        parts.append(f"{len(trends)} timely trend(s)")
    return "Influenced by " + "; ".join(parts) + "." if parts else "Influenced by the base Gavin/Leadpoet style rules."


def build_quality_notes(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
) -> str:
    angle = request.angle or request.topic
    example_note = f"{len(bundle.examples)} example(s)" if bundle.examples else "base Leadpoet context"
    trend_note = f" and {len(trends)} trend(s)" if trends else ""
    return (
        f"Fits the requested angle by centering '{angle}'. "
        f"Grounded in {example_note}{trend_note}. "
        "Review for specificity, real point of view, natural Gavin voice, and inbound relevance."
    )


def _format_example(post, bundle: ContextBundle) -> str:
    note = bundle.notes_by_post_id.get(post.id)
    performance = _performance_summary(bundle.metrics_by_post_id.get(post.id))
    parts = [
        "Example:",
        post.text,
        "",
        "Why it worked:",
        (note.worked_reason if note and note.worked_reason else "Strong example of Gavin/Leadpoet voice."),
    ]
    if performance:
        parts.extend(["", "Performance:", performance])
    return "\n".join(parts)


def _format_trend(trend: TrendItem) -> str:
    parts = [
        "Trend:",
        trend.title,
        "",
        "What happened:",
        trend.summary,
        "",
        "Why it matters:",
        trend.why_it_matters,
        "",
        "Leadpoet angle:",
        trend.leadpoet_angle,
    ]
    if trend.suggested_hooks:
        parts.extend(["", "Possible hooks:", trend.suggested_hooks])
    return "\n".join(parts)


def _performance_summary(metrics) -> str:
    if not metrics:
        return ""
    pieces: list[str] = []
    if metrics.impressions is not None:
        pieces.append(f"{metrics.impressions} impressions")
    if metrics.likes is not None:
        pieces.append(f"{metrics.likes} likes")
    if metrics.replies is not None:
        pieces.append(f"{metrics.replies} replies")
    if metrics.reposts is not None:
        pieces.append(f"{metrics.reposts} reposts")
    if metrics.bookmarks is not None:
        pieces.append(f"{metrics.bookmarks} bookmarks")
    if metrics.engagement_rate is not None:
        pieces.append(f"{metrics.engagement_rate:.2%} engagement rate")
    return ", ".join(pieces)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
