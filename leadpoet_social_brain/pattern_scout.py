from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from leadpoet_social_brain.core.models import ViralPattern, utc_now
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.trend_scout import (
    SCRAPINGDOG_NEWS_URL,
    SCRAPINGDOG_TIMEOUT_SECONDS,
    fetch_json,
    scrapingdog_api_key,
)


PATTERN_REFRESH_MISSING_SCRAPINGDOG = (
    "Pattern refresh requires ScrapingDog. You can still add patterns manually."
)
PATTERN_REFRESH_MISSING_PROVIDER = "Pattern refresh requires an LLM provider."
PATTERN_CATEGORIES = {"ai", "sales", "lead gen", "gtm", "bittensor", "founder", "product", "other"}
PATTERN_REFRESH_QUERY = (
    'site:x.com ("AI" OR "sales" OR "lead generation" OR GTM OR "AI SDR" OR Bittensor) '
    '("likes" OR "reposted" OR "bookmarks" OR "founder")'
)
DEFAULT_PATTERN_NAME = "Observed pattern"
DEFAULT_ADAPTATION_RULE = "Use the structure, not the wording."
MIN_PATTERN_SOURCE_TEXT_LENGTH = 40
PatternCandidateFetcher = Callable[..., list[dict[str, object]]]


@dataclass(frozen=True)
class PatternBatchResult:
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    categories: Counter[str] = field(default_factory=Counter)
    pattern_names: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class PatternRefreshResult:
    fetched: int = 0
    stored: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    patterns: list[ViralPattern] = field(default_factory=list)


def add_pattern_example(
    store: SQLiteStore,
    *,
    source_url: str | None = None,
    source_author: str | None = None,
    source_text: str | None = None,
    source_summary: str | None = None,
    topic: str | None = None,
    category: str | None = None,
    pattern_name: str | None = None,
    hook_style: str | None = None,
    structure: str | None = None,
    pacing: str | None = None,
    tone: str | None = None,
    formatting: str | None = None,
    why_it_worked: str | None = None,
    adaptation_rule: str | None = None,
    engagement_notes: str | None = None,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    quotes: int | None = None,
    bookmarks: int | None = None,
    impressions: int | None = None,
    collected_at: str | None = None,
    window: str = "manual",
    status: str = "active",
    raw_text: str | None = None,
    platform: str = "x",
    pattern_id: str | None = None,
) -> ViralPattern:
    if not any([source_url, source_text, source_summary, pattern_name, structure]):
        raise ValueError("Pattern requires at least source text, source URL, source summary, pattern name, or structure.")
    saved = ViralPattern(
        id=pattern_id or stable_pattern_id(source_url or f"{pattern_name}:{source_text}:{source_summary}"),
        platform=clean_text(platform) or "x",
        source_url=clean_optional(source_url),
        source_author=clean_optional(source_author),
        source_text=clean_optional(source_text),
        source_summary=clean_optional(source_summary) or summarize_source(source_text),
        topic=clean_optional(topic),
        category=normalize_category(category),
        pattern_name=clean_optional(pattern_name) or DEFAULT_PATTERN_NAME,
        hook_style=clean_optional(hook_style),
        structure=clean_optional(structure),
        pacing=clean_optional(pacing),
        tone=clean_optional(tone),
        formatting=clean_optional(formatting),
        why_it_worked=clean_optional(why_it_worked),
        adaptation_rule=clean_optional(adaptation_rule) or DEFAULT_ADAPTATION_RULE,
        engagement_notes=clean_optional(engagement_notes),
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
        bookmarks=bookmarks,
        impressions=impressions,
        collected_at=clean_optional(collected_at) or utc_now(),
        window=clean_optional(window) or "manual",
        status=clean_optional(status) or "active",
        raw_text=raw_text,
    )
    return store.save_viral_pattern(saved)


def ingest_patterns_batch_csv(store: SQLiteStore, csv_path: str | Path) -> PatternBatchResult:
    imported = 0
    skipped = 0
    errors: list[str] = []
    categories: Counter[str] = Counter()
    pattern_names: Counter[str] = Counter()
    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            try:
                pattern = add_pattern_example(
                    store,
                    source_url=cell(row, "source_url"),
                    source_author=cell(row, "source_author"),
                    source_text=cell(row, "source_text"),
                    source_summary=cell(row, "source_summary"),
                    topic=cell(row, "topic"),
                    category=cell(row, "category"),
                    pattern_name=cell(row, "pattern_name"),
                    hook_style=cell(row, "hook_style"),
                    structure=cell(row, "structure"),
                    pacing=cell(row, "pacing"),
                    tone=cell(row, "tone"),
                    formatting=cell(row, "formatting"),
                    why_it_worked=cell(row, "why_it_worked"),
                    adaptation_rule=cell(row, "adaptation_rule"),
                    engagement_notes=cell(row, "engagement_notes"),
                    likes=optional_int(cell(row, "likes")),
                    replies=optional_int(cell(row, "replies")),
                    reposts=optional_int(cell(row, "reposts")),
                    quotes=optional_int(cell(row, "quotes")),
                    bookmarks=optional_int(cell(row, "bookmarks")),
                    impressions=optional_int(cell(row, "impressions")),
                    collected_at=cell(row, "collected_at"),
                    status=cell(row, "status") or "active",
                    window=cell(row, "window") or "manual",
                    raw_text=cell(row, "raw_text") or None,
                )
            except Exception as exc:  # noqa: BLE001 - import should continue and report row errors.
                skipped += 1
                errors.append(f"line {line_number}: {exc}")
                continue
            imported += 1
            categories.update([pattern.category or "other"])
            pattern_names.update([pattern.pattern_name or "Observed pattern"])
    return PatternBatchResult(imported=imported, skipped=skipped, errors=errors, categories=categories, pattern_names=pattern_names)


def refresh_patterns(
    store: SQLiteStore,
    provider: DraftProvider | None,
    *,
    limit: int = 5,
    window: str = "daily",
    fetch_candidates: PatternCandidateFetcher | None = None,
) -> PatternRefreshResult:
    if provider is None:
        raise ValueError(PATTERN_REFRESH_MISSING_PROVIDER)
    if fetch_candidates is None and not scrapingdog_api_key():
        raise ValueError(PATTERN_REFRESH_MISSING_SCRAPINGDOG)

    candidates = (
        fetch_candidates(limit=limit)
        if fetch_candidates
        else fetch_scrapingdog_pattern_candidates(limit=limit)
    )
    stored: list[ViralPattern] = []
    errors: list[str] = []
    skipped = 0
    for candidate in candidates[:limit]:
        try:
            if not is_meaningful_pattern_source(candidate):
                skipped += 1
                continue
            analysis = analyze_pattern_candidate(provider, candidate)
            if not is_actionable_pattern_analysis(analysis):
                skipped += 1
                continue
            pattern = add_pattern_example(
                store,
                source_url=clean_optional(candidate.get("source_url") or candidate.get("url") or candidate.get("link")),
                source_author=clean_optional(candidate.get("source_author") or candidate.get("author") or candidate.get("source")),
                source_text=clean_optional(candidate.get("source_text") or candidate.get("text") or candidate.get("snippet") or candidate.get("description") or candidate.get("title")),
                source_summary=clean_optional(analysis.get("source_summary") or candidate.get("summary") or candidate.get("title")),
                topic=clean_optional(analysis.get("topic") or candidate.get("topic")),
                category=clean_optional(analysis.get("category")),
                pattern_name=clean_optional(analysis.get("pattern_name")),
                hook_style=clean_optional(analysis.get("hook_style")),
                structure=clean_optional(analysis.get("structure")),
                pacing=clean_optional(analysis.get("pacing")),
                tone=clean_optional(analysis.get("tone")),
                formatting=clean_optional(analysis.get("formatting")),
                why_it_worked=clean_optional(analysis.get("why_it_worked")),
                adaptation_rule=clean_optional(analysis.get("adaptation_rule")),
                engagement_notes=clean_optional(analysis.get("engagement_signal") or candidate.get("engagement_notes")),
                likes=optional_int(candidate.get("likes")),
                replies=optional_int(candidate.get("replies")),
                reposts=optional_int(candidate.get("reposts")),
                quotes=optional_int(candidate.get("quotes")),
                bookmarks=optional_int(candidate.get("bookmarks")),
                impressions=optional_int(candidate.get("impressions")),
                window=window,
                status="active",
                raw_text=json.dumps({"candidate": candidate, "analysis": analysis}, sort_keys=True),
            )
        except Exception as exc:  # noqa: BLE001 - one candidate should not fail the refresh.
            skipped += 1
            errors.append(str(exc))
            continue
        stored.append(pattern)
    return PatternRefreshResult(fetched=len(candidates), stored=len(stored), skipped=skipped, errors=errors, patterns=stored)


def select_relevant_patterns(
    store: SQLiteStore,
    *,
    topic: str | None = None,
    pattern_ids: list[str] | None = None,
    limit: int = 3,
) -> list[ViralPattern]:
    selectable_statuses = {"active", "saved"}
    if pattern_ids:
        patterns = [
            pattern for pattern_id in pattern_ids
            if (pattern := store.get_viral_pattern(pattern_id)) is not None and pattern.status in selectable_statuses
        ]
        return patterns[:limit]
    active = [
        *store.list_viral_patterns(status="saved", limit=100),
        *store.list_viral_patterns(status="active", limit=100),
    ]
    seen: set[str] = set()
    active = [pattern for pattern in active if not (pattern.id in seen or seen.add(pattern.id))]
    ranked = sorted(active, key=lambda pattern: pattern_rank(pattern, topic=topic), reverse=True)
    return ranked[:limit]


def fetch_scrapingdog_pattern_candidates(*, limit: int = 5) -> list[dict[str, object]]:
    api_key = scrapingdog_api_key()
    if not api_key:
        raise ValueError(PATTERN_REFRESH_MISSING_SCRAPINGDOG)
    payload = fetch_json(
        SCRAPINGDOG_NEWS_URL,
        {
            "api_key": api_key,
            "query": PATTERN_REFRESH_QUERY,
            "results": str(max(limit, 5)),
            "country": "us",
            "language": "en",
            "tbs": "qdr:d",
        },
        timeout=SCRAPINGDOG_TIMEOUT_SECONDS,
    )
    results = payload.get("news_results", [])
    if not isinstance(results, list):
        return []
    candidates: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        candidates.append(
            {
                "source_url": item.get("link") or item.get("url"),
                "source_author": source.get("name") if isinstance(source, dict) else source,
                "source_text": item.get("snippet") or item.get("description") or item.get("title"),
                "source_summary": item.get("title"),
                "raw": item,
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def analyze_pattern_candidate(provider: DraftProvider, candidate: dict[str, object]) -> dict[str, object]:
    source_text = clean_text(candidate.get("source_text") or candidate.get("text") or candidate.get("snippet") or candidate.get("title"))
    source_url = clean_text(candidate.get("source_url") or candidate.get("url") or candidate.get("link"))
    prompt = f"""Analyze this source X/social post as a reusable post structure for Koinoniko.

Do not copy the wording. Extract the mechanic only.

Source URL:
{source_url or "unknown"}

Source text:
{source_text or "No text provided."}

Return compact JSON only with:
pattern_name, source_summary, hook_style, structure, pacing, tone, formatting, why_it_worked, adaptation_rule, what_not_to_copy, category, topic, engagement_signal.

Categories must be one of: ai, sales, lead gen, gtm, bittensor, founder, product, other.
The adaptation_rule should be concise and should include: Use the structure, not the wording."""
    raw = generate_with_provider(provider, prompt, variants=1)[0]
    parsed = parse_json_object(raw)
    if not parsed:
        raise ValueError("Provider did not return pattern analysis JSON.")
    parsed["category"] = normalize_category(parsed.get("category"))
    if not clean_optional(parsed.get("pattern_name")):
        parsed["pattern_name"] = DEFAULT_PATTERN_NAME
    if not clean_optional(parsed.get("adaptation_rule")):
        parsed["adaptation_rule"] = DEFAULT_ADAPTATION_RULE
    return parsed


def pattern_rank(pattern: ViralPattern, *, topic: str | None = None) -> tuple[int, int, str]:
    topic_text = clean_text(topic).lower()
    haystack = " ".join(
        clean_text(value).lower()
        for value in [pattern.topic, pattern.category, pattern.pattern_name, pattern.structure, pattern.why_it_worked]
    )
    relevance = 1 if topic_text and topic_text in haystack else 0
    engagement = sum(value or 0 for value in [pattern.likes, pattern.replies * 3 if pattern.replies else None, pattern.reposts * 2 if pattern.reposts else None, pattern.bookmarks * 4 if pattern.bookmarks else None])
    return (relevance, engagement, pattern.collected_at or pattern.updated_at)


def parse_json_object(raw: str) -> dict[str, object] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def pattern_groups(store: SQLiteStore, *, limit: int = 5) -> dict[str, list[ViralPattern]]:
    def visible(window: str) -> list[ViralPattern]:
        query_limit = max(limit * 4, 20)
        patterns = [
            *store.list_viral_patterns(status="active", window=window, limit=query_limit),
            *store.list_viral_patterns(status="saved", window=window, limit=query_limit),
        ]
        seen: set[str] = set()
        return [
            pattern for pattern in patterns
            if not is_placeholder_pattern(pattern) and not (pattern.id in seen or seen.add(pattern.id))
        ][:limit]

    return {
        "daily": visible("daily"),
        "weekly": visible("weekly"),
    }


def stable_pattern_id(value: str | None) -> str:
    digest = hashlib.sha256(clean_text(value).encode("utf-8")).hexdigest()[:18]
    return f"pattern_{digest}"


def summarize_source(text: str | None, max_length: int = 140) -> str | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= max_length else f"{cleaned[: max_length - 3].rstrip()}..."


def candidate_source_text(candidate: dict[str, object]) -> str:
    return clean_text(
        candidate.get("source_text")
        or candidate.get("text")
        or candidate.get("snippet")
        or candidate.get("description")
        or candidate.get("title")
    )


def is_meaningful_pattern_source(candidate: dict[str, object]) -> bool:
    return is_meaningful_pattern_source_text(candidate_source_text(candidate))


def is_meaningful_pattern_source_text(value: object) -> bool:
    cleaned = clean_text(value)
    if len(cleaned) < MIN_PATTERN_SOURCE_TEXT_LENGTH:
        return False
    lowered = cleaned.lower()
    if lowered in {"unknown", "no text provided", "no source text stored"}:
        return False
    if re.search(r"\(@[^)]+\)\s*/\s*(posts|with replies|replies|media|likes)\s*/\s*x$", cleaned, flags=re.I):
        return False
    return True


def is_actionable_pattern_analysis(analysis: dict[str, object]) -> bool:
    return any(
        clean_optional(analysis.get(key))
        for key in ["structure", "hook_style", "pacing", "tone", "formatting", "why_it_worked"]
    )


def is_placeholder_pattern(pattern: ViralPattern) -> bool:
    default_name = clean_text(pattern.pattern_name).lower() == DEFAULT_PATTERN_NAME.lower()
    default_rule = clean_text(pattern.adaptation_rule).lower() in {"", DEFAULT_ADAPTATION_RULE.lower()}
    has_analysis = any(
        clean_optional(value)
        for value in [
            pattern.structure,
            pattern.hook_style,
            pattern.pacing,
            pattern.tone,
            pattern.formatting,
            pattern.why_it_worked,
        ]
    )
    has_source = any(is_meaningful_pattern_source_text(value) for value in [pattern.source_text, pattern.source_summary])
    return default_name and default_rule and not has_analysis and not has_source


def normalize_category(value: object) -> str:
    cleaned = clean_text(value).lower().replace("lead generation", "lead gen")
    if cleaned in PATTERN_CATEGORIES:
        return cleaned
    if "bittensor" in cleaned:
        return "bittensor"
    if "lead" in cleaned:
        return "lead gen"
    if "sales" in cleaned or "sdr" in cleaned:
        return "sales"
    if "gtm" in cleaned:
        return "gtm"
    if "founder" in cleaned or "operator" in cleaned:
        return "founder"
    if "product" in cleaned:
        return "product"
    if "ai" in cleaned or "agent" in cleaned:
        return "ai"
    return "other"


def cell(row: dict[str, str], key: str) -> str:
    return clean_text(row.get(key))


def clean_optional(value: object) -> str | None:
    cleaned = clean_text(value)
    return cleaned or None


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def optional_int(value: object) -> int | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def google_search_url(query: str) -> str:
    return f"https://www.google.com/search?{urlencode({'q': query})}"
