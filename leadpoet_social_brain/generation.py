from __future__ import annotations

import json
import re

from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContextBundle, DraftRequest, GeneratedDraft, TrendItem, ViralPattern
from leadpoet_social_brain.pattern_scout import select_relevant_patterns
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.providers import DraftProvider, ModelRouting, generate_with_provider, resolve_model_routing
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

    def generate(
        self,
        request: DraftRequest,
        *,
        variants: int = 3,
        save: bool = True,
        best_version: bool = False,
    ) -> list[GeneratedDraft]:
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
        patterns = self._patterns_for(request)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        if not self.provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        provider_name = provider_name_for(self.provider)
        model_routing = resolve_model_routing(self.store, self.provider)
        if best_version:
            return self._generate_best_version(
                request,
                bundle,
                skills=skills,
                trends=trends,
                patterns=patterns,
                max_chars=max_chars,
                provider_name=provider_name,
                model_routing=model_routing,
                save=save,
            )

        prompt = build_generation_prompt(request, bundle, skills=skills, trends=trends, patterns=patterns, max_chars=max_chars)
        raw_texts = generate_with_provider(
            self.provider,
            prompt,
            variants=variants,
            model=model_routing.default,
        )
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
                explanation=explain_influence(bundle, trends=trends, patterns=patterns),
                influences=build_influence_labels(bundle, trends=trends, patterns=patterns),
                context_snapshot=build_context_snapshot(
                    request,
                    bundle,
                    skills=skills,
                    trends=trends,
                    patterns=patterns,
                    quality_notes=quality_notes,
                    provider_name=provider_name,
                    quality_mode="standard",
                    model_routing=model_routing,
                    models_used={"default": model_routing.default},
                ),
            )
            if save:
                self.store.save_generated_draft(draft)
            drafts.append(draft)
        return drafts

    def _generate_best_version(
        self,
        request: DraftRequest,
        bundle: ContextBundle,
        *,
        skills: list[SkillAsset],
        trends: list[TrendItem],
        patterns: list[ViralPattern],
        max_chars: int,
        provider_name: str,
        model_routing: ModelRouting,
        save: bool,
    ) -> list[GeneratedDraft]:
        assert self.provider is not None
        adapter = self._adapter_for(request.platform)
        strategy_prompt = build_strategy_prompt(request, bundle, trends=trends, patterns=patterns)
        strategy_raw = generate_with_provider(
            self.provider,
            strategy_prompt,
            variants=1,
            model=model_routing.strategist,
        )[0]
        strategy_summary = parse_strategy_summary(strategy_raw)
        writer_prompt = build_best_writer_prompt(
            request,
            bundle,
            skills=skills,
            trends=trends,
            patterns=patterns,
            strategy_summary=strategy_summary,
            max_chars=max_chars,
        )
        draft_options = generate_with_provider(
            self.provider,
            writer_prompt,
            variants=3,
            model=model_routing.natural_writer,
        )
        cleaned_options = [clean_generated_text(raw, max_chars=max_chars) for raw in draft_options]
        cleaned_options = [text for text in cleaned_options if text]
        if not cleaned_options:
            raise ValueError("Provider did not return usable draft text.")
        evaluation_prompt = build_evaluation_prompt(
            request,
            skills=skills,
            strategy_summary=strategy_summary,
            draft_options=cleaned_options,
        )
        evaluation_raw = generate_with_provider(
            self.provider,
            evaluation_prompt,
            variants=1,
            model=model_routing.evaluator,
        )[0]
        final_text, quality_notes, evaluation = parse_evaluation_result(evaluation_raw, cleaned_options)
        text = clean_generated_text(final_text, max_chars=max_chars)
        adapter.validate_text(text, max_chars=max_chars)
        notes = quality_notes or build_quality_notes(request, bundle, trends=trends)
        draft = GeneratedDraft(
            text=text,
            request=request,
            explanation=explain_influence(bundle, trends=trends, patterns=patterns),
            influences=build_influence_labels(bundle, trends=trends, patterns=patterns),
            context_snapshot=build_context_snapshot(
                request,
                bundle,
                skills=skills,
                trends=trends,
                patterns=patterns,
                quality_notes=notes,
                provider_name=provider_name,
                quality_mode="best_version",
                model_routing=model_routing,
                models_used={
                    "default": model_routing.default,
                    "strategist": model_routing.strategist,
                    "natural_writer": model_routing.natural_writer,
                    "evaluator": model_routing.evaluator,
                },
                strategy_summary=strategy_summary,
                evaluation=evaluation,
            ),
        )
        if save:
            self.store.save_generated_draft(draft)
        return [draft]

    def build_prompt(self, request: DraftRequest) -> str:
        adapter = self._adapter_for(request.platform)
        max_chars = request.max_chars or adapter.rules.default_max_chars
        bundle = self.context_manager.build_bundle(
            platform=request.platform,
            topic=request.topic,
            post_type=request.post_type,
        )
        trends = self._trends_for(request)
        patterns = self._patterns_for(request)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        return build_generation_prompt(request, bundle, skills=skills, trends=trends, patterns=patterns, max_chars=max_chars)

    def _adapter_for(self, platform: str):
        if platform == "x":
            return get_x_adapter()
        raise ValueError(f"Unsupported platform '{platform}'. Add a platform adapter first.")

    def _trends_for(self, request: DraftRequest) -> list[TrendItem]:
        if request.trend_ids:
            return [
                trend for trend_id in request.trend_ids
                if (trend := self.store.get_trend_item(trend_id)) is not None
            ]
        if not request.use_trends:
            return []
        return self.store.relevant_trends(topic=request.topic, angle=request.angle, limit=3)

    def _patterns_for(self, request: DraftRequest) -> list[ViralPattern]:
        if not request.use_patterns and not request.pattern_ids:
            return []
        return select_relevant_patterns(
            self.store,
            topic=request.topic,
            pattern_ids=request.pattern_ids,
            limit=3,
        )


def build_generation_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
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
    voice = "\n".join(f"- {section.name}: {section.content}" for section in observed_style_sections)
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    if not examples_text:
        examples_text = "No examples yet. Lean on Leadpoet context and write naturally."
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    analytics_section = f"\n\nUseful performance guidance:\n{analytics}" if analytics else ""
    trend_section = f"\n\nTimely market context:\n{trends_text}" if trends_text else ""
    pattern_section = f"\n\nPattern mechanics to adapt:\n{patterns_text}" if patterns_text else ""
    return f"""Write {request.platform.upper()} posts for Leadpoet.

Study these Gavin/Leadpoet examples first:
{examples_text}

Leadpoet context:
{context or "- No positioning context yet."}

Koinoniko Voice:
{voice or "- No Koinoniko Voice profile yet."}
{trend_section}
{pattern_section}
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

Priority order: Koinoniko Voice, Leadpoet context, current trend/topic, Pattern mechanics, then platform rules.
Pattern safety: Use the structure, not the wording. Do not copy source post wording, claims, examples, or framing directly. If a Pattern conflicts with Voice, Voice wins.

Return only draft post text variants, no commentary. Do not write a bland news summary. Make the post timely, specific, and founder-led."""


def build_strategy_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    patterns: list[ViralPattern],
) -> str:
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    return f"""You are helping Gavin decide the strongest X post angle for Leadpoet.

Do not write the post yet. Decide what the post should accomplish.

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "choose the strongest specific angle"}
- Audience: {request.audience or "Leadpoet's current audience"}
- Extra context: {request.extra_context or "none"}

Leadpoet context:
{context or "- No positioning context yet."}

Gavin examples and worked-reason notes:
{examples_text or "No examples selected."}

Trend context:
{trends_text or "No timely trends selected."}

Pattern mechanics:
{patterns_text or "No Pattern Scout examples selected."}

Analytics hints:
{analytics or "- No analytics hints available."}

Return compact JSON only with these keys:
core_point, intended_audience, why_timely, post_angle, hook_direction, what_to_avoid, success_criteria."""


def build_best_writer_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    strategy_summary: dict[str, object],
    max_chars: int,
) -> str:
    skill_text = "\n\n".join(f"## {skill.path}\n{skill.content}" for skill in skills)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    return f"""Write 3 X draft options for Gavin.

Prioritize exact Gavin examples, worked-reason notes, and the writing skills below over generic marketing instincts.

Strategy:
{json.dumps(strategy_summary, indent=2)}

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Tone: {request.tone or "plain, sharp, useful"}
- CTA: {request.call_to_action or "only include if natural"}
- Extra context: {request.extra_context or "none"}
- Character limit: {max_chars}

Exact Gavin/Leadpoet examples:
{examples_text or "No examples selected."}

Leadpoet context:
{context or "- No positioning context yet."}

Trend context:
{trends_text or "No timely trends selected."}

Pattern mechanics:
{patterns_text or "No Pattern Scout examples selected."}

Performance hints:
{analytics or "- No analytics hints available."}

Writing and review instructions:
{skill_text}

Avoid corporate thought leadership, generic hype, fake profundity, em dashes, overly symmetrical phrasing, LinkedIn-style moral lessons, and "not just X, but Y" framing.
Use Pattern mechanics only as structure. Do not copy source post wording or make the post feel derivative. Make it specific to Gavin/Leadpoet.

Return only draft post text variants. Separate variants with a line containing only ---."""


def build_evaluation_prompt(
    request: DraftRequest,
    *,
    skills: list[SkillAsset],
    strategy_summary: dict[str, object],
    draft_options: list[str],
) -> str:
    skill_text = "\n\n".join(f"## {skill.path}\n{skill.content}" for skill in skills)
    options = "\n\n".join(f"Draft {index}:\n{text}" for index, text in enumerate(draft_options, start=1))
    return f"""Choose the best X post for Gavin and clean it up lightly.

Original request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "none"}
- Audience: {request.audience or "Leadpoet's current audience"}

Success criteria:
{json.dumps(strategy_summary.get("success_criteria", ""), indent=2)}

Voice and review rules:
{skill_text}

Draft options:
{options}

Return compact JSON only with:
ranked_drafts, quality_notes, warnings, suggested_final_version.

The suggested_final_version must be natural, specific, founder-led, concise, non-corporate, and free of em dashes."""


def build_context_snapshot(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    quality_notes: str,
    provider_name: str,
    quality_mode: str,
    model_routing: ModelRouting,
    models_used: dict[str, str | None],
    strategy_summary: dict[str, object] | None = None,
    evaluation: dict[str, object] | None = None,
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
        "quality_mode": quality_mode,
        "provider_name": provider_name,
        "model_routing": model_routing.as_dict(),
        "models_used": models_used,
        "default_model": models_used.get("default"),
        "strategist_model": models_used.get("strategist"),
        "natural_writer_model": models_used.get("natural_writer"),
        "evaluator_model": models_used.get("evaluator"),
        "strategy_summary": strategy_summary,
        "evaluation": evaluation,
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
        "selected_viral_patterns": [
            {
                "id": pattern.id,
                "pattern_name": pattern.pattern_name,
                "category": pattern.category,
                "topic": pattern.topic,
                "source_url": pattern.source_url,
                "source_author": pattern.source_author,
                "source_summary": pattern.source_summary,
                "hook_style": pattern.hook_style,
                "structure": pattern.structure,
                "pacing": pattern.pacing,
                "tone": pattern.tone,
                "formatting": pattern.formatting,
                "why_it_worked": pattern.why_it_worked,
                "adaptation_rule": pattern.adaptation_rule,
            }
            for pattern in patterns
        ],
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


def parse_strategy_summary(raw: str) -> dict[str, object]:
    parsed = parse_json_object(raw)
    if isinstance(parsed, dict):
        return {
            "core_point": str(parsed.get("core_point") or "").strip(),
            "intended_audience": str(parsed.get("intended_audience") or "").strip(),
            "why_timely": str(parsed.get("why_timely") or "").strip(),
            "post_angle": str(parsed.get("post_angle") or "").strip(),
            "hook_direction": str(parsed.get("hook_direction") or "").strip(),
            "what_to_avoid": str(parsed.get("what_to_avoid") or "").strip(),
            "success_criteria": parsed.get("success_criteria") or "",
        }
    return {"raw": raw.strip()}


def parse_evaluation_result(raw: str, draft_options: list[str]) -> tuple[str, str, dict[str, object]]:
    parsed = parse_json_object(raw)
    if isinstance(parsed, dict):
        final = str(parsed.get("suggested_final_version") or "").strip() or draft_options[0]
        notes = str(parsed.get("quality_notes") or "").strip()
        return final, notes, parsed
    return draft_options[0], raw.strip(), {"raw": raw.strip()}


def parse_json_object(raw: str) -> object | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def provider_name_for(provider: DraftProvider) -> str:
    provider_class = provider.__class__
    return f"{provider_class.__module__}.{provider_class.__name__}"


def build_influence_labels(bundle: ContextBundle, *, trends: list[TrendItem], patterns: list[ViralPattern]) -> list[str]:
    labels: list[str] = []
    labels.extend(f"voice:{section.name}" for section in bundle.style_sections)
    labels.extend(f"context:{section.name}" for section in bundle.positioning_sections)
    labels.extend(f"example:{post.id}" for post in bundle.examples)
    labels.extend(f"pattern:{pattern['name']}" for pattern in _selected_pattern_refs(bundle))
    labels.extend(f"pattern:{pattern.pattern_name}" for pattern in patterns if pattern.pattern_name)
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


def explain_influence(bundle: ContextBundle, *, trends: list[TrendItem], patterns: list[ViralPattern]) -> str:
    parts: list[str] = []
    if bundle.examples:
        parts.append(f"{len(bundle.examples)} high-performing X example(s)")
    if _selected_pattern_refs(bundle):
        parts.append("patterns observed in selected examples")
    if patterns:
        parts.append(f"{len(patterns)} Pattern Scout mechanic(s)")
    if bundle.positioning_sections:
        kinds = sorted({section.kind for section in bundle.positioning_sections})
        parts.append(f"Leadpoet context: {', '.join(kinds)}")
    if bundle.analytics_hints:
        parts.append("recent performance hints")
    if trends:
        parts.append(f"{len(trends)} timely trend(s)")
    return "Influenced by " + "; ".join(parts) + "." if parts else "Influenced by the base Koinoniko Voice rules."


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


def _format_viral_pattern(pattern: ViralPattern) -> str:
    return "\n".join(
        [
            "Pattern:",
            pattern.pattern_name or "Observed pattern",
            "",
            "Structure:",
            pattern.structure or pattern.source_summary or "A reusable post structure from a source example.",
            "",
            "Why it worked:",
            pattern.why_it_worked or pattern.engagement_notes or "The structure created attention or made the point easier to remember.",
            "",
            "Adaptation rule:",
            pattern.adaptation_rule or "Use the structure, not the wording.",
        ]
    )


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
