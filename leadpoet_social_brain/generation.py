from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContextBundle, DraftRequest, GeneratedDraft, TrendItem, ViralPattern
from leadpoet_social_brain.pattern_scout import select_relevant_patterns
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.providers import DraftProvider, ModelRouting, generate_with_provider, resolve_model_routing
from leadpoet_social_brain.skills import SkillAsset, generation_skills
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.voice import (
    active_rules_prompt_section,
    avoid_phrases_prompt_section,
    load_avoid_phrases,
    load_positioning_rules,
    load_review_rules,
    load_voice_rules_override,
    review_filter_skill_content,
)


MISSING_PROVIDER_MESSAGE = (
    "No LLM provider configured. Set SOCIAL_BRAIN_PROVIDER=module.path:create_provider to generate drafts."
)

POST_LENGTH_PRESETS = {
    "short": {
        "label": "Short",
        "max_chars": 160,
        "instruction": "Very concise. Aim for 1-3 short lines and one clean point. Do not pad for rhythm.",
    },
    "standard": {
        "label": "Standard",
        "max_chars": 280,
        "instruction": "Default X shape. Aim for roughly 140-260 characters, with room below the hard limit.",
    },
    "long": {
        "label": "Long",
        "max_chars": 1200,
        "instruction": "Long X post. Aim for 400-900 characters in short paragraphs. Build the point, but do not turn it into a thread unless explicitly asked.",
    },
}


NATURAL_VOICE_POLISH_PATTERNS = [
    (r"\bthe future of\b", "avoid polished 'future of' framing"),
    (r"\bhere'?s the thing\b", "remove stock setup phrase"),
    (r"\bin today'?s (?:world|market|landscape)\b", "remove generic topical setup"),
    (r"\btoday'?s rapidly evolving\b", "remove generic topical setup"),
    (r"\bas (?:ai|the market|the industry|technology|sales|gtm)[^.\n]{0,80}\bcontinues to\b", "remove polished 'as X continues to' setup"),
    (r"\b(?:new era|paradigm shift|next wave)\b", "remove generic trend language"),
    (r"\b(?:leverage|harness|seamless|cutting-edge|transformative)\b", "replace corporate/AI-polish language with plain words"),
    (r"\bat scale\b", "replace generic scale language with a concrete mechanism"),
    (r"\bthe (?:real )?(?:opportunity|takeaway|lesson) is\b", "avoid tidy thought-leadership framing"),
]

STOCK_CONTRAST_PATTERNS = [
    (r"\bit(?:'s| is) not about [^.\n]{1,120}\.\s+it(?:'s| is) about\b", "avoid stock 'it's not about X, it's about Y' framing"),
    (r"\bnot about [^.\n]{1,120};?\s+(?:it(?:'s| is)|but)\s+about\b", "avoid stock 'not about X, about Y' framing"),
]

UNSUPPORTED_FIRST_PERSON_PATTERNS = [
    (r"\bi spent (?:the )?(?:morning|afternoon|day|week|weekend|month)\b", "remove unsupported first-person research setup"),
    (r"\bi (?:was )?talking to\b", "remove unsupported first-person conversation setup"),
    (r"\bafter talking to\b", "remove unsupported first-person conversation setup"),
    (r"\ba customer told me\b", "remove unsupported customer anecdote"),
    (r"\bwe (?:tested|ran|analyzed|surveyed|interviewed)\b", "remove unsupported proof or research setup"),
    (r"\bi (?:dug into|read through|analyzed|surveyed|interviewed)\b", "remove unsupported research setup"),
]

LEADPOET_DEFINITION_PATTERNS = [
    (r"\bleadpoet is building\b", "avoid default 'Leadpoet is building...' product definition"),
    (r"\bleadpoet is built\b", "avoid default 'Leadpoet is built for...' product definition"),
    (r"\bleadpoet is\b", "avoid standalone 'Leadpoet is...' product definition"),
    (r"(?m)(?:^|\n\n)\s*for leadpoet,\s+", "avoid formulaic 'For Leadpoet...' product lead-in"),
    (r"(?m)(?:^|[.!?]\s+|\n\n)\s*leadpoet\s+(?:should|can|needs?|has\s+to)\b", "avoid formulaic Leadpoet modal lead-in"),
    (r"\b(?:that|this)\s+is\s+where\s+leadpoet\b|\bthat['’]?s\s+where\s+leadpoet\b", "avoid formulaic 'that is where Leadpoet...' product lead-in"),
    (r"\bthat\s+is\s+the\s+leadpoet\s+(?:job|bar|point|constraint|product belief)\b", "avoid formulaic 'That is the Leadpoet...' closer"),
    (r"\bthe\s+leadpoet\s+(?:product belief|signal|point|constraint|job|bar|part)\s+is\b", "avoid formulaic 'The Leadpoet ... is...' product definition"),
    (r"(?m)(?:^|\n\n)\s*leadpoet(?:'s|’s)?\s+(?:bet|job|role)\b", "avoid formulaic Leadpoet closer"),
    (r"(?m)(?:^|\n\n)\s*the layer worth building\b", "avoid generic 'layer worth building' closer"),
]

POST_STRUCTURE_SHAPES = [
    (
        "specific signal -> blunt implication -> product mechanism",
        "Start with a named source/detail, then a short implication, then the concrete mechanism the workspace cares about.",
    ),
    (
        "weak default teardown",
        "Name the weak default, explain why it fails, then give the sharper replacement model.",
    ),
    (
        "buyer/user scene",
        "Open inside a practical buyer or rep moment without inventing a story; make the situation do the work.",
    ),
    (
        "two short claims + consequence",
        "Use two clipped observations, then one consequence that makes the point feel earned.",
    ),
    (
        "market proof -> boundary",
        "Treat the trend as proof of a shift, then draw a clear boundary around what the trend does not solve.",
    ),
    (
        "question -> answer -> why it matters",
        "Ask the real operational question, answer it plainly, then state why the answer changes action.",
    ),
    (
        "definition inversion",
        "Redefine a familiar category by what actually changes behavior, not by the vendor label.",
    ),
    (
        "micro-list with a turn",
        "Use 2-3 concrete fragments or clauses, then turn them into one useful conclusion.",
    ),
    (
        "plain product belief",
        "Skip the trend recap and state a grounded belief from saved context in a human way.",
    ),
]

POST_LAYOUT_TARGETS = [
    (
        "single compact paragraph",
        "Write one compact paragraph with no blank line; keep the product mention embedded inside the flow.",
    ),
    (
        "two short paragraphs",
        "Use two short paragraphs. The second paragraph should add consequence, not repeat the first.",
    ),
    (
        "question opener",
        "Open with one plain question, then answer it in one short paragraph.",
    ),
    (
        "micro-list turn",
        "Use 2-3 very short list-like lines or fragments, then one turn that makes the point.",
    ),
    (
        "three clipped lines",
        "Use three short lines with no long setup; each line should move the argument forward.",
    ),
    (
        "colon hinge",
        "Use a colon in the first line to set up the specific mechanism, then land the implication.",
    ),
    (
        "longer observation",
        "Use one slightly longer observation followed by a short final sentence. Do not make it a tidy slogan.",
    ),
]

POST_CLOSING_TARGETS = [
    (
        "mechanism stop",
        "End on the concrete mechanism or signal. Do not add a summary sentence after it.",
    ),
    (
        "operator consequence",
        "End with what the rep, founder, or buyer can do differently because of the point.",
    ),
    (
        "plain implication",
        "End with one plain implication. No motivational lesson, CTA, or brand tagline.",
    ),
    (
        "source boundary",
        "End by naming what the trend proves and what it still does not solve.",
    ),
    (
        "no product-name closer",
        "If the product name appears, place it earlier; the final line should stand on the idea itself.",
    ),
    (
        "short verdict",
        "End with a short verdict that feels like a person deciding, not a slogan.",
    ),
    (
        "open loop",
        "End with the operational question the reader should now ask, without sounding like engagement bait.",
    ),
]

POST_HUMAN_RHYTHM_TARGETS = [
    (
        "founder aside",
        "Use one plain founder-belief sentence like 'I think...' or 'The mistake I keep seeing...' without inventing evidence.",
    ),
    (
        "clipped operator note",
        "Make it feel like an operator note: short, concrete sentences with one useful fragment allowed.",
    ),
    (
        "scene without anecdote",
        "Open on a generic rep, buyer, or founder moment. Do not pretend it happened personally.",
    ),
    (
        "specific noun pressure",
        "Lean on concrete nouns close together, such as CRM record, timing signal, account change, or sequence copy.",
    ),
    (
        "quiet disagreement",
        "Disagree with the market default in a calm, specific way. Avoid symmetrical X-but-Y phrasing.",
    ),
    (
        "rough edge",
        "Leave one sentence slightly blunt or unfinished-feeling instead of smoothing it into a slogan.",
    ),
    (
        "questionable assumption",
        "Name the assumption behind the trend, then test it against the saved context.",
    ),
]

PRODUCT_MENTION_LANES = [
    (
        "mechanism-first",
        "Name the mechanism before the product. The product appears as the place that mechanism has to become usable.",
    ),
    (
        "consequence-first",
        "State the practical consequence first, then mention the product as the thing that makes the consequence actionable.",
    ),
    (
        "quiet embedded name",
        "Use the product name inside a sentence, not as the subject of a definition or final standalone line.",
    ),
    (
        "no-name wedge",
        "Skip the product name if the saved-context wedge is already clear; make the proprietary timing/context point carry it.",
    ),
    (
        "operator scene",
        "Mention the product through a rep, founder, or buyer situation rather than a category claim.",
    ),
    (
        "boundary line",
        "Draw the boundary around what the trend does not solve, then place the product on the sharper side of that boundary.",
    ),
    (
        "signal language",
        "Talk about the specific signal, timing, or account context the product cares about before naming the product.",
    ),
    (
        "ownership without definition",
        "Use a grounded verb from the sentence context, like catches, notices, keeps honest, makes visible, or proves; avoid 'is', 'is building', and tidy taglines.",
    ),
    (
        "implicit product belief",
        "Make a product belief from saved context without sounding like a product description.",
    ),
]

PRODUCT_MENTION_PLACEMENT_TARGETS = [
    (
        "early embedded",
        "If the product name is needed, place it in the first paragraph after a concrete mechanism. Do not open with a product definition.",
    ),
    (
        "middle turn",
        "Let the first idea stand on its own, then use the product name at the turn into implication or mechanism.",
    ),
    (
        "late consequence",
        "Mention the product near the end as a consequence of the point, embedded in a sentence rather than a tagline.",
    ),
    (
        "product-free close",
        "If the product name appears, keep the final line product-free so the post ends on the idea, not the brand.",
    ),
    (
        "quiet tuck-in",
        "Use the least disruptive product mention possible. If omission is allowed, let the mechanism carry the point.",
    ),
]


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
    ) -> list[GeneratedDraft]:
        _ = variants
        adapter = self._adapter_for(request.platform)
        if hasattr(adapter, "normalize_post_type"):
            request = replace(request, post_type=adapter.normalize_post_type(request.post_type))
        if hasattr(adapter, "validate_post_type"):
            adapter.validate_post_type(request.post_type)
        max_chars = resolve_length_max_chars(request, adapter.rules.default_max_chars)
        bundle = self.context_manager.build_bundle(
            platform=request.platform,
            topic=request.topic,
            post_type=request.post_type,
        )
        trends = self._trends_for(request)
        patterns = self._patterns_for(request)
        avoid_phrases = load_avoid_phrases(self.store)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        active_rules = active_writing_rules_for_prompt(self.store, skills)
        skills = skills_with_review_rules(skills, active_rules["review_rules"])
        if not self.provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        provider_name = provider_name_for(self.provider)
        model_routing = resolve_model_routing(self.store, self.provider)
        return self._generate_quality_draft(
            request,
            bundle,
            skills=skills,
            trends=trends,
            patterns=patterns,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            max_chars=max_chars,
            provider_name=provider_name,
            model_routing=model_routing,
            save=save,
        )

    def _generate_quality_draft(
        self,
        request: DraftRequest,
        bundle: ContextBundle,
        *,
        skills: list[SkillAsset],
        trends: list[TrendItem],
        patterns: list[ViralPattern],
        avoid_phrases: list[str],
        active_rules: dict[str, list[str]],
        max_chars: int,
        provider_name: str,
        model_routing: ModelRouting,
        save: bool,
    ) -> list[GeneratedDraft]:
        assert self.provider is not None
        adapter = self._adapter_for(request.platform)
        strategy_prompt = build_strategy_prompt(
            request,
            bundle,
            trends=trends,
            patterns=patterns,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
        )
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
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            strategy_summary=strategy_summary,
            max_chars=max_chars,
        )
        draft_options = generate_with_provider(
            self.provider,
            writer_prompt,
            variants=3,
            model=model_routing.natural_writer,
        )
        cleaned_options = clean_draft_options(
            self.provider,
            draft_options,
            max_chars=max_chars,
            model=model_routing.natural_writer,
            repair_context="quality-reviewed X draft option",
        )
        if not cleaned_options:
            retry_options = generate_with_provider(
                self.provider,
                build_writer_retry_prompt(
                    writer_prompt,
                    max_chars=max_chars,
                    anchor=workspace_anchor_name(bundle, active_rules),
                ),
                variants=1,
                model=model_routing.natural_writer,
            )
            cleaned_options = clean_draft_options(
                self.provider,
                retry_options,
                max_chars=max_chars,
                model=model_routing.natural_writer,
                repair_context="quality-reviewed X draft retry",
            )
            if not cleaned_options:
                raise ValueError("Provider did not return usable draft text.")
        evaluation_prompt = build_evaluation_prompt(
            request,
            skills=skills,
            strategy_summary=strategy_summary,
            draft_options=cleaned_options,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            max_chars=max_chars,
        )
        evaluation_raw = generate_with_provider(
            self.provider,
            evaluation_prompt,
            variants=1,
            model=model_routing.evaluator,
        )[0]
        final_text, quality_notes, evaluation = parse_evaluation_result(evaluation_raw, cleaned_options)
        text = fit_generated_text_to_limit(
            self.provider,
            final_text,
            max_chars=max_chars,
            model=model_routing.natural_writer,
            repair_context="quality-reviewed final X post",
        )
        text, rule_warnings = repair_draft_against_rules(
            self.provider,
            text,
            request,
            bundle,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            max_chars=max_chars,
            model=model_routing.natural_writer,
        )
        repaired_successfully = text != final_text and not rule_warnings
        if rule_warnings:
            existing_warnings = evaluation.get("warnings") if isinstance(evaluation, dict) else []
            evaluation = {
                **evaluation,
                "warnings": [
                    *(
                        [str(warning) for warning in existing_warnings if str(warning).strip()]
                        if isinstance(existing_warnings, list)
                        else []
                    ),
                    *rule_warnings,
                ],
            }
        evaluation = clean_evaluation_warnings_after_final_text(
            evaluation,
            text=text,
            request=request,
            bundle=bundle,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            repaired_successfully=repaired_successfully,
        )
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
                avoid_phrases=avoid_phrases,
                active_rules=active_rules,
                quality_notes=notes,
                provider_name=provider_name,
                quality_mode="quality_review",
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
            draft = self.store.save_generated_draft(draft)
        return [draft]

    def build_prompt(self, request: DraftRequest) -> str:
        adapter = self._adapter_for(request.platform)
        max_chars = resolve_length_max_chars(request, adapter.rules.default_max_chars)
        bundle = self.context_manager.build_bundle(
            platform=request.platform,
            topic=request.topic,
            post_type=request.post_type,
        )
        trends = self._trends_for(request)
        patterns = self._patterns_for(request)
        avoid_phrases = load_avoid_phrases(self.store)
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        active_rules = active_writing_rules_for_prompt(self.store, skills)
        skills = skills_with_review_rules(skills, active_rules["review_rules"])
        return build_generation_prompt(
            request,
            bundle,
            skills=skills,
            trends=trends,
            patterns=patterns,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
            max_chars=max_chars,
        )

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


def active_writing_rules_for_prompt(store: SQLiteStore, skills: list[SkillAsset]) -> dict[str, list[str]]:
    return {
        "positioning_rules": load_positioning_rules(store),
        "voice_rules": load_voice_rules_override(store) or [],
        "review_rules": load_review_rules(store, default_review_rules_from_skills(skills)),
    }


def default_review_rules_from_skills(skills: list[SkillAsset]) -> list[str]:
    for skill in skills:
        if skill.path == "skills/review_filter.md":
            return [line[2:].strip() for line in skill.content.splitlines() if line.startswith("- ")]
    return []


def skills_with_review_rules(skills: list[SkillAsset], review_rules: list[str]) -> list[SkillAsset]:
    content = review_filter_skill_content(review_rules)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    replacement = SkillAsset(path="skills/review_filter.md", content=content, sha256=digest)
    updated: list[SkillAsset] = []
    replaced = False
    for skill in skills:
        if skill.path == "skills/review_filter.md":
            updated.append(replacement)
            replaced = True
        else:
            updated.append(skill)
    if not replaced:
        updated.append(replacement)
    return updated


def normalize_length_mode(mode: str | None) -> str:
    cleaned = str(mode or "standard").strip().lower().replace("_", "-")
    aliases = {
        "concise": "short",
        "brief": "short",
        "normal": "standard",
        "default": "standard",
        "longform": "long",
        "long-form": "long",
        "expanded": "long",
    }
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned if cleaned in POST_LENGTH_PRESETS else "standard"


def resolve_length_max_chars(request: DraftRequest, platform_default: int) -> int:
    if request.max_chars:
        return request.max_chars
    mode = normalize_length_mode(request.length_mode)
    return int(POST_LENGTH_PRESETS.get(mode, {}).get("max_chars") or platform_default)


def length_prompt_section(request: DraftRequest, max_chars: int) -> str:
    mode = normalize_length_mode(request.length_mode)
    preset = POST_LENGTH_PRESETS[mode]
    return (
        f"- Length form: {preset['label']}\n"
        f"- Length target: {preset['instruction']}\n"
        f"- Character limit: {max_chars}"
    )


def natural_voice_prompt_section(bundle: ContextBundle | None = None, anchor: str | None = None) -> str:
    example_line = (
        "- Voice Examples are the calibration set: borrow cadence, bluntness, and specificity, not wording."
        if bundle and bundle.examples
        else "- With thin examples, use saved Facts & Context and keep the language plainer than feels necessary."
    )
    product_label = anchor or "the product"
    return (
        "Natural Voice Calibration:\n"
        "- Sound like one specific founder noticing a real thing, not a brand explaining a category.\n"
        "- Prefer exact nouns from the request/context over umbrella language like platform, solution, workflow, or transformation.\n"
        "- Keep the first line plain. No grand setup, hype setup, or polished category recap.\n"
        "- Let the point land without a motivational closer, generic lesson, or forced CTA.\n"
        "- If a sentence feels smooth enough for a launch page, shorten it and make it more concrete.\n"
        "- Avoid stock contrast frames like \"it's not about X, it's about Y\" and \"not just X, but Y\".\n"
        f"- Do not default to standalone product-definition sentences like \"{product_label} is...\" or \"{product_label} is building...\".\n"
        "- If the product name appears, earn it through the specific mechanism, scene, or consequence in the post.\n"
        f"{example_line}"
    )


def human_rhythm_prompt_section(request: DraftRequest, trends: list[TrendItem]) -> str:
    selected = selected_human_rhythm_targets(request, trends, count=4)
    primary_name, primary_guidance = selected[0]
    options = "\n".join(f"- {name}: {guidance}" for name, guidance in selected)
    return (
        "Human Rhythm Variation:\n"
        "The post should not sound machine-even. Vary sentence rhythm, opener texture, and how the product name enters.\n"
        f"Primary human rhythm target for this request: {primary_name}: {primary_guidance}\n"
        "Use the primary rhythm target unless it would make the post worse. "
        "In multi-option prompts, option 1 should use the primary rhythm and each option should feel rhythmically distinct.\n"
        f"{options}"
    )


def structure_variation_prompt_section(request: DraftRequest, trends: list[TrendItem]) -> str:
    selected = selected_structure_shapes(request, trends, count=4)
    primary_name, primary_guidance = selected[0]
    layout_name, layout_guidance = primary_layout_target(request, trends)
    closing_name, closing_guidance = primary_closing_target(request, trends)
    options = "\n".join(f"- {name}: {guidance}" for name, guidance in selected)
    return (
        "Structure Variation:\n"
        "Avoid the default template: trend recap -> generic product definition -> tidy closer.\n"
        f"Primary structure for this request: {primary_name}: {primary_guidance}\n"
        f"Primary layout target for this request: {layout_name}: {layout_guidance}\n"
        f"Primary closing target for this request: {closing_name}: {closing_guidance}\n"
        "Use the primary structure unless another listed shape clearly fits the idea better. "
        "Use the primary layout target unless it would make the post worse. "
        "Use the primary closing target unless it would make the post worse. "
        "In multi-option prompts, option 1 should use the primary structure/layout/closing and each option must use a different shape.\n"
        f"{options}"
    )


def product_mention_prompt_section(
    anchor: str | None = None,
    request: DraftRequest | None = None,
    trends: list[TrendItem] | None = None,
) -> str:
    product_label = anchor or "the product"
    example_prefix = anchor or "the product"
    selected = selected_product_mention_lanes(request, trends or [], count=4) if request else PRODUCT_MENTION_LANES[:4]
    primary_name, primary_guidance = selected[0]
    lane_options = "\n".join(f"- {name}: {guidance}" for name, guidance in selected)
    placement_selected = (
        selected_product_mention_placement_targets(request, trends or [], count=4)
        if request
        else PRODUCT_MENTION_PLACEMENT_TARGETS[:4]
    )
    placement_name, placement_guidance = placement_selected[0]
    placement_options = "\n".join(f"- {name}: {guidance}" for name, guidance in placement_selected)
    return (
        "Dynamic Product Mention:\n"
        "- Do not make the product mention the same final paragraph every time.\n"
        f"- Avoid standalone sentences beginning \"{product_label} is...\", \"{product_label} is building...\", or \"{product_label}'s bet...\".\n"
        "- Prefer varied human phrasing: \"the part we care about...\", \"the mechanism has to start before copy...\", "
        f"\"the timing signal {example_prefix} cares about is...\", or no product name if the saved-context mechanism is already clear.\n"
        "- If the post needs the name for positioning, embed it in a concrete sentence rather than defining the company.\n"
        f"Primary product mention lane for this request: {primary_name}: {primary_guidance}\n"
        f"Primary product mention placement target for this request: {placement_name}: {placement_guidance}\n"
        "Use the lane for the rhetorical job and the placement target for where the name enters.\n"
        "Product mention lanes for this request:\n"
        f"{lane_options}\n"
        "Product mention placement targets for this request:\n"
        f"{placement_options}\n"
        "- In multi-option prompts, option 1 should use the primary lane and each option should use a different product mention lane."
    )


def selected_product_mention_lanes(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    seed_source = variation_seed_source(request, trends)
    digest = hashlib.sha256(f"product-mention|{seed_source}".encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(PRODUCT_MENTION_LANES)
    return [
        PRODUCT_MENTION_LANES[(start + offset) % len(PRODUCT_MENTION_LANES)]
        for offset in range(min(count, len(PRODUCT_MENTION_LANES)))
    ]


def primary_product_mention_lane(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_product_mention_lanes(request, trends, count=1)[0]


def selected_product_mention_placement_targets(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    seed_source = variation_seed_source(request, trends)
    digest = hashlib.sha256(f"product-placement|{seed_source}".encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(PRODUCT_MENTION_PLACEMENT_TARGETS)
    return [
        PRODUCT_MENTION_PLACEMENT_TARGETS[(start + offset) % len(PRODUCT_MENTION_PLACEMENT_TARGETS)]
        for offset in range(min(count, len(PRODUCT_MENTION_PLACEMENT_TARGETS)))
    ]


def primary_product_mention_placement_target(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_product_mention_placement_targets(request, trends, count=1)[0]


def selected_structure_shapes(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    digest = hashlib.sha256(variation_seed_source(request, trends).encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(POST_STRUCTURE_SHAPES)
    return [
        POST_STRUCTURE_SHAPES[(start + offset) % len(POST_STRUCTURE_SHAPES)]
        for offset in range(min(count, len(POST_STRUCTURE_SHAPES)))
    ]


def primary_structure_shape(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_structure_shapes(request, trends, count=1)[0]


def selected_layout_targets(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    digest = hashlib.sha256(f"layout-target|{variation_seed_source(request, trends)}".encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(POST_LAYOUT_TARGETS)
    return [
        POST_LAYOUT_TARGETS[(start + offset) % len(POST_LAYOUT_TARGETS)]
        for offset in range(min(count, len(POST_LAYOUT_TARGETS)))
    ]


def primary_layout_target(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_layout_targets(request, trends, count=1)[0]


def selected_closing_targets(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    digest = hashlib.sha256(f"closing-target|{variation_seed_source(request, trends)}".encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(POST_CLOSING_TARGETS)
    return [
        POST_CLOSING_TARGETS[(start + offset) % len(POST_CLOSING_TARGETS)]
        for offset in range(min(count, len(POST_CLOSING_TARGETS)))
    ]


def primary_closing_target(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_closing_targets(request, trends, count=1)[0]


def selected_human_rhythm_targets(
    request: DraftRequest,
    trends: list[TrendItem],
    *,
    count: int,
) -> list[tuple[str, str]]:
    digest = hashlib.sha256(f"human-rhythm|{variation_seed_source(request, trends)}".encode("utf-8")).hexdigest()
    start = int(digest[:8], 16) % len(POST_HUMAN_RHYTHM_TARGETS)
    return [
        POST_HUMAN_RHYTHM_TARGETS[(start + offset) % len(POST_HUMAN_RHYTHM_TARGETS)]
        for offset in range(min(count, len(POST_HUMAN_RHYTHM_TARGETS)))
    ]


def primary_human_rhythm_target(request: DraftRequest, trends: list[TrendItem]) -> tuple[str, str]:
    return selected_human_rhythm_targets(request, trends, count=1)[0]


def draft_option_variation_matrix(request: DraftRequest, trends: list[TrendItem], *, count: int = 3) -> str:
    structures = selected_structure_shapes(request, trends, count=count)
    layouts = selected_layout_targets(request, trends, count=count)
    closings = selected_closing_targets(request, trends, count=count)
    rhythms = selected_human_rhythm_targets(request, trends, count=count)
    lanes = selected_product_mention_lanes(request, trends, count=count)
    placements = selected_product_mention_placement_targets(request, trends, count=count)
    lines = [
        "Draft Option Variation Map:",
        "Use these as distinct assignments, not labels to print in the post.",
    ]
    for index in range(count):
        lines.append(
            (
                f"- Option {index + 1}: "
                f"structure={structures[index][0]}; "
                f"layout={layouts[index][0]}; "
                f"closing={closings[index][0]}; "
                f"rhythm={rhythms[index][0]}; "
                f"product mention lane={lanes[index][0]}; "
                f"product placement={placements[index][0]}"
            )
        )
    return "\n".join(lines)


def variation_seed_source(request: DraftRequest, trends: list[TrendItem]) -> str:
    return "|".join(
        [
            request.platform,
            request.topic,
            request.post_type,
            request.angle or "",
            ",".join(request.trend_ids or []),
            ",".join(trend.id for trend in trends),
        ]
    )


def build_generation_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    max_chars: int,
    avoid_phrases: list[str] | None = None,
    active_rules: dict[str, list[str]] | None = None,
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
        examples_text = "No examples yet. Lean on saved Facts & Context and write naturally."
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    analytics_section = f"\n\nUseful performance guidance:\n{analytics}" if analytics else ""
    trend_section = f"\n\nTimely market context:\n{trends_text}" if trends_text else ""
    pattern_section = f"\n\nPattern mechanics to adapt:\n{patterns_text}" if patterns_text else ""
    avoid_section = avoid_phrases_prompt_section(avoid_phrases or [])
    active_rules_section = active_rules_prompt_section(
        positioning_rules=(active_rules or {}).get("positioning_rules", []),
        voice_rules=(active_rules or {}).get("voice_rules", []),
    )
    anchor = workspace_anchor_name(bundle, active_rules or {})
    natural_voice_section = natural_voice_prompt_section(bundle, anchor=anchor)
    human_rhythm_section = human_rhythm_prompt_section(request, trends)
    structure_section = structure_variation_prompt_section(request, trends)
    product_mention_section = product_mention_prompt_section(anchor, request=request, trends=trends)
    length_section = length_prompt_section(request, max_chars)
    return f"""Write {request.platform.upper()} posts for this workspace.

Study these selected Voice Examples first:
{examples_text}

Facts & Context:
{context or "- No saved facts/context yet."}

Voice profile:
{voice or "- No saved Voice profile yet."}

{natural_voice_section}

{human_rhythm_section}

{structure_section}

{product_mention_section}

{active_rules_section}
{trend_section}
{pattern_section}
{analytics_section}

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "choose the strongest specific angle"}
- Audience: {request.audience or "the saved target audience"}
- Tone: {request.tone or "plain, sharp, useful"}
- CTA: {request.call_to_action or "only include if it feels natural"}
- Extra context: {request.extra_context or "none"}
{length_section}

Writing and review instructions:
{skill_text or "- No skill files found. Use retrieved context and keep the post natural."}

{avoid_section}

Priority order: Voice profile, Facts & Context, current trend/topic, Pattern mechanics, then platform rules.
Pattern safety: Use the structure, not the wording. Do not copy source post wording, claims, examples, or framing directly. If a Pattern conflicts with Voice, Voice wins.

Each variant must be {max_chars} characters or fewer and end as a complete thought.
Return only draft post text variants, no commentary. Do not write a bland news summary. Make the post timely, specific, and grounded in saved context."""


def build_strategy_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    avoid_phrases: list[str] | None = None,
    active_rules: dict[str, list[str]] | None = None,
) -> str:
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    avoid_section = avoid_phrases_prompt_section(avoid_phrases or [])
    active_rules_section = active_rules_prompt_section(
        positioning_rules=(active_rules or {}).get("positioning_rules", []),
        voice_rules=(active_rules or {}).get("voice_rules", []),
    )
    max_chars = resolve_length_max_chars(request, get_x_adapter().rules.default_max_chars)
    length_section = length_prompt_section(request, max_chars)
    anchor = workspace_anchor_name(bundle, active_rules or {})
    natural_voice_section = natural_voice_prompt_section(bundle, anchor=anchor)
    human_rhythm_section = human_rhythm_prompt_section(request, trends)
    structure_section = structure_variation_prompt_section(request, trends)
    product_mention_section = product_mention_prompt_section(anchor, request=request, trends=trends)
    return f"""You are the hidden content strategist for this workspace's posts.

Do not write the post yet. Produce the structured Post Intent that the natural writer will use.

Your job:
- Turn the user's raw idea into a clear thesis, angle, and post shape.
- Ground the post in supplied context, examples, trends, patterns, or the user's exact words.
- If using a trend, company, product, or case study, name the exact mechanism that transfers into the saved context.
- If the source-to-context bridge is weak, do not force the trend; choose a grounded observation/take instead.
- Decide what the writer may safely imply and what it must not invent.
- If there is no real anecdote, source, quote, metric, or lived event, set the post shape to observation/take instead of story.
- Do not invent customer conversations, personal activities, research sessions, meetings, metrics, quotes, product facts, or examples.
- For product_update and launch posts, do not invent shipped features, integrations, launch timing, product metrics, or "now live" claims. If the request does not supply a concrete product change, tell the writer to use saved product context as a product belief instead of a fake announcement.
- Keep humor dry and grounded. Avoid jokes that become the point.

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "choose the strongest specific angle"}
- Audience: {request.audience or "the saved target audience"}
- Extra context: {request.extra_context or "none"}
{length_section}

Facts & Context:
{context or "- No saved facts/context yet."}

{active_rules_section}

{natural_voice_section}

{human_rhythm_section}

{structure_section}

{product_mention_section}

Voice Examples and worked-reason notes:
{examples_text or "No examples selected."}

Trend context:
{trends_text or "No timely trends selected."}

Pattern mechanics:
{patterns_text or "No Pattern Scout examples selected."}

Analytics hints:
{analytics or "- No analytics hints available."}

{avoid_section}

Return compact JSON only with these keys:
thesis, angle, intended_audience, reader_problem, why_this_is_worth_posting, concrete_detail, source_material, source_bridge, evidence_boundary, post_shape, hook_direction, tone_direction, claims_to_make, claims_to_avoid, forbidden_moves, success_criteria.

Field guidance:
- thesis: one plain sentence the post is trying to make true in the reader's head.
- concrete_detail: one specific noun, mechanism, situation, or contrast from the request/context. If thin, use the best grounded mechanism and say it is thin.
- source_material: what the writer is allowed to use as evidence.
- source_bridge: the specific source fact -> mechanism -> saved context connection. If the bridge is weak, say it is weak and tell the writer not to use the source.
- evidence_boundary: what the writer must not pretend happened.
- post_shape: one of observation, product lesson, teardown, market take, trend response, customer-inspired, short contrarian claim.
- forbidden_moves: include unsupported first-person setup, fake anecdotes, generic AI hype, shallow topical pegs, unexplained "same problem" bridges, and any request-specific traps.
- success_criteria: concise checklist for the evaluator. It must include source bridge strength, saved-context specificity, and why the post is worth publishing now."""


def build_best_writer_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    strategy_summary: dict[str, object],
    max_chars: int,
    avoid_phrases: list[str] | None = None,
    active_rules: dict[str, list[str]] | None = None,
) -> str:
    skill_text = "\n\n".join(f"## {skill.path}\n{skill.content}" for skill in skills)
    examples_text = "\n\n".join(_format_example(post, bundle) for post in bundle.examples)
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    trends_text = "\n\n".join(_format_trend(trend) for trend in trends)
    patterns_text = "\n\n".join(_format_viral_pattern(pattern) for pattern in patterns)
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    avoid_section = avoid_phrases_prompt_section(avoid_phrases or [])
    active_rules_section = active_rules_prompt_section(
        positioning_rules=(active_rules or {}).get("positioning_rules", []),
        voice_rules=(active_rules or {}).get("voice_rules", []),
    )
    length_section = length_prompt_section(request, max_chars)
    anchor = workspace_anchor_name(bundle, active_rules or {})
    natural_voice_section = natural_voice_prompt_section(bundle, anchor=anchor)
    human_rhythm_section = human_rhythm_prompt_section(request, trends)
    structure_section = structure_variation_prompt_section(request, trends)
    product_mention_section = product_mention_prompt_section(anchor, request=request, trends=trends)
    option_variation_section = draft_option_variation_matrix(request, trends, count=3)
    return f"""Write 3 X draft options for this workspace.

Prioritize exact Voice Examples, worked-reason notes, and the writing skills below over generic marketing instincts.

Hidden Post Intent from the strategist:
{json.dumps(strategy_summary, indent=2)}

Use this intent as the content source of truth. The strategist can infer the angle, but you may not invent evidence.

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Tone: {request.tone or "plain, sharp, useful"}
- CTA: {request.call_to_action or "only include if natural"}
- Extra context: {request.extra_context or "none"}
{length_section}

Exact Voice Examples:
{examples_text or "No examples selected."}

Facts & Context:
{context or "- No saved facts/context yet."}

{active_rules_section}

{natural_voice_section}

{human_rhythm_section}

{structure_section}

{product_mention_section}

{option_variation_section}

Trend context:
{trends_text or "No timely trends selected."}

Pattern mechanics:
{patterns_text or "No Pattern Scout examples selected."}

Performance hints:
{analytics or "- No analytics hints available."}

Writing and review instructions:
{skill_text}

{avoid_section}

Grounding rules:
- Do not write unsupported first-person setups like "I spent the morning...", "I was talking to...", "a customer told me...", or "we tested..." unless the Post Intent/source material explicitly supports them.
- If no real anecdote is supplied, write an observation, take, teardown, or product lesson.
- Keep one concrete detail from the Post Intent visible in the draft.
- If using Trend context or another named company/product/case study, make the bridge explicit: source fact -> transferable mechanism -> saved reader/product context.
- Do not use "same problem", "part to watch", or similar shorthand unless the draft immediately names the concrete mechanism and why it matters.
- If the source bridge is weak or generic, do not force the source; write a grounded evergreen post from saved Facts & Context instead.
- Do not add new stats, quotes, customer claims, product facts, or examples.
- For product_update and launch posts, only announce a shipped change if the request or saved context names that change. If no specific change is supplied, make it a concrete product belief or capability from saved context.
- Humor is allowed only if it sharpens the thesis.

Avoid corporate thought leadership, generic hype, fake profundity, em dashes, overly symmetrical phrasing, LinkedIn-style moral lessons, and stock X-but-Y framing.
Use Pattern mechanics only as structure. Do not copy source post wording or make the post feel derivative. Make it specific to the saved Facts & Context.

Each option must be {max_chars} characters or fewer and end as a complete thought.
Return only draft post text variants. Separate variants with a line containing only ---."""


def build_evaluation_prompt(
    request: DraftRequest,
    *,
    skills: list[SkillAsset],
    strategy_summary: dict[str, object],
    draft_options: list[str],
    trends: list[TrendItem],
    max_chars: int,
    avoid_phrases: list[str] | None = None,
    active_rules: dict[str, list[str]] | None = None,
) -> str:
    skill_text = "\n\n".join(f"## {skill.path}\n{skill.content}" for skill in skills)
    options = "\n\n".join(f"Draft {index}:\n{text}" for index, text in enumerate(draft_options, start=1))
    avoid_section = avoid_phrases_prompt_section(avoid_phrases or [])
    active_rules_section = active_rules_prompt_section(
        positioning_rules=(active_rules or {}).get("positioning_rules", []),
        voice_rules=(active_rules or {}).get("voice_rules", []),
    )
    length_section = length_prompt_section(request, max_chars)
    anchor = workspace_anchor_from_active_rules(active_rules or {})
    natural_voice_section = natural_voice_prompt_section(anchor=anchor)
    human_rhythm_section = human_rhythm_prompt_section(request, trends)
    structure_section = structure_variation_prompt_section(request, trends)
    product_mention_section = product_mention_prompt_section(anchor, request=request, trends=trends)
    return f"""Choose the best X post for this workspace and clean it up lightly.

Original request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "none"}
- Audience: {request.audience or "the saved target audience"}
{length_section}

Hidden Post Intent:
{json.dumps(strategy_summary, indent=2)}

Success criteria:
{json.dumps(strategy_summary.get("success_criteria", ""), indent=2)}

Voice and review rules:
{skill_text}

{active_rules_section}

{natural_voice_section}

{human_rhythm_section}

{structure_section}

{product_mention_section}

{avoid_section}

Draft options:
{options}

Return compact JSON only. Do not include analysis, chain-of-thought, markdown, or commentary.
Keep the JSON short enough to fit comfortably in the response.
Use these keys:
ranked_drafts, quality_notes, warnings, suggested_final_version.

Final evaluator should check:
- strategy fidelity: the draft expresses the thesis and uses the concrete detail
- post-worthiness: there is a clear reason this should be posted now; a topical source by itself is not enough
- source bridge: any trend/company/product/case study reference states the transferable mechanism instead of relying on a loose analogy
- positioning fidelity: if saved Facts & Context names a company/product in the core mechanism, the final draft must keep that name attached to the concrete saved-context mechanism
- generic swap test: the draft would still be specific if company and product names were removed; if it could apply to any workspace, fail it
- evidence safety: no invented first-person anecdote, fake customer story, fake quote, fake metric, unsupported reading/research session, or unsupported product claim
- natural voice: sounds like a specific person posting a useful thought, not an AI recap, brand post, or launch page
- structure freshness: does not use the default trend recap -> generic Leadpoet definition -> tidy closer template
- dynamic product mention: avoids standalone "Leadpoet is..." or "Leadpoet is building..." phrasing unless quoted from source
- specificity: at least one concrete noun, situation, mechanism, or contrast
- substance: the draft has a real point, not a vibe, joke, or generic AI take
- no avoid phrases
- no generic AI language
- no forced CTA
- no corporate launch tone
- product truthfulness: product_update and launch drafts must not invent shipped features, integrations, launch timing, or "now live" claims unless the request or saved context supplied them
- publishable output: the final text must be an actual post, never a refusal, caveat, instruction to provide more details, or internal quality note

If the best draft violates any check, rewrite it once in suggested_final_version while preserving the thesis. If no draft has a real source bridge or post-worthy point, include "not_post_worthy" in warnings and rewrite toward the strongest grounded evergreen point. If product specifics are missing, write a product belief grounded in saved context rather than asking for more details. If a draft uses an Avoid Phrase, revise away from it in suggested_final_version.
The suggested_final_version must be natural, specific, founder-led, match the requested length form, non-corporate, free of em dashes, {max_chars} characters or fewer, and complete."""


def build_context_snapshot(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    trends: list[TrendItem],
    patterns: list[ViralPattern],
    avoid_phrases: list[str] | None,
    active_rules: dict[str, list[str]] | None,
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
    primary_structure = primary_structure_shape(request, trends)
    primary_product_lane = primary_product_mention_lane(request, trends)
    primary_product_placement = primary_product_mention_placement_target(request, trends)
    primary_layout = primary_layout_target(request, trends)
    primary_closing = primary_closing_target(request, trends)
    primary_rhythm = primary_human_rhythm_target(request, trends)
    return {
        "workspace_id": bundle.workspace_id,
        "request": request.__dict__,
        "length_mode": normalize_length_mode(request.length_mode),
        "length_label": POST_LENGTH_PRESETS[normalize_length_mode(request.length_mode)]["label"],
        "max_chars": request.max_chars or POST_LENGTH_PRESETS[normalize_length_mode(request.length_mode)]["max_chars"],
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
        "primary_structure_option": {
            "name": primary_structure[0],
            "guidance": primary_structure[1],
        },
        "primary_product_mention_lane": {
            "name": primary_product_lane[0],
            "guidance": primary_product_lane[1],
        },
        "primary_product_mention_placement_target": {
            "name": primary_product_placement[0],
            "guidance": primary_product_placement[1],
        },
        "primary_layout_target": {
            "name": primary_layout[0],
            "guidance": primary_layout[1],
        },
        "primary_closing_target": {
            "name": primary_closing[0],
            "guidance": primary_closing[1],
        },
        "primary_human_rhythm_target": {
            "name": primary_rhythm[0],
            "guidance": primary_rhythm[1],
        },
        "structure_variation_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_structure_shapes(request, trends, count=4)
        ],
        "layout_target_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_layout_targets(request, trends, count=4)
        ],
        "closing_target_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_closing_targets(request, trends, count=4)
        ],
        "human_rhythm_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_human_rhythm_targets(request, trends, count=4)
        ],
        "product_mention_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_product_mention_lanes(request, trends, count=4)
        ],
        "product_mention_placement_options": [
            {"name": name, "guidance": guidance}
            for name, guidance in selected_product_mention_placement_targets(request, trends, count=4)
        ],
        "avoid_phrases": avoid_phrases or [],
        "active_writing_rules": active_rules or {},
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
                "what_happened": trend.summary,
                "why_it_matters": trend.why_it_matters,
                "suggested_hooks": trend.suggested_hooks,
                "source_url": trend.source_url,
                "source_confidence": trend.source_confidence,
                "enrichment_status": trend.enrichment_status,
            }
            for trend in trends
        ],
    }


def parse_strategy_summary(raw: str) -> dict[str, object]:
    parsed = parse_json_object(raw)
    if isinstance(parsed, dict):
        return {
            "thesis": str(parsed.get("thesis") or parsed.get("core_point") or "").strip(),
            "angle": str(parsed.get("angle") or parsed.get("post_angle") or "").strip(),
            "core_point": str(parsed.get("core_point") or parsed.get("thesis") or "").strip(),
            "intended_audience": str(parsed.get("intended_audience") or "").strip(),
            "reader_problem": str(parsed.get("reader_problem") or "").strip(),
            "why_timely": str(parsed.get("why_timely") or "").strip(),
            "why_this_is_worth_posting": str(
                parsed.get("why_this_is_worth_posting") or parsed.get("why_timely") or ""
            ).strip(),
            "post_angle": str(parsed.get("post_angle") or parsed.get("angle") or "").strip(),
            "concrete_detail": str(parsed.get("concrete_detail") or "").strip(),
            "source_material": _strategy_value(parsed.get("source_material")),
            "source_bridge": str(parsed.get("source_bridge") or "").strip(),
            "evidence_boundary": str(parsed.get("evidence_boundary") or "").strip(),
            "post_shape": str(parsed.get("post_shape") or "").strip(),
            "hook_direction": str(parsed.get("hook_direction") or "").strip(),
            "tone_direction": str(parsed.get("tone_direction") or "").strip(),
            "claims_to_make": _strategy_list(parsed.get("claims_to_make")),
            "claims_to_avoid": _strategy_list(parsed.get("claims_to_avoid")),
            "forbidden_moves": _strategy_list(parsed.get("forbidden_moves")),
            "what_to_avoid": str(parsed.get("what_to_avoid") or "").strip(),
            "success_criteria": _strategy_value(parsed.get("success_criteria")) or "",
        }
    return {"raw": raw.strip()}


def _strategy_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return value
    return str(value).strip()


def _strategy_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return [value]


def parse_evaluation_result(raw: str, draft_options: list[str]) -> tuple[str, str, dict[str, object]]:
    parsed = parse_json_object(raw)
    if isinstance(parsed, dict):
        final = str(parsed.get("suggested_final_version") or "").strip() or draft_options[0]
        notes = str(parsed.get("quality_notes") or "").strip()
        return final, notes, parsed
    return draft_options[0], raw.strip(), {"raw": raw.strip()}


STALE_REPAIRED_WARNING_LABELS = {
    "all_drafts_unpublishable",
    "no_usable_draft_submitted",
    "violates_publishable_output_requirement",
}


def clean_evaluation_warnings_after_final_text(
    evaluation: dict[str, object],
    *,
    text: str,
    request: DraftRequest,
    bundle: ContextBundle,
    trends: list[TrendItem],
    avoid_phrases: list[str],
    active_rules: dict[str, list[str]],
    repaired_successfully: bool,
) -> dict[str, object]:
    warnings = evaluation.get("warnings")
    if not isinstance(warnings, list):
        return evaluation
    anchor = workspace_anchor_name(bundle, active_rules)
    remaining_violations = draft_rule_violations(
        text,
        request,
        bundle,
        trends=trends,
        avoid_phrases=avoid_phrases,
        active_rules=active_rules,
    )
    cleaned: list[object] = []
    for warning in warnings:
        normalized = " ".join(str(warning or "").split())
        lowered = normalized.lower()
        if not normalized:
            continue
        if repaired_successfully and normalized in STALE_REPAIRED_WARNING_LABELS:
            continue
        if (
            anchor
            and anchor.lower() in text.lower()
            and not any("anchor the point" in violation.lower() for violation in remaining_violations)
            and (
                "positioning_gap" in lowered
                or "generic intent layer" in lowered
                or "generic 'intent layer'" in lowered
                or f"no draft names {anchor.lower()}" in lowered
                or f"missing {anchor.lower()}" in lowered
            )
        ):
            continue
        cleaned.append(warning)
    return {**evaluation, "warnings": cleaned}


def repair_draft_against_rules(
    provider: DraftProvider,
    text: str,
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    avoid_phrases: list[str],
    active_rules: dict[str, list[str]],
    max_chars: int,
    model: str | None,
) -> tuple[str, list[str]]:
    violations = draft_rule_violations(
        text,
        request,
        bundle,
        trends=trends,
        avoid_phrases=avoid_phrases,
        active_rules=active_rules,
    )
    repaired = text
    for _ in range(2):
        if not violations:
            return repaired, []
        repair_prompt = build_rule_repair_prompt(
            repaired,
            violations=violations,
            request=request,
            bundle=bundle,
            trends=trends,
            active_rules=active_rules,
            max_chars=max_chars,
        )
        repair_options = generate_with_provider(provider, repair_prompt, variants=1, model=model)
        if not repair_options:
            return apply_deterministic_rule_repair(
                repaired,
                request,
                bundle,
                trends=trends,
                avoid_phrases=avoid_phrases,
                active_rules=active_rules,
                max_chars=max_chars,
                previous_violations=violations,
            )
        repaired_raw = repair_options[0]
        repaired = fit_generated_text_to_limit(
            provider,
            repaired_raw,
            max_chars=max_chars,
            model=model,
            repair_context="rule-repaired X post",
        )
        violations = draft_rule_violations(
            repaired,
            request,
            bundle,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
        )
    repaired, violations = apply_deterministic_rule_repair(
        repaired,
        request,
        bundle,
        trends=trends,
        avoid_phrases=avoid_phrases,
        active_rules=active_rules,
        max_chars=max_chars,
        previous_violations=violations,
    )
    anchored = anchor_draft_to_workspace(
        repaired,
        request,
        bundle,
        trends=trends,
        active_rules=active_rules,
        max_chars=max_chars,
    )
    if anchored != repaired:
        violations = draft_rule_violations(
            anchored,
            request,
            bundle,
            trends=trends,
            avoid_phrases=avoid_phrases,
            active_rules=active_rules,
        )
        return anchored, violations
    return repaired, violations


def apply_deterministic_rule_repair(
    text: str,
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    avoid_phrases: list[str],
    active_rules: dict[str, list[str]],
    max_chars: int,
    previous_violations: list[str],
) -> tuple[str, list[str]]:
    repaired = deterministic_leadpoet_definition_repair(
        text,
        request=request,
        trends=trends,
        max_chars=max_chars,
    )
    if repaired == text:
        return text, previous_violations
    violations = draft_rule_violations(
        repaired,
        request,
        bundle,
        trends=trends,
        avoid_phrases=avoid_phrases,
        active_rules=active_rules,
    )
    if len(violations) <= len(previous_violations):
        return repaired, violations
    return text, previous_violations


def deterministic_leadpoet_definition_repair(
    text: str,
    *,
    request: DraftRequest,
    trends: list[TrendItem],
    max_chars: int,
) -> str:
    repaired = text
    product_lane = primary_product_mention_lane(request, trends)[0]
    for pattern in [
        r"\bLeadpoet\s+is\s+building[^.!?\n]*(?:[.!?]|$)",
        r"\bLeadpoet\s+is\s+built[^.!?\n]*(?:[.!?]|$)",
        r"\bLeadpoet\s+is[^.!?\n]*(?:[.!?]|$)",
        r"\bLeadpoet(?:'s|’s)\s+(?:bet|job|role)\s+is[^.!?\n]*(?:[.!?]|$)",
        r"\bThe\s+Leadpoet\s+(?:product belief|signal|point|constraint|job|bar|part)\s+is[^.!?\n]*(?:[.!?]|$)",
    ]:
        repaired = re.sub(
            pattern,
            lambda match: leadpoet_definition_replacement(match, product_lane=product_lane),
            repaired,
            flags=re.I,
        )
    repaired = re.sub(
        r"(?m)(^|\n\n)(\s*)For\s+Leadpoet,\s*([^.!?\n]*(?:[.!?]|$))",
        lambda match: leadpoet_formulaic_intro_replacement(match, product_lane=product_lane),
        repaired,
        flags=re.I,
    )
    repaired = re.sub(
        r"(?m)(^|(?<=[.!?])\s+|\n\n)(\s*)Leadpoet\s+(should|can|needs?|has\s+to)\s+([^.!?\n]*(?:[.!?]|$))",
        lambda match: leadpoet_modal_leadin_replacement(match, product_lane=product_lane),
        repaired,
        flags=re.I,
    )
    repaired = re.sub(
        r"(?m)(^|(?<=[.!?])\s+|\n\n)(\s*)(?:That|This)\s+is\s+where\s+Leadpoet\s+([^.!?\n]*(?:[.!?]|$))",
        lambda match: leadpoet_where_leadin_replacement(match, product_lane=product_lane),
        repaired,
        flags=re.I,
    )
    repaired = re.sub(
        r"(?m)(^|(?<=[.!?])\s+|\n\n)(\s*)That['’]?s\s+where\s+Leadpoet\s+([^.!?\n]*(?:[.!?]|$))",
        lambda match: leadpoet_where_leadin_replacement(match, product_lane=product_lane),
        repaired,
        flags=re.I,
    )
    cleaned = clean_generated_text(repaired, max_chars=max_chars)
    if not cleaned or len(cleaned) > max_chars:
        return text
    return cleaned


def leadpoet_definition_replacement(match: re.Match[str], *, product_lane: str) -> str:
    original = " ".join(match.group(0).strip().split())
    lowered = original.lower()
    lane_replacements = {
        "mechanism-first": "The hard part is turning context into the next account worth working. Leadpoet belongs in that mechanism, not in a category line.",
        "consequence-first": "A rep should know why this account, this week. Leadpoet only matters when that answer changes the next action.",
        "quiet embedded name": "Put Leadpoet inside the account reason: what changed, why now, and why copy comes after that.",
        "no-name wedge": "The useful wedge is buyer timing: why this account deserves action now.",
        "operator scene": "A rep should see what changed at the account before a sequence asks for copy.",
        "boundary line": "Verified data can say who exists; the sharper signal says who is worth action now.",
        "signal language": "The signal has to explain why now, not only who fits. Keep Leadpoet next to that account change.",
        "ownership without definition": "Before louder outbound, the account-timing question is the part Leadpoet must get right.",
        "implicit product belief": "Timing should choose the account before copy writes the email. Inside Leadpoet, that belief beats another volume claim.",
    }
    if product_lane in lane_replacements:
        return lane_replacements[product_lane]
    if "not another" in lowered or "not a " in lowered or "not the " in lowered:
        return lane_replacements["consequence-first"]
    if "context" in lowered:
        return lane_replacements["mechanism-first"]
    if "timing" in lowered or "intent" in lowered:
        return lane_replacements["signal language"]
    if "layer" in lowered or "signal" in lowered:
        return lane_replacements["ownership without definition"]
    return lane_replacements["quiet embedded name"]


def leadpoet_formulaic_intro_replacement(match: re.Match[str], *, product_lane: str) -> str:
    prefix = match.group(1) or ""
    indent = match.group(2) or ""
    body = " ".join((match.group(3) or "").strip().split())
    ending = "."
    if body and body[-1] in ".!?":
        ending = body[-1]
        body = body[:-1].strip()
    lowered_body = body.lower()
    if lowered_body.startswith("the harder question is "):
        tail = body[len("the harder question is "):]
        return f"{prefix}{indent}The harder question is {tail}{ending} Leadpoet belongs close to that account-timing problem, not in a definition line."
    if lowered_body.startswith("account context has to "):
        tail = body[len("account context has to "):]
        return f"{prefix}{indent}Account context has to {tail}{ending} That is the part Leadpoet must make useful."
    if lowered_body.startswith("timing means "):
        tail = body[len("timing means "):]
        return f"{prefix}{indent}Timing is useful when it means {tail}{ending} That is Leadpoet's bar."
    if lowered_body.startswith("buyer timing is "):
        tail = body[len("buyer timing is "):]
        return f"{prefix}{indent}Buyer timing stays tied to {tail}{ending} The Leadpoet part gets specific there."
    if body:
        body = body[0].lower() + body[1:]
    body = f"{body}{ending}" if body else ""
    lane_templates = {
        "mechanism-first": "Start with the mechanism: {body} Leadpoet only matters if that becomes usable.",
        "consequence-first": "That only matters if Leadpoet makes it actionable: {body}",
        "quiet embedded name": "A useful Leadpoet mention sits inside the claim: {body}",
        "operator scene": "A rep should feel the account signal before copy starts: {body}",
        "boundary line": "That is the sharper side of the boundary for Leadpoet: {body}",
        "signal language": "The signal has to make this plain before Leadpoet earns the mention: {body}",
        "ownership without definition": "The account-timing question can carry the Leadpoet name without a definition line: {body}",
        "implicit product belief": "Inside Leadpoet, keep the belief concrete: {body}",
    }
    if product_lane == "no-name wedge":
        template = "The useful wedge is buyer timing: {body}"
    else:
        template = lane_templates.get(product_lane, "A useful Leadpoet mention sits inside the claim: {body}")
    replacement = template.format(body=body or "buyer timing changes the account choice.")
    return f"{prefix}{indent}{replacement}"


def leadpoet_modal_leadin_replacement(match: re.Match[str], *, product_lane: str) -> str:
    prefix = match.group(1) or ""
    indent = match.group(2) or ""
    body = " ".join((match.group(4) or "").strip().split())
    ending = "."
    if body and body[-1] in ".!?":
        ending = body[-1]
        body = body[:-1].strip()
    body = body[0].lower() + body[1:] if body else "make buyer timing visible"
    templates = {
        "mechanism-first": "The mechanism has to {body}. Leadpoet only matters if that becomes usable.",
        "consequence-first": "That only matters when Leadpoet turns it into action: {body}.",
        "quiet embedded name": "A useful Leadpoet mention sits next to the mechanism that can {body}.",
        "no-name wedge": "The useful signal has to {body}.",
        "operator scene": "A rep should feel the account signal when Leadpoet can {body}.",
        "boundary line": "Verified data is not enough; the Leadpoet side of the line is whether it can {body}.",
        "signal language": "The useful signal is the one Leadpoet can use to {body}.",
        "ownership without definition": "The account-timing question is the Leadpoet part when it can {body}.",
        "implicit product belief": "Inside Leadpoet, keep the belief concrete: {body}.",
    }
    replacement = templates.get(product_lane, templates["quiet embedded name"]).format(body=body)
    if ending != "." and replacement.endswith("."):
        replacement = f"{replacement[:-1]}{ending}"
    return f"{prefix}{indent}{replacement}"


def leadpoet_where_leadin_replacement(match: re.Match[str], *, product_lane: str) -> str:
    prefix = match.group(1) or ""
    indent = match.group(2) or ""
    body = " ".join((match.group(3) or "").strip().split())
    ending = "."
    if body and body[-1] in ".!?":
        ending = body[-1]
        body = body[:-1].strip()
    lowered = body.lower()
    if lowered.startswith("focuses:"):
        detail = body.split(":", 1)[1].strip()
        replacement = f"{detail[0].upper() + detail[1:] if detail else 'Timing before copy'} is the part Leadpoet keeps visible."
    elif lowered.startswith("focuses on "):
        detail = body[len("focuses on "):].strip()
        replacement = f"{detail[0].upper() + detail[1:] if detail else 'Buyer timing'} is the part Leadpoet keeps visible."
    elif lowered.startswith(("should ", "can ", "needs ", "need ", "has to ")):
        replacement = f"Leadpoet's constraint is concrete: {body}."
    else:
        templates = {
            "mechanism-first": "The mechanism should carry Leadpoet into the sentence: {body}.",
            "consequence-first": "The consequence should carry Leadpoet into the sentence: {body}.",
            "quiet embedded name": "A useful Leadpoet mention stays concrete: {body}.",
            "no-name wedge": "The useful wedge is concrete: {body}.",
            "operator scene": "A rep should feel the Leadpoet part in the work: {body}.",
            "boundary line": "The sharper boundary for Leadpoet is this: {body}.",
            "signal language": "The account signal gets specific around Leadpoet here: {body}.",
            "ownership without definition": "The account-timing question keeps Leadpoet grounded here: {body}.",
            "implicit product belief": "Inside Leadpoet, the product belief stays concrete: {body}.",
        }
        replacement = templates.get(product_lane, templates["quiet embedded name"]).format(
            body=body[0].lower() + body[1:] if body else "buyer timing changes the account choice"
        )
    if ending != "." and replacement.endswith("."):
        replacement = f"{replacement[:-1]}{ending}"
    return f"{prefix}{indent}{replacement}"


def draft_rule_violations(
    text: str,
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    avoid_phrases: list[str],
    active_rules: dict[str, list[str]],
) -> list[str]:
    lowered = text.lower()
    violations: list[str] = []
    for phrase in avoid_phrases:
        phrase = " ".join(str(phrase or "").lower().split())
        if not phrase:
            continue
        if phrase == "not just x, but y":
            if "not just" in lowered:
                violations.append("avoid 'not just X, but Y' framing")
            continue
        if phrase in lowered:
            violations.append(f"avoid phrase: {phrase}")
    if "—" in text:
        violations.append("remove em dash")
    if re.search(r"\b(cannot proceed|no product update supplied|provide:|provide more details|need more details|to write this post)\b", lowered):
        violations.append("return a publishable post, not a refusal, caveat, or request for more details")
    violations.extend(natural_voice_violations(text, request))
    anchor = workspace_anchor_name(bundle, active_rules)
    if anchor and re.search(rf"\b(i do not|i don't) know enough about {re.escape(anchor.lower())}\b", lowered):
        violations.append(f"remove model uncertainty about {anchor}; use saved context or make a narrower claim")
    if (
        anchor
        and should_require_anchor(request, trends)
        and not product_lane_allows_omitting_anchor(request, trends)
        and anchor.lower() not in lowered
    ):
        violations.append(f"anchor the point to {anchor} as the source of the core wedge")
    return dedupe_preserve_order(violations)


def natural_voice_violations(text: str, request: DraftRequest) -> list[str]:
    violations: list[str] = []
    for pattern, label in NATURAL_VOICE_POLISH_PATTERNS:
        if re.search(pattern, text, flags=re.I):
            violations.append(label)
    for pattern, label in STOCK_CONTRAST_PATTERNS:
        if re.search(pattern, text, flags=re.I):
            violations.append(label)
    for pattern, label in LEADPOET_DEFINITION_PATTERNS:
        if re.search(pattern, text, flags=re.I):
            violations.append(label)
    if not request_supports_first_person_evidence(request):
        for pattern, label in UNSUPPORTED_FIRST_PERSON_PATTERNS:
            if re.search(pattern, text, flags=re.I):
                violations.append(label)
    return dedupe_preserve_order(violations)


def request_supports_first_person_evidence(request: DraftRequest) -> bool:
    haystack = " ".join(
        str(value or "")
        for value in [
            request.angle,
            request.extra_context,
            request.call_to_action,
        ]
    ).lower()
    if not haystack:
        return False
    return any(
        marker in haystack
        for marker in [
            "i spent",
            "i talked",
            "talked to",
            "customer told",
            "we tested",
            "we ran",
            "we analyzed",
            "we surveyed",
            "we interviewed",
            "i dug into",
            "i read through",
            "i analyzed",
            "after talking to",
        ]
    )


def workspace_anchor_name(bundle: ContextBundle, active_rules: dict[str, list[str]]) -> str | None:
    sources = [
        *active_rules.get("positioning_rules", []),
        *(section.content for section in bundle.positioning_sections),
        *active_rules.get("voice_rules", []),
    ]
    for source in sources:
        candidate = first_workspace_anchor_candidate(source)
        if candidate:
            return candidate
    haystack = "\n".join(sources)
    if re.search(r"\bLeadpoet\b", haystack):
        return "Leadpoet"
    return None


def workspace_anchor_from_active_rules(active_rules: dict[str, list[str]]) -> str | None:
    sources = [
        *active_rules.get("positioning_rules", []),
        *active_rules.get("voice_rules", []),
        *active_rules.get("review_rules", []),
    ]
    for source in sources:
        candidate = first_workspace_anchor_candidate(source)
        if candidate:
            return candidate
    haystack = "\n".join(sources)
    if re.search(r"\bLeadpoet\b", haystack):
        return "Leadpoet"
    return None


ANCHOR_STOPWORDS = {
    "A",
    "An",
    "And",
    "At",
    "Bittensor",
    "Context",
    "Do",
    "Facts",
    "For",
    "Gavin",
    "If",
    "Koinoniko",
    "No",
    "Other",
    "Positioning",
    "Rules",
    "Sales",
    "The",
    "This",
    "Use",
    "Voice",
    "When",
    "X",
}


def first_workspace_anchor_candidate(text: str) -> str | None:
    patterns = [
        r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})\s+(?:is|owns|helps|turns|gives|makes|builds|provides)\b",
        r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})'s\s+(?:core|unique|main|primary|product|platform|wedge|advantage)\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = " ".join(match.group(1).split())
            if is_valid_workspace_anchor(candidate):
                return candidate
    return None


def is_valid_workspace_anchor(candidate: str) -> bool:
    if not candidate:
        return False
    words = candidate.split()
    if len(words) > 3:
        return False
    if candidate in ANCHOR_STOPWORDS:
        return False
    if any(word in ANCHOR_STOPWORDS for word in words):
        return False
    if candidate.isupper() and len(candidate) <= 2:
        return False
    return True


def anchor_draft_to_workspace(
    text: str,
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    active_rules: dict[str, list[str]],
    max_chars: int,
) -> str:
    anchor = workspace_anchor_name(bundle, active_rules)
    if (
        not anchor
        or not should_require_anchor(request, trends)
        or product_lane_allows_omitting_anchor(request, trends)
        or anchor.lower() in text.lower()
    ):
        return text
    anchor_line = workspace_anchor_line(anchor, request, bundle, trends=trends, active_rules=active_rules)
    placement_target = primary_product_mention_placement_target(request, trends)[0]
    candidates = workspace_anchor_candidates(text, anchor_line, placement_target)
    for candidate in candidates:
        cleaned = clean_generated_text(candidate, max_chars=max_chars)
        if len(cleaned) <= max_chars and anchor.lower() in cleaned.lower():
            return cleaned
    return text


def workspace_anchor_candidates(text: str, anchor_line: str, placement_target: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return [anchor_line]
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", stripped) if paragraph.strip()]
    if not paragraphs:
        return [anchor_line]
    first_line = first_sentence_or_line(stripped)
    candidates: list[str] = []
    if placement_target == "early embedded":
        candidates.extend([
            f"{anchor_line}\n\n{stripped}",
            f"{first_line}\n\n{anchor_line}",
        ])
    elif placement_target == "middle turn":
        if len(paragraphs) > 1:
            candidates.append("\n\n".join([paragraphs[0], anchor_line, *paragraphs[1:]]))
        candidates.append(f"{first_line}\n\n{anchor_line}")
    elif placement_target == "late consequence":
        candidates.append(f"{stripped}\n\n{anchor_line}")
    elif placement_target == "product-free close":
        if len(paragraphs) > 1:
            candidates.append("\n\n".join([*paragraphs[:-1], anchor_line, paragraphs[-1]]))
        candidates.append(f"{anchor_line}\n\n{stripped}")
    elif placement_target == "quiet tuck-in":
        candidates.extend([
            f"{first_line}\n\n{anchor_line}",
            f"{stripped}\n\n{anchor_line}",
        ])
    candidates.extend([
        f"{stripped}\n\n{anchor_line}",
        f"{first_line}\n\n{anchor_line}",
        anchor_line,
    ])
    return dedupe_preserve_order(candidates)


def workspace_anchor_line(
    anchor: str,
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
    active_rules: dict[str, list[str]],
) -> str:
    if anchor.lower() == "leadpoet":
        return leadpoet_anchor_line(primary_product_mention_lane(request, trends)[0])
    sources = [
        *active_rules.get("positioning_rules", []),
        *(section.content for section in bundle.positioning_sections),
    ]
    for source in sources:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", source):
            sentence = " ".join(sentence.strip(" -•\t").split())
            if anchor.lower() not in sentence.lower():
                continue
            if ":" in sentence:
                prefix = sentence.split(":", 1)[0].strip()
                if anchor.lower() in prefix.lower():
                    sentence = f"{prefix}."
            if len(sentence) <= 120:
                return sentence
    return f"For {anchor}, the useful layer starts with proprietary context and timing."


def leadpoet_anchor_line(product_lane: str) -> str:
    lines = {
        "mechanism-first": "Buyer timing has to become visible before a sequence starts; that account-change check is where Leadpoet belongs.",
        "consequence-first": "The account choice gets better when the why-now reason is visible inside Leadpoet.",
        "quiet embedded name": "Put Leadpoet inside the account reason before the copy starts.",
        "no-name wedge": "The account reason can carry the post; Leadpoet does not need the last-line tagline.",
        "operator scene": "A rep should see the changed account before the generated email. Put Leadpoet at that handoff.",
        "boundary line": "Verified data can say who exists; the Leadpoet side is who is worth action now.",
        "signal language": "The useful account signal ties timing, context, and the next account before Leadpoet enters the copy.",
        "ownership without definition": "Before outbound gets louder, the account-timing question is the Leadpoet work.",
        "implicit product belief": "Inside Leadpoet, the belief stays practical: timing should pick the account before copy writes the email.",
    }
    return lines.get(product_lane, lines["signal language"])


def first_sentence_or_line(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    match = re.match(r"(.+?[.!?])(?:\s|$)", cleaned)
    return match.group(1).strip() if match else cleaned


def should_require_anchor(request: DraftRequest, trends: list[TrendItem]) -> bool:
    topic = (request.topic or "").lower()
    post_type = (request.post_type or "").lower()
    if "bittensor" in topic or "bittensor" in post_type:
        return False
    if trends:
        return True
    return any(
        marker in f"{topic} {post_type}"
        for marker in [
            "leadpoet",
            "sales",
            "intent",
            "founder",
            "product",
            "launch",
            "investor",
            "technical",
        ]
    )


def product_lane_allows_omitting_anchor(request: DraftRequest, trends: list[TrendItem]) -> bool:
    return primary_product_mention_lane(request, trends)[0] == "no-name wedge"


def build_rule_repair_prompt(
    text: str,
    *,
    violations: list[str],
    request: DraftRequest,
    bundle: ContextBundle,
    trends: list[TrendItem],
    active_rules: dict[str, list[str]] | None = None,
    max_chars: int,
) -> str:
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    length_section = length_prompt_section(request, max_chars)
    anchor = workspace_anchor_name(bundle, active_rules or {})
    natural_voice_section = natural_voice_prompt_section(bundle, anchor=anchor)
    human_rhythm_section = human_rhythm_prompt_section(request, trends)
    product_mention_section = product_mention_prompt_section(anchor, request=request, trends=trends)
    structure_section = structure_variation_prompt_section(request, trends)
    return f"""Repair this X post so it follows the active Voice and context rules.

Return only the revised post text.

Violations to fix:
{chr(10).join(f"- {violation}" for violation in violations)}

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "none"}
{length_section}

Facts & Context:
{context or "- No saved facts/context available."}

{natural_voice_section}

{human_rhythm_section}

{structure_section}

{product_mention_section}

Rules:
- Keep the same core point.
- Make it sound natural and founder-written.
- Do not make it more polished or corporate.
- Prefer one concrete mechanism or situation over a polished summary.
- If a violation mentions a product-definition sentence, rewrite that sentence into a mechanism, scene, or consequence instead of another "Leadpoet is..." line.
- If a violation mentions a formulaic product lead-in, remove the preface and make the product name part of the actual claim.
- If a violation mentions a formulaic Leadpoet modal lead-in, make Leadpoet the object, mechanism, or constraint inside the sentence instead of the sentence subject.
- If a violation mentions "that is where Leadpoet...", make the point concrete without using that wrapper.
- If a violation mentions a formulaic Leadpoet closer, move the product name inside a concrete mechanism or remove it.
- Do not add unsupported claims, anecdotes, proof, metrics, or customer stories.
- Do not include the exact words "not just".
- Do not refuse, caveat, or ask for more details. Return a publishable post.
- If product detail is missing, write a product belief from saved Facts & Context instead of a fake announcement.
- Stay under {max_chars} characters.
- End as a complete thought.

Draft:
{text}"""


def dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


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


def clean_draft_options(
    provider: DraftProvider,
    raw_options: list[str],
    *,
    max_chars: int,
    model: str | None,
    repair_context: str,
) -> list[str]:
    cleaned_options: list[str] = []
    for raw in raw_options:
        try:
            text = fit_generated_text_to_limit(
                provider,
                raw,
                max_chars=max_chars,
                model=model,
                repair_context=repair_context,
            )
        except ValueError:
            continue
        if text:
            cleaned_options.append(text)
    return cleaned_options


def build_writer_retry_prompt(writer_prompt: str, *, max_chars: int, anchor: str | None = None) -> str:
    anchor_instruction = (
        f"Keep the {anchor}/source bridge explicit when the topic is sales, AI, intent, product, launch, or investor-facing."
        if anchor
        else "Keep the saved-context/source bridge explicit when the topic is sales, AI, intent, product, launch, or investor-facing."
    )
    return f"""{writer_prompt}

The previous draft options were not usable.

Return one complete X post only.
Rules:
- Stay under {max_chars} characters.
- Use the saved Facts & Context.
- {anchor_instruction}
- Make it sound like a specific person writing plainly, not an AI recap or brand post.
- Do not include avoid phrases, em dashes, markdown, numbering labels, or commentary.
- Do not invent shipped features, customer stories, product metrics, launch timing, or private proof.
- Do not refuse or ask the user for missing details. If product specifics are thin, write a product belief from saved context."""


def fit_generated_text_to_limit(
    provider: DraftProvider,
    text: str,
    *,
    max_chars: int,
    model: str | None = None,
    repair_context: str = "generated X post",
) -> str:
    cleaned = clean_generated_text(text, max_chars=max_chars)
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    repair_prompt = build_length_repair_prompt(cleaned, max_chars=max_chars, repair_context=repair_context)
    repaired_options = generate_with_provider(provider, repair_prompt, variants=1, model=model)
    repaired = clean_generated_text(repaired_options[0] if repaired_options else "", max_chars=max_chars)
    if not repaired:
        raise ValueError("Provider returned an empty shortened post.")
    if len(repaired) > max_chars:
        raise ValueError(
            f"Provider returned a {len(repaired)} character post after shortening, over the {max_chars} character limit. "
            "Try a shorter instruction or ask for a tighter post."
        )
    return repaired


def build_length_repair_prompt(text: str, *, max_chars: int, repair_context: str) -> str:
    return f"""Shorten this {repair_context} to {max_chars} characters or fewer.

Rules:
- Preserve the core point.
- Preserve the saved Voice profile.
- End as a complete thought.
- Do not end mid-sentence, mid-phrase, or with a dangling setup.
- Return only the revised post text. No commentary.

Original ({len(text)} characters):
{text}"""


def clean_generated_text(text: str, *, max_chars: int) -> str:
    _ = max_chars
    cleaned = re.sub(r"(\S)\s+[—–]\s+([^—–\n]+?)\s+[—–]\s+", r"\1: \2 ", text)
    cleaned = re.sub(r"\s+[—–]\s+", ": ", cleaned)
    cleaned = cleaned.replace("—", ": ").replace("–", "-")
    cleaned = re.sub(r"\bnot just ([^.\n,;:]{1,80}),?\s+but\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r",\s*not just\s+", " instead of ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(game-changing|revolutionary|unlock|delve into|in today's world)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


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
        parts.append(f"facts/context: {', '.join(kinds)}")
    if bundle.analytics_hints:
        parts.append("recent performance hints")
    if trends:
        parts.append(f"{len(trends)} timely trend(s)")
    return "Influenced by " + "; ".join(parts) + "." if parts else "Influenced by the saved Voice rules."


def build_quality_notes(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    trends: list[TrendItem],
) -> str:
    angle = request.angle or request.topic
    example_note = f"{len(bundle.examples)} example(s)" if bundle.examples else "saved facts/context"
    trend_note = f" and {len(trends)} trend(s)" if trends else ""
    return (
        f"Fits the requested angle by centering '{angle}'. "
        f"Grounded in {example_note}{trend_note}. "
        "Review for specificity, real point of view, natural voice, and audience relevance."
    )


def _format_example(post, bundle: ContextBundle) -> str:
    note = bundle.notes_by_post_id.get(post.id)
    performance = _performance_summary(bundle.metrics_by_post_id.get(post.id))
    parts = [
        "Example:",
        post.text,
        "",
        "Why it worked:",
        (note.worked_reason if note and note.worked_reason else "Strong Voice Example."),
    ]
    if performance:
        parts.extend(["", "Performance:", performance])
    return "\n".join(parts)


def _format_trend(trend: TrendItem) -> str:
    hooks = _trend_list(trend.suggested_hooks)[:3]
    key_points = _trend_json_list(trend.key_points_json)
    claims = _trend_json_list(trend.important_claims_json)
    post_angles = _trend_json_list(trend.post_angles_json)
    what_is_new = claims[0] if claims else (key_points[1] if len(key_points) > 1 else trend.source_excerpt or "The timing or market context around this signal has changed.")
    why_gtm = key_points[2] if len(key_points) > 2 else trend.why_it_matters
    source_label = " ".join(
        part for part in [
            trend.source_author or trend.source_title,
            f"({trend.source_url})" if trend.source_url else "",
        ]
        if part
    ) or "No source URL stored."
    parts = [
        "Trend:",
        trend.title,
        "",
        "What happened:",
        trend.summary,
        "",
        "What is new:",
        what_is_new,
        "",
        "Why it matters:",
        trend.why_it_matters,
        "",
        "Why it matters to AI / sales / lead generation / GTM:",
        why_gtm,
        "",
        "Saved angle:",
        trend.leadpoet_angle,
        "",
        "Suggested hooks:",
        "\n".join(f"- {hook}" for hook in hooks) if hooks else "- No hooks stored.",
        "",
        "Possible post angles:",
        "\n".join(f"- {angle}" for angle in post_angles[:3]) if post_angles else "- Use the saved angle above.",
        "",
        "Source:",
        source_label,
    ]
    return "\n".join(parts)


def _trend_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _trend_list(value)
    if not isinstance(parsed, list):
        return []
    return [item for item in (str(raw).strip() for raw in parsed) if item]


def _trend_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        item.strip(" -")
        for item in re.split(r"\n+|;", value)
        if item.strip(" -")
    ]


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
