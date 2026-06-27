from __future__ import annotations

import json

from leadpoet_social_brain.core.models import ContextSection
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


VOICE_AVOID_PHRASES_SETTING = "voice.avoid_phrases"
VOICE_POSITIONING_RULES_SETTING = "voice.positioning_rules"
VOICE_RULES_SETTING = "voice.rules"
VOICE_REVIEW_RULES_SETTING = "voice.review_rules"
LEADPOET_INTENT_CONTEXT_NAME = "leadpoet_intent_layer_v0"
DEFAULT_AVOID_PHRASES = [
    "game changer",
    "unlock",
    "supercharge",
    "AI is transforming",
    "not just X, but Y",
    "generic launch language",
    "overly polished thought leadership",
    "vague market commentary",
]

POSITIONING_RULES = [
    (
        "Leadpoet is the core intent layer: proprietary buyer context, timing signals, "
        "and account-level intent."
    ),
    (
        "Other tools, agents, and case studies are market proof that intent matters, "
        "not the source of Leadpoet's unique wedge."
    ),
    (
        "When referencing another product, validate the market shift, then pull the "
        "point back to Leadpoet owning buyer timing and proprietary context."
    ),
]

DEFAULT_VOICE_RULES = [
    "Plain, direct, and specific.",
    "Founder-led without sounding like a brand account.",
    "Practical enough that a buyer or investor can see the business point.",
]


def load_avoid_phrases(store: SQLiteStore) -> list[str]:
    return load_rules_setting(store, VOICE_AVOID_PHRASES_SETTING, DEFAULT_AVOID_PHRASES)


def save_avoid_phrases(store: SQLiteStore, phrases: list[object]) -> list[str]:
    normalized = normalize_avoid_phrases(phrases)
    store.save_app_setting(VOICE_AVOID_PHRASES_SETTING, json.dumps(normalized))
    return normalized


def load_positioning_rules(store: SQLiteStore) -> list[str]:
    return load_rules_setting(store, VOICE_POSITIONING_RULES_SETTING, POSITIONING_RULES)


def save_positioning_rules(store: SQLiteStore, rules: list[object]) -> list[str]:
    normalized = normalize_rules(rules)
    store.save_app_setting(VOICE_POSITIONING_RULES_SETTING, json.dumps(normalized))
    store.save_context_section(
        ContextSection(
            kind="positioning",
            name=LEADPOET_INTENT_CONTEXT_NAME,
            content=positioning_rules_context_content(normalized),
            weight=1.8,
        )
    )
    return normalized


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
    return load_rules_setting(store, VOICE_REVIEW_RULES_SETTING, default_rules)


def save_review_rules(store: SQLiteStore, rules: list[object]) -> list[str]:
    normalized = normalize_rules(rules)
    store.save_app_setting(VOICE_REVIEW_RULES_SETTING, json.dumps(normalized))
    return normalized


def load_rules_setting(store: SQLiteStore, key: str, default_rules: list[str]) -> list[str]:
    raw = store.get_app_setting(key)
    if raw is None:
        return list(default_rules)
    return parse_rules_value(raw, default_rules)


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


def positioning_rules_context_content(rules: list[str]) -> str:
    if not rules:
        return "Leadpoet positioning rules are currently empty."
    return "Leadpoet positioning rules:\n" + "\n".join(f"- {rule}" for rule in rules)


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
        sections.append("Positioning Rules:\n" + "\n".join(f"- {rule}" for rule in positioning_rules))
    if voice_rules:
        sections.append("Voice Rules:\n" + "\n".join(f"- {rule}" for rule in voice_rules))
    if not sections:
        return "Active Writing Rules:\n- None configured."
    return "Active Writing Rules:\n" + "\n\n".join(sections)


def avoid_phrases_prompt_section(phrases: list[str]) -> str:
    if not phrases:
        return "Avoid Phrases:\n- None configured."
    lines = "\n".join(f"- {phrase}" for phrase in phrases)
    return (
        "Avoid Phrases:\n"
        f"{lines}\n\n"
        "Instruction:\n"
        "Do not use these phrases or close variants. If the idea is useful, "
        "rewrite it in plain Gavin-style language."
    )
