from __future__ import annotations

import re
from collections import defaultdict

from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


ACCEPTED_REVISION_PREFERENCES: tuple[tuple[str, str, str], ...] = (
    (
        "concise",
        r"\b(?:shorter|shorten|concise|tighter|tighten|trim|cut\s+(?:this|it|down)|fewer\s+words)\b",
        "Prefer tighter copy; the user repeatedly accepted revisions that removed unnecessary words.",
    ),
    (
        "specific",
        r"\b(?:more\s+specific|specificity|more\s+concrete|concrete\s+(?:detail|example)|"
        r"name\s+the\s+(?:evidence|mechanism)|show\s+the\s+(?:evidence|mechanism))\b",
        "Prefer a named mechanism or observable evidence; the user repeatedly accepted more specific revisions.",
    ),
    (
        "plain",
        r"\b(?:more\s+natural|sound\s+(?:more\s+)?human|plain(?:er)?\s+(?:language|english)|"
        r"simpl(?:er|ify)|less\s+polished)\b",
        "Prefer plain, natural language; the user repeatedly accepted revisions that sounded less polished.",
    ),
    (
        "no_cta",
        r"\b(?:(?:remove|drop|omit|no)\s+(?:the\s+)?(?:cta|call\s+to\s+action|link)|"
        r"without\s+(?:a\s+)?(?:cta|call\s+to\s+action))\b",
        "Do not force a CTA; the user repeatedly accepted revisions that removed one.",
    ),
    (
        "less_product",
        r"\b(?:(?:remove|drop|omit)\s+(?:the\s+)?(?:product|brand|leadpoet)(?:\s+mention)?|"
        r"less\s+(?:product|brand)[ -]?(?:led|heavy)|without\s+(?:mentioning\s+)?leadpoet)\b",
        "Keep product mentions optional; the user repeatedly accepted less product-led revisions.",
    ),
    (
        "opening",
        r"\b(?:(?:stronger|sharper|better)\s+(?:hook|opening|first\s+line)|"
        r"tighten\s+(?:the\s+)?(?:hook|opening|first\s+line))\b",
        "Open with the useful point faster; the user repeatedly accepted sharper openings.",
    ),
    (
        "less_hype",
        r"\b(?:less\s+(?:hype|hypey|corporate|promotional|salesy)|"
        r"remove\s+(?:the\s+)?(?:hype|marketing\s+language)|tone\s+(?:it\s+)?down)\b",
        "Avoid hype and corporate promotion; the user repeatedly accepted toned-down revisions.",
    ),
)


def accepted_revision_preference_hints(
    store: SQLiteStore,
    *,
    minimum_independent_acceptances: int = 3,
    queue_limit: int = 500,
    version_limit_per_queue: int = 50,
) -> list[str]:
    """Return fixed preference hints backed by repeated accepted edit decisions.

    Raw revision instructions are never placed in prompts.  Only a small, fixed
    taxonomy can emit a hint, and a hint needs support from distinct queue items so
    one editing session cannot silently become a workspace-wide Voice rule.
    """

    threshold = max(2, int(minimum_independent_acceptances))
    supporting_queue_ids: dict[str, set[str]] = defaultdict(set)
    for item in store.list_content_queue(platform="x", limit=max(0, int(queue_limit))):
        for version in store.list_queued_post_versions(
            item.id,
            limit=max(0, int(version_limit_per_queue)),
        ):
            if version.edit_type != "llm" or not version.accepted_at or not version.edit_instruction:
                continue
            if not version.source_draft_id or item.draft_id != version.source_draft_id:
                continue
            if " ".join(item.text.split()) != " ".join(version.text.split()):
                continue
            draft = store.get_generated_draft(version.source_draft_id)
            before = str(draft.request.revision_source_text or "") if draft else ""
            if not before or " ".join(before.split()) == " ".join(version.text.split()):
                continue
            instruction = " ".join(version.edit_instruction.casefold().replace("’", "'").split())
            for name, pattern, _hint in ACCEPTED_REVISION_PREFERENCES:
                if (
                    instruction_has_unnegated_preference(instruction, pattern)
                    and observed_revision_supports_preference(name, before, version.text)
                ):
                    supporting_queue_ids[name].add(item.id)

    hints: list[str] = []
    for name, _pattern, hint in ACCEPTED_REVISION_PREFERENCES:
        count = len(supporting_queue_ids.get(name, set()))
        if count >= threshold:
            hints.append(f"Accepted edit signal ({count} independent posts): {hint}")
    return hints


def instruction_has_unnegated_preference(instruction: str, pattern: str) -> bool:
    for match in re.finditer(pattern, instruction, flags=re.I):
        prefix = instruction[max(0, match.start() - 48):match.start()]
        if re.search(
            r"\b(?:do\s+not|don't|never)\b(?:\s+[a-z]+){0,4}\s*$",
            prefix,
            flags=re.I,
        ):
            continue
        return True
    return False


def observed_revision_supports_preference(name: str, before: str, after: str) -> bool:
    clean_before = " ".join(str(before or "").split())
    clean_after = " ".join(str(after or "").split())
    if not clean_before or clean_before == clean_after:
        return False
    if name == "concise":
        return len(clean_after) < len(clean_before)
    if name == "no_cta":
        return cta_signature_count(clean_after) < cta_signature_count(clean_before)
    if name == "less_product":
        return product_reference_count(clean_after) < product_reference_count(clean_before)
    return True


def cta_signature_count(text: str) -> int:
    patterns = [
        r"https?://\S+",
        r"\b(?:dm|reply|comment|follow|subscribe|sign\s+up|book\s+(?:a\s+)?call|try\s+it|link\s+below)\b",
    ]
    return sum(len(re.findall(pattern, text, flags=re.I)) for pattern in patterns)


def product_reference_count(text: str) -> int:
    return len(
        re.findall(
            r"\b(?:leadpoet|signaldesk|the\s+product|our\s+product|our\s+platform|the\s+brand)\b",
            text,
            flags=re.I,
        )
    )


__all__ = ["ACCEPTED_REVISION_PREFERENCES", "accepted_revision_preference_hints"]
