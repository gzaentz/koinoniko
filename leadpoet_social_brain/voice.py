from __future__ import annotations

import json
import re

from leadpoet_social_brain.core.models import DEFAULT_WORKSPACE_ID, ContextSection
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


VOICE_AVOID_PHRASES_SETTING = "voice.avoid_phrases"
VOICE_POSITIONING_RULES_SETTING = "voice.positioning_rules"
VOICE_RULES_SETTING = "voice.rules"
VOICE_REVIEW_RULES_SETTING = "voice.review_rules"
POSITIONING_RULES_CONTEXT_NAME = "active_positioning_rules"
POSITIONING_SOURCE_PROMPT_GUIDANCE = (
    "Use saved positioning as a factual boundary and optional idea source, not a checklist to inject into every post; "
    "only use the wedge when it materially helps the current reader payoff. When it is relevant, "
    "translate any product-definition or product-subject phrasing into a concrete mechanism, "
    "account change, or consequence before writing."
)
DEFAULT_AVOID_PHRASES = [
    "game changer",
    "unlock",
    "supercharge",
    "AI is transforming",
    "the future of",
    "here's the thing",
    "as AI continues",
    "at scale",
    "new era",
    "paradigm shift",
    "seamless",
    "leverage",
    "not just X, but Y",
    "generic launch language",
    "overly polished thought leadership",
    "vague market commentary",
]

DEFAULT_WORKSPACE_POSITIONING_RULES = [
    "Keep the Leadpoet wedge concrete: proprietary buyer context, timing signals, and account-level intent.",
    "Use other tools, agents, and case studies for the exact mechanism, tradeoff, or evidence boundary they reveal; do not turn each one into generic proof that intent matters.",
    "When another product or market example materially illuminates the Leadpoet wedge, connect the exact mechanism; otherwise let it teach a useful standalone lesson without forcing buyer timing or account context into the post.",
]

LEGACY_WORKSPACE_POSITIONING_REPLACEMENTS = {
    "Leadpoet owns the intent wedge: proprietary buyer context, timing signals, and account-level intent.": DEFAULT_WORKSPACE_POSITIONING_RULES[0],
    "Other tools, agents, and case studies are market proof that intent matters, not the source of Leadpoet's unique wedge.": DEFAULT_WORKSPACE_POSITIONING_RULES[1],
    "Other tools, agents, and case studies are market proof that intent matters, not the mechanism to copy.": DEFAULT_WORKSPACE_POSITIONING_RULES[1],
    "When referencing another product, validate the market shift, then pull the point back to Leadpoet owning buyer timing and proprietary context.": DEFAULT_WORKSPACE_POSITIONING_RULES[2],
    "When referencing another product, validate the market shift, then bring the point back to buyer timing and proprietary context inside Leadpoet.": DEFAULT_WORKSPACE_POSITIONING_RULES[2],
    "When referencing another product, validate the market shift, then bring the point back to buyer timing, proprietary context, and the account reason before copy or outreach.": DEFAULT_WORKSPACE_POSITIONING_RULES[2],
    "When referencing another product, trend, or case study, connect it back only to the saved positioning.": (
        "If a post connects another product, trend, or case study to the workspace, use only saved positioning; "
        "omit the connection when it does not improve the reader payoff."
    ),
}

UNIVERSAL_POSITIONING_RULES = [
    "Use saved Facts & Context as the source of truth for writer, company, product, audience, and positioning.",
    "Do not invent company claims, founder background, customer proof, metrics, or product capabilities.",
    (
        "If a post connects another product, trend, or case study to the workspace, use only saved positioning; "
        "omit the connection when it does not improve the reader payoff."
    ),
]

POSITIONING_RULES = [
    *DEFAULT_WORKSPACE_POSITIONING_RULES,
    *UNIVERSAL_POSITIONING_RULES,
]

DEFAULT_VOICE_RULES = [
    "Plain, direct, and specific.",
    "Founder-led without sounding like a brand account.",
    "Practical enough that a buyer or investor can see the business point.",
    "Use exact nouns from saved context instead of polished category language.",
    "Sound like a useful thought the writer would actually post, not an AI recap.",
    "Vary how the product is mentioned; avoid default product-definition sentences.",
    "Vary the editorial job across posts: teach, explain, diagnose, compare, or unpack evidence instead of repeating one positioning thesis.",
]

REQUIRED_REVIEW_RULES = [
    (
        "If a trend, company, product, or case study is used, explain the exact mechanism "
        "that transfers to saved Facts & Context; do not rely on 'same problem' or 'part to watch' shorthand."
    ),
    "Reject drafts that could apply to any workspace after swapping company or product names.",
    "Reject cookie-cutter product closers that define the product the same way instead of earning the mention through the post.",
    "Reject drafts that restate a saturated recent thesis with new synonyms when the user's brief did not explicitly require that thesis.",
    "Require one concrete reader payoff: a mechanism, diagnostic, decision rule, step, tradeoff, example, or evidence boundary.",
    (
        "A post-worthy draft must answer why this is worth posting now: concrete reader payoff, "
        "timely trigger, or saved-context proof."
    ),
    "Do not use a news item as a topical peg unless the insight depends on a specific fact from that item.",
]


def load_avoid_phrases(store: SQLiteStore) -> list[str]:
    return load_rules_setting(store, VOICE_AVOID_PHRASES_SETTING, DEFAULT_AVOID_PHRASES)


def save_avoid_phrases(store: SQLiteStore, phrases: list[object]) -> list[str]:
    normalized = normalize_avoid_phrases(phrases)
    store.save_app_setting(VOICE_AVOID_PHRASES_SETTING, json.dumps(normalized))
    return normalized


def load_positioning_rules(store: SQLiteStore) -> list[str]:
    raw = store.get_app_setting(VOICE_POSITIONING_RULES_SETTING)
    if raw is None:
        rules = default_positioning_rules(store)
    else:
        rules = with_universal_positioning_rules(rewrite_legacy_positioning_rules(parse_rules_value(raw, [])))
    sync_positioning_rules_context(store, rules)
    return rules


def save_positioning_rules(store: SQLiteStore, rules: list[object]) -> list[str]:
    normalized = with_universal_positioning_rules(rewrite_legacy_positioning_rules(normalize_rules(rules)))
    store.save_app_setting(VOICE_POSITIONING_RULES_SETTING, json.dumps(normalized))
    sync_positioning_rules_context(store, normalized)
    return normalized


def sync_positioning_rules_context(store: SQLiteStore, rules: list[str]) -> None:
    content = positioning_rules_context_content(rules)
    existing = next(
        (
            section
            for section in store.list_context_sections(kinds={"positioning"}, active_only=False)
            if section.name == POSITIONING_RULES_CONTEXT_NAME
        ),
        None,
    )
    if existing and existing.content == content and existing.active and existing.weight == 1.8:
        return
    store.save_context_section(
        ContextSection(
            kind="positioning",
            name=POSITIONING_RULES_CONTEXT_NAME,
            content=content,
            weight=1.8,
        )
    )


def load_voice_rules_override(store: SQLiteStore) -> list[str] | None:
    raw = store.get_app_setting(VOICE_RULES_SETTING)
    if raw is None:
        return None
    return parse_rules_value(raw, [])


def save_voice_rules_override(store: SQLiteStore, rules: list[object]) -> list[str]:
    normalized = normalize_rules(rules)
    store.save_app_setting(VOICE_RULES_SETTING, json.dumps(normalized))
    return normalized


def load_review_rules(store: SQLiteStore, default_rules: list[str]) -> list[str]:
    return with_required_review_rules(load_rules_setting(store, VOICE_REVIEW_RULES_SETTING, default_rules))


def save_review_rules(store: SQLiteStore, rules: list[object]) -> list[str]:
    normalized = normalize_rules(rules)
    store.save_app_setting(VOICE_REVIEW_RULES_SETTING, json.dumps(normalized))
    return normalized


def load_rules_setting(store: SQLiteStore, key: str, default_rules: list[str]) -> list[str]:
    raw = store.get_app_setting(key)
    if raw is None:
        return list(default_rules)
    return parse_rules_value(raw, default_rules)


def default_positioning_rules(store: SQLiteStore) -> list[str]:
    if store.workspace_id == DEFAULT_WORKSPACE_ID:
        return list(POSITIONING_RULES)
    return list(UNIVERSAL_POSITIONING_RULES)


def parse_rules_value(raw: object, default_rules: list[str]) -> list[str]:
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        parsed = str(raw).splitlines()
    if not isinstance(parsed, list):
        return list(default_rules)
    return normalize_rules(parsed)


def normalize_avoid_phrases(phrases: list[object]) -> list[str]:
    return normalize_rules(phrases)


def normalize_rules(rules: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        cleaned = " ".join(str(rule or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized[:40]


def with_required_review_rules(rules: list[str]) -> list[str]:
    return normalize_rules([*rules, *REQUIRED_REVIEW_RULES])


def with_universal_positioning_rules(rules: list[str]) -> list[str]:
    return normalize_rules([*rules, *UNIVERSAL_POSITIONING_RULES])


def rewrite_legacy_positioning_rules(rules: list[str]) -> list[str]:
    return [LEGACY_WORKSPACE_POSITIONING_REPLACEMENTS.get(rule, rule) for rule in rules]


def positioning_rules_context_content(rules: list[str]) -> str:
    if not rules:
        return "Positioning rules are currently empty."
    lines = "\n".join(f"- {prompt_safe_positioning_rule(rule)}" for rule in rules)
    return f"Positioning rules:\n{POSITIONING_SOURCE_PROMPT_GUIDANCE}\n{lines}"


def review_filter_skill_content(rules: list[str]) -> str:
    lines = "\n".join(f"- {rule}" for rule in rules)
    return (
        "# Review Filter\n\n"
        "Before finalizing, check the draft against these rules.\n\n"
        f"{lines or '- No review filters configured.'}"
    )


def active_rules_prompt_section(
    *,
    positioning_rules: list[str],
    voice_rules: list[str] | None,
) -> str:
    sections: list[str] = []
    if positioning_rules:
        lines = "\n".join(f"- {prompt_safe_positioning_rule(rule)}" for rule in positioning_rules)
        sections.append(f"Positioning Rules:\n- {POSITIONING_SOURCE_PROMPT_GUIDANCE}\n{lines}")
    if voice_rules:
        sections.append("Voice Rules:\n" + "\n".join(f"- {rule}" for rule in voice_rules))
    if not sections:
        return "Active Writing Rules:\n- None configured."
    return "Active Writing Rules:\n" + "\n\n".join(sections)


def prompt_safe_positioning_rule(rule: str) -> str:
    cleaned = " ".join(str(rule or "").split())
    if not cleaned:
        return cleaned
    for pattern, rewrite in POSITIONING_RULE_PROMPT_REWRITES:
        match = pattern.match(cleaned)
        if match:
            return rewrite(match)
    return cleaned


def _clean_positioning_meaning(value: str) -> str:
    return value.strip().rstrip(".")


def _rewrite_product_owns_rule(match: re.Match[str]) -> str:
    product, meaning = match.groups()
    return (
        f"{product} positioning meaning: {_clean_positioning_meaning(meaning)}. "
        "Express this through a concrete mechanism, account change, or consequence."
    )


def _rewrite_product_is_rule(match: re.Match[str]) -> str:
    product, meaning = match.groups()
    return (
        f"{product} positioning meaning: {_clean_positioning_meaning(meaning)}. "
        "Do not turn it into a standalone company-definition sentence."
    )


def _rewrite_product_helper_rule(match: re.Match[str]) -> str:
    product, meaning = match.groups()
    return (
        f"{product} positioning meaning: {_clean_positioning_meaning(meaning)}. "
        "Let the mechanism or consequence lead instead of a product-subject helper sentence."
    )


POSITIONING_RULE_PROMPT_REWRITES = [
    (re.compile(r"^([A-Z][A-Za-z0-9_.-]*)\s+owns?\s+(.+)$"), _rewrite_product_owns_rule),
    (re.compile(r"^([A-Z][A-Za-z0-9_.-]*)\s+is\s+building\s+(.+)$"), _rewrite_product_is_rule),
    (re.compile(r"^([A-Z][A-Za-z0-9_.-]*)\s+is\s+built\s+for\s+(.+)$"), _rewrite_product_is_rule),
    (re.compile(r"^([A-Z][A-Za-z0-9_.-]*)\s+is\s+(.+)$"), _rewrite_product_is_rule),
    (
        re.compile(
            r"^([A-Z][A-Za-z0-9_.-]*)\s+"
            r"(?:helps?|enables?|unlocks?|provides?|makes?|starts?|keeps?|turns?|focuses|gives?|answers?|shows?|lets?|finds?|delivers?|powers?|brings?|drives?|creates?)\s+(.+)$",
        ),
        _rewrite_product_helper_rule,
    ),
]


def avoid_phrases_prompt_section(phrases: list[str]) -> str:
    if not phrases:
        return "Avoid Phrases:\n- None configured."
    lines = "\n".join(f"- {phrase}" for phrase in phrases)
    return (
        "Avoid Phrases:\n"
        f"{lines}\n\n"
        "Instruction:\n"
        "Do not use these phrases or close variants. If the idea is useful, "
        "rewrite it in plain saved-voice language. This holds even when the brief, "
        "angle, source, or existing draft uses the phrase itself: keep the meaning "
        "and paraphrase the wording. Echoing an avoided phrase back from the request "
        "is still a defect."
    )
