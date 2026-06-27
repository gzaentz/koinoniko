from __future__ import annotations

import json

from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


VOICE_AVOID_PHRASES_SETTING = "voice.avoid_phrases"
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


def load_avoid_phrases(store: SQLiteStore) -> list[str]:
    raw = store.get_app_setting(VOICE_AVOID_PHRASES_SETTING)
    if raw is None:
        return list(DEFAULT_AVOID_PHRASES)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw.splitlines()
    if not isinstance(parsed, list):
        return list(DEFAULT_AVOID_PHRASES)
    return normalize_avoid_phrases(parsed)


def save_avoid_phrases(store: SQLiteStore, phrases: list[object]) -> list[str]:
    normalized = normalize_avoid_phrases(phrases)
    store.save_app_setting(VOICE_AVOID_PHRASES_SETTING, json.dumps(normalized))
    return normalized


def normalize_avoid_phrases(phrases: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        cleaned = " ".join(str(phrase or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized[:40]


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
