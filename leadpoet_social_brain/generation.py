from __future__ import annotations

import re

from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContextBundle, DraftRequest, GeneratedDraft
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.providers import DraftProvider
from leadpoet_social_brain.skills import SkillAsset, generation_skills
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


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
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        prompt = build_generation_prompt(request, bundle, skills=skills, max_chars=max_chars)
        if self.provider:
            raw_texts = self.provider.generate(prompt, variants=variants)
        else:
            raw_texts = fallback_generate(request, bundle, variants=variants, max_chars=max_chars)
        drafts: list[GeneratedDraft] = []
        for raw_text in raw_texts:
            text = clean_generated_text(raw_text, max_chars=max_chars)
            adapter.validate_text(text, max_chars=max_chars)
            draft = GeneratedDraft(
                text=text,
                request=request,
                explanation=explain_influence(bundle),
                influences=bundle.influence_labels(),
                context_snapshot=build_context_snapshot(request, bundle, skills=skills),
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
        skills = generation_skills(platform=request.platform, post_type=request.post_type)
        return build_generation_prompt(request, bundle, skills=skills, max_chars=max_chars)

    def _adapter_for(self, platform: str):
        if platform == "x":
            return get_x_adapter()
        raise ValueError(f"Unsupported platform '{platform}'. Add a platform adapter first.")


def build_generation_prompt(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
    max_chars: int,
) -> str:
    skill_text = "\n\n".join(
        f"## {skill.path}\n{skill.content}"
        for skill in skills
    )
    style = "\n".join(f"- {section.name}: {section.content}" for section in bundle.style_sections)
    context = "\n".join(f"- {section.kind}/{section.name}: {section.content}" for section in bundle.positioning_sections)
    patterns = "\n".join(f"- {pattern.category}/{pattern.name}: {pattern.description}" for pattern in bundle.patterns)
    examples = []
    for post in bundle.examples:
        note = bundle.notes_by_post_id.get(post.id)
        note_text = ""
        if note:
            labels = ", ".join(
                label
                for label in [
                    note.pattern_name,
                    note.hook,
                    note.structure,
                    note.tone,
                    note.cta,
                    note.formatting,
                ]
                if label
            )
            note_text = (
                f" Worked because: {note.worked_reason or 'not specified'} "
                f"Labels: {labels or 'not specified'}."
            )
        examples.append(f"- {post.text}{note_text}")
    examples_text = "\n".join(examples) or "- No examples yet. Lean on style and Leadpoet context."
    analytics = "\n".join(f"- {hint}" for hint in bundle.analytics_hints)
    return f"""Write {request.platform.upper()} posts for Leadpoet.

Writing instructions:
{skill_text or "- No skill files found. Use retrieved context and keep the post natural."}

Request:
- Topic: {request.topic}
- Post type: {request.post_type}
- Angle: {request.angle or "choose the strongest specific angle"}
- Audience: {request.audience or "Leadpoet's current audience"}
- Tone: {request.tone or "plain, sharp, useful"}
- CTA: {request.call_to_action or "only include if it feels natural"}
- Extra context: {request.extra_context or "none"}
- Character limit: {max_chars}

Style:
{style or "- No style guide yet."}

Leadpoet context:
{context or "- No positioning context yet."}

Patterns:
{patterns or "- No active patterns yet."}

High-performing examples:
{examples_text}

Performance hints:
{analytics or "- No performance hints yet."}

Return only draft post text variants, no commentary."""


def build_context_snapshot(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    skills: list[SkillAsset],
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
            }
        )
    return {
        "request": request.__dict__,
        "skills": [
            {
                "path": skill.path,
                "sha256": skill.sha256,
            }
            for skill in skills
        ],
        "selected_examples": examples,
        "selected_patterns": [
            {
                "id": pattern.id,
                "name": pattern.name,
                "category": pattern.category,
                "description": pattern.description,
            }
            for pattern in bundle.patterns
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
    }


def fallback_generate(
    request: DraftRequest,
    bundle: ContextBundle,
    *,
    variants: int,
    max_chars: int,
) -> list[str]:
    angle_phrase = _compact(request.angle or request.topic)
    angle_sentence = _sentence(_claim_from_angle(angle_phrase))
    topic_phrase = _compact(request.topic)
    topic_sentence = _sentence_case(topic_phrase)
    cta = _compact(request.call_to_action or "")
    templates = _templates_for(request.post_type)
    drafts: list[str] = []
    for index in range(variants):
        template = templates[index % len(templates)]
        text = template.format(
            topic=topic_phrase,
            topic_sentence=topic_sentence,
            angle=angle_phrase,
            angle_sentence=angle_sentence,
            cta=cta,
        ).strip()
        if cta and "{cta}" not in template:
            text = f"{text}\n\n{cta}"
        text = _nudge_with_context(text, request, bundle, index=index)
        drafts.append(clean_generated_text(text, max_chars=max_chars))
    return drafts


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


def explain_influence(bundle: ContextBundle) -> str:
    parts: list[str] = []
    if bundle.examples:
        parts.append(f"{len(bundle.examples)} high-performing X example(s)")
    if bundle.patterns:
        parts.append("active hook/structure/tone patterns")
    if bundle.positioning_sections:
        kinds = sorted({section.kind for section in bundle.positioning_sections})
        parts.append(f"Leadpoet context: {', '.join(kinds)}")
    if bundle.analytics_hints:
        parts.append("recent performance hints")
    return "Influenced by " + "; ".join(parts) + "." if parts else "Influenced by the base Gavin/Leadpoet style rules."


def _templates_for(post_type: str) -> list[str]:
    if post_type == "bittensor_update":
        post_type = "bittensor"
    templates = {
        "product_update": [
            "We shipped a small but important Leadpoet improvement around {angle}.\n\nThe point is not more automation. It is better context at the moment a buyer actually cares.",
            "Product note:\n\n{topic_sentence} gets more useful when the system knows the account, the timing, and the reason to reach out.\n\nThat is the bar we are building toward.",
        ],
        "founder_insight": [
            "Most teams do not have a sales AI problem.\n\nThey have a context problem.\n\n{angle_sentence}\n\nGeneric language is easy to ignore. Specific timing is not.",
            "The more I work on {topic}, the more obvious this feels:\n\nThe winning sales systems will sound less automated, not more.\n\nThey will know what changed and why it matters.",
        ],
        "bittensor": [
            "The interesting part of Bittensor is not that a subnet exists.\n\nIt is whether the subnet can turn a narrow skill into a market people actually want.\n\nThat is the lens I keep using for {topic}.",
            "A useful subnet should make one thing measurably better.\n\nFor {topic}, the question is simple: what signal improves, who pays attention, and why now?",
        ],
        "investor_facing": [
            "The market is moving from sales automation to sales intelligence.\n\nThat sounds subtle. It is not.\n\nAutomation sends more. Intelligence knows when saying less is worth more.",
            "My bet with Leadpoet is simple:\n\nThe next sales platform is not a bigger sequence tool. It is a context engine for revenue teams.",
        ],
        "quote_tweet": [
            "This is the part people miss.\n\n{angle_sentence}\n\nThe tooling only matters if it changes the quality of the conversation.",
            "Yes.\n\nAnd I would add: the hard part is not generating the sentence. It is knowing which sentence is worth sending.",
        ],
        "launch": [
            "Launching a new Leadpoet piece today around {topic}.\n\nBuilt for people who think sales AI should be more than prettier spam.\n\n{cta}",
            "New from Leadpoet:\n\n{angle_sentence}\n\nIf you care about sales AI that uses real context, this is the direction we are pushing.",
        ],
        "technical_explanation": [
            "{topic_sentence} in plain English:\n\nA model is only as useful as the context it can act on.\n\nFor sales, that means account facts, timing, pain, proof, and a reason to talk now.",
            "The technical problem in {topic} is not just generation.\n\nIt is retrieval, ranking, timing, and taste.\n\nThe sentence at the end is the easy part.",
        ],
        "light_funny": [
            "Startup math:\n\nIf the AI writes 10x more emails but they all sound like everyone else's emails, you have invented a faster trash can.",
            "Some sales AI feels like giving a megaphone to a spreadsheet.\n\nI would rather make the spreadsheet smarter first.",
        ],
    }
    return templates.get(post_type, templates["founder_insight"])


def _nudge_with_context(text: str, request: DraftRequest, bundle: ContextBundle, *, index: int) -> str:
    if request.post_type == "bittensor":
        return text
    if index % 2 == 0 and bundle.patterns:
        pattern_names = {pattern.name for pattern in bundle.patterns}
        if "enemy_then_better_model" in pattern_names and "generic" not in text.lower():
            return f"{text}\n\nThe enemy is generic output pretending to be intelligence."
    return text


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sentence_case(value: str) -> str:
    compact = _compact(value)
    if not compact:
        return compact
    return compact[0].upper() + compact[1:]


def _sentence(value: str) -> str:
    compact = _sentence_case(value).rstrip(".!?")
    return f"{compact}."


def _claim_from_angle(value: str) -> str:
    compact = _compact(value)
    lowered = compact.lower()
    if lowered.startswith("why "):
        return compact[4:]
    return compact
