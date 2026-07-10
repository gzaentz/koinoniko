from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


EDITORIAL_VALUE_MODES = [
    (
        "practical playbook",
        "Give the reader a short sequence, check, or action they can use without buying anything.",
    ),
    (
        "mechanism explainer",
        "Explain how or why one concrete mechanism works, including the causal step most people miss.",
    ),
    (
        "diagnostic",
        "Name a recognizable symptom, the likely cause, and one useful way to test it.",
    ),
    (
        "decision framework",
        "Give the reader a criterion or tradeoff they can use to make a better choice.",
    ),
    (
        "evidence unpacking",
        "Use one grounded fact or source detail, explain what it proves, and state what it does not prove.",
    ),
    (
        "build note",
        "Share a concrete product or operating choice, the constraint behind it, and the lesson that transfers.",
    ),
    (
        "buyer question",
        "Give the reader a sharp question to ask and explain what a strong answer should contain.",
    ),
    (
        "useful distinction",
        "Separate two ideas people blur together and show when the distinction changes action.",
    ),
]


_MESSAGE_TERMS = (
    r"\bcopy\b",
    r"\bemails?\b",
    r"\bmessages?\b",
    r"\bsentences?\b",
    r"\bsequences?\b",
    r"\bwriting\b",
    r"\bwrite\b",
    r"\bword(?:s|ing)?\b",
    r"\bsend(?:ing|s)?\b",
)

_JUDGMENT_TERMS = (
    r"\baccounts?\b",
    r"\bbuyers?\b",
    r"\bcontacts?\b",
    r"\bcontext\b",
    r"\bevidence\b",
    r"\bintent\b",
    r"\bjudg(?:e|es|ed|ing|ment)\b",
    r"\bprioriti[sz](?:e|es|ed|ing|ation)\b",
    r"\breasons?\b",
    r"\brelevance\b",
    r"\bsignals?\b",
    r"\btargets?\b",
    r"\btargeting\b",
    r"\bwho(?:'s| is)? worth\b",
)

_DOWNSTREAM_CONTRAST_TERMS = (
    r"\bbefore\b",
    r"\bdownstream\b",
    r"\beasy part\b",
    r"\bhard part\b",
    r"\boptimizing the wrong\b",
    r"\bwrong (?:step|test|thing)\b",
    r"\bupstream\b",
)


def editorial_theme_fingerprints(text: str) -> list[str]:
    """Return broad thesis fingerprints used to prevent semantic repetition.

    These are deliberately coarser than wording similarity. A post can change its
    hook and sentence shape while still making the same underlying argument.
    """

    normalized = _normalized_text(text)
    if not normalized:
        return []

    themes: list[str] = []
    has_message = _matches_any(normalized, _MESSAGE_TERMS)
    has_judgment = _matches_any(normalized, _JUDGMENT_TERMS)
    has_downstream_contrast = _matches_any(normalized, _DOWNSTREAM_CONTRAST_TERMS)

    if has_message and has_judgment and has_downstream_contrast:
        themes.append("copy-is-downstream-of-judgment")
    if (
        _matches_any(
            normalized,
            (
                r"\bwhy now\b",
                r"\btiming\b",
                r"\bin[- ]market\b",
                r"\bready to (?:buy|listen|talk)\b",
                r"\breason to (?:listen|care|act|reply)\b",
            ),
        )
        and _matches_any(normalized, (r"\baccounts?\b", r"\bbuyers?\b", r"\bcontacts?\b", r"\bwho\b"))
    ):
        themes.append("buyer-timing-and-why-now")
    if (
        _matches_any(
            normalized,
            (
                r"\bvolume\b",
                r"\bmore (?:emails?|messages?|outbound|sequences?|sends?)\b",
                r"\bfaster sequences?\b",
                r"\blouder\b",
                r"\bat scale\b",
            ),
        )
        and _matches_any(
            normalized,
            (
                r"\bcontext\b",
                r"\bjudg(?:e|es|ed|ing|ment)\b",
                r"\bquality\b",
                r"\breasons?\b",
                r"\brelevance\b",
                r"\btiming\b",
                r"\bin[- ]market\b",
            ),
        )
    ):
        themes.append("volume-versus-relevance")
    if (
        _matches_any(normalized, (r"\bcrm\b", r"\bdata\b", r"\brecords?\b", r"\bcontacts?\b"))
        and _matches_any(
            normalized,
            (
                r"\baccurate\b",
                r"\bclean\b",
                r"\bcomplete\b",
                r"\bcorrect\b",
                r"\benrich(?:ed|ment)?\b",
                r"\bquality\b",
                r"\bstale\b",
                r"\bverif(?:y|ied|ication)\b",
            ),
        )
    ):
        themes.append("record-and-data-quality")
    if (
        _matches_any(normalized, (r"\bai\b", r"\bagents?\b", r"\bautomat(?:e|ed|es|ing|ion)\b"))
        and _matches_any(
            normalized,
            (
                r"\bcontext\b",
                r"\bevidence\b",
                r"\bjudg(?:e|es|ed|ing|ment)\b",
                r"\breasons?\b",
                r"\brelevance\b",
            ),
        )
    ):
        themes.append("automation-needs-context")
    if (
        _matches_any(normalized, (r"\bsignals?\b", r"\bintent\b", r"\bwhat changed\b", r"\baccount change\b"))
        and _matches_any(
            normalized,
            (
                r"\baction\b",
                r"\bcontact\b",
                r"\boutreach\b",
                r"\bprioriti[sz]",
                r"\bsequence\b",
                r"\bsend\b",
            ),
        )
    ):
        themes.append("signal-to-action")
    return themes


def editorial_theme_counts(texts: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for text in texts:
        for theme in editorial_theme_fingerprints(text):
            counts[theme] = counts.get(theme, 0) + 1
    return counts


def explicitly_requested_editorial_themes(text: str) -> list[str]:
    """Recognize a thesis requested in a brief, which is often shorter than a post."""

    normalized = _normalized_text(text)
    themes = editorial_theme_fingerprints(normalized)
    if _matches_any(normalized, _MESSAGE_TERMS) and _matches_any(normalized, _JUDGMENT_TERMS):
        themes.append("copy-is-downstream-of-judgment")
    return list(dict.fromkeys(themes))


def quantified_claims(text: str) -> list[str]:
    """Extract canonical numeric/threshold claims that require explicit evidence."""

    value = " ".join(str(text or "").replace(",", "").lower().split())
    generic_number_pattern = r"\b\d{2,}(?:\.\d+)?\b"
    patterns = [
        r"\b\d+(?:\.\d+)?\s*%\+?",
        r"\$\s*\d+(?:\.\d+)?\s*(?:k|m|b|thousand|million|billion)?\b",
        r"\b\d+(?:\.\d+)?\s*(?:thousand|million|billion)\b",
        r"\b\d+(?:\.\d+)?[-\s]*(?:minutes?|hours?|days?|weeks?|months?|years?)\b",
        r"\b(?:minutes?|hours?|days?|weeks?|months?|years?)\s*\d+(?:\.\d+)?\b",
        r"\b\d+(?:\.\d+)?\s*(?:x|times)\b",
        r"\b(?:under|over|more than|less than|at least|at most)\s+\d+(?:\.\d+)?\b",
        (
            r"\b\d+(?:\.\d+)?\+?\s*(?:users?|sellers?|buyers?|accounts?|customers?|companies?|"
            r"meetings?|replies?|leads?|records?|domains?|signals?)\b"
        ),
        generic_number_pattern,
        r"\b(?:doubled|tripled|quadrupled|halved|twice|three\s+times)\b",
        r"\b(?:cut|cuts|reduced?|reduces?|fell|falls?|dropped?|drops?)\b[^.!?]{0,45}\b(?:in|by)\s+half\b",
        (
            r"\b(?:dozens|hundreds|thousands|millions|billions)\s+of\s+"
            r"(?:(?:active|annual|current|enterprise|new|paying|potential|qualified|verified)\s+){0,3}"
            r"(?:users?|sellers?|buyers?|accounts?|customers?|companies?|meetings?|replies?|"
            r"leads?|records?|domains?|signals?|calls?|emails?|people|teams?)\b"
        ),
        (
            r"\b(?:most|the\s+majority\s+of|a\s+majority\s+of|more\s+than\s+half\s+of|"
            r"roughly\s+half\s+of|the\s+vast\s+majority\s+of|nearly\s+all|almost\s+all)\s+"
            r"(?:(?:active|annual|current|enterprise|new|paying|potential|qualified|verified)\s+){0,3}"
            r"(?:users?|sellers?|buyers?|accounts?|customers?|companies?|"
            r"meetings?|replies?|leads?|records?|domains?|signals?|calls?|emails?|people|teams?)\b"
        ),
    ]
    claims: list[str] = []
    covered_spans: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.I):
            if pattern == generic_number_pattern and any(
                match.start() < end and match.end() > start
                for start, end in covered_spans
            ):
                continue
            canonical = _canonical_quantified_claim(match.group(0))
            if canonical and canonical not in claims:
                claims.append(canonical)
            if pattern != generic_number_pattern:
                covered_spans.append(match.span())
    return claims


def unsupported_quantified_claims(text: str, evidence_text: str) -> list[str]:
    evidence_claims = set(quantified_claims(evidence_text))
    return [claim for claim in quantified_claims(text) if claim not in evidence_claims]


def dedupe_editorial_trends(trends: Iterable[Any]) -> list[Any]:
    """Keep one representative per underlying news event, preserving input order."""

    selected: list[Any] = []
    for trend in trends:
        if any(same_editorial_event(trend, existing) for existing in selected):
            continue
        selected.append(trend)
    return selected


def same_editorial_event(left: Any, right: Any) -> bool:
    left_title = str(getattr(left, "title", "") or "")
    right_title = str(getattr(right, "title", "") or "")
    if not left_title or not right_title:
        return False
    left_event = _event_type(left_title)
    right_event = _event_type(right_title)
    left_tokens = _event_tokens(left_title)
    right_tokens = _event_tokens(right_title)
    shared = left_tokens & right_tokens
    if left_event and left_event == right_event == "acquisition":
        return len(shared) >= 2
    if left_event and left_event == right_event == "funding":
        left_money = {claim for claim in quantified_claims(left_title) if claim.startswith("money:")}
        right_money = {claim for claim in quantified_claims(right_title) if claim.startswith("money:")}
        return bool(shared) and bool(left_money & right_money)
    union = left_tokens | right_tokens
    return bool(union) and len(shared) >= 3 and (len(shared) / len(union)) >= 0.55


def _canonical_quantified_claim(value: str) -> str:
    cleaned = " ".join(value.lower().replace(",", "").split())
    verbal_multiple = {
        "doubled": "multiple:2",
        "twice": "multiple:2",
        "tripled": "multiple:3",
        "three times": "multiple:3",
        "quadrupled": "multiple:4",
        "halved": "multiple:0.5",
    }
    if cleaned in verbal_multiple:
        return verbal_multiple[cleaned]
    if re.search(
        r"\b(?:cut|cuts|reduced?|reduces?|fell|falls?|dropped?|drops?)\b[^.!?]{0,45}\b(?:in|by) half\b",
        cleaned,
    ):
        return "multiple:0.5"
    approximate = re.match(
        r"(dozens|hundreds|thousands|millions|billions)\s+of\s+(.+)",
        cleaned,
    )
    if approximate:
        noun = approximate.group(2).rstrip("s")
        return f"approximate_count:{approximate.group(1)}:{noun}"
    proportion = re.match(
        r"(most|the majority of|a majority of|more than half of|roughly half of|"
        r"the vast majority of|nearly all|almost all)\s+(.+)",
        cleaned,
    )
    if proportion:
        label = "half" if proportion.group(1) == "roughly half of" else "majority"
        return f"proportion:{label}:{proportion.group(2).rstrip('s')}"
    number_match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not number_match:
        return ""
    number = number_match.group(0)
    if "%" in cleaned:
        return f"percent:{number}"
    if "$" in cleaned or re.search(r"\b(?:thousand|million|billion)\b", cleaned):
        suffix = ""
        if re.search(rf"\b{re.escape(number)}\s*(?:b|billion)\b", cleaned):
            suffix = "b"
        elif re.search(rf"\b{re.escape(number)}\s*(?:m|million)\b", cleaned):
            suffix = "m"
        elif re.search(rf"\b{re.escape(number)}\s*(?:k|thousand)\b", cleaned):
            suffix = "k"
        return f"money:{number}{suffix}"
    duration = re.search(r"\b(minutes?|hours?|days?|weeks?|months?|years?)\b", cleaned)
    if duration:
        return f"duration:{number}:{duration.group(1).rstrip('s')}"
    if re.search(r"\b(?:x|times)\b", cleaned):
        return f"multiple:{number}"
    noun = re.search(
        r"\b(users?|sellers?|buyers?|accounts?|customers?|companies?|meetings?|replies?|leads?|records?|domains?|signals?)\b",
        cleaned,
    )
    if noun:
        return f"count:{number}:{noun.group(1).rstrip('s')}"
    qualifier = re.match(r"(under|over|more than|less than|at least|at most)", cleaned)
    return f"threshold:{qualifier.group(1) if qualifier else ''}:{number}"


_EVENT_STOPWORDS = {
    "a", "ai", "an", "and", "as", "at", "b2b", "co", "company", "exclusive", "for", "in", "into",
    "its", "of", "on", "startup", "the", "to", "with", "sales", "crm", "agent", "agents", "system",
    "acquire", "acquires", "acquisition", "buys", "bought", "deal", "deals", "raises", "raised", "funding",
    "financing", "closes", "close", "series", "million", "leadership", "position", "platform",
}


def _event_type(title: str) -> str:
    lowered = title.lower()
    if re.search(r"\b(?:acquir(?:e|es|ed|ing|isition)|buys?|bought|deal)\b", lowered):
        return "acquisition"
    if re.search(r"\b(?:raises?|raised|funding|financing|series\s+[a-z]|closes?\s+\$)\b", lowered):
        return "funding"
    return ""


def _event_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", title.lower())
        if len(token) >= 3 and token not in _EVENT_STOPWORDS and not token.isdigit()
    }


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def _normalized_text(text: str) -> str:
    return " ".join(str(text or "").replace("’", "'").lower().split())
