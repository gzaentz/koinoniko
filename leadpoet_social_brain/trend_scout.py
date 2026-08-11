from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from leadpoet_social_brain.core.models import DEFAULT_WORKSPACE_ID, TrendItem, utc_now
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore
from leadpoet_social_brain.voice import load_positioning_rules, load_voice_rules_override


SCRAPINGDOG_API_KEY_ENV = "SCRAPINGDOG_API_KEY"
SCRAPINGDOG_API_KEY_ALT_ENV = "SCRAPING_DOG_API_KEY"
SCRAPINGDOG_TRENDS_URL = "https://api.scrapingdog.com/google_trends/trending_now"
SCRAPINGDOG_NEWS_URL = "https://api.scrapingdog.com/google_news"
MISSING_SCRAPINGDOG_TRENDS_MESSAGE = "ScrapingDog API key is not configured; trend refresh skipped."
SCRAPINGDOG_TIMEOUT_SECONDS = 45
SOURCE_FETCH_TIMEOUT_SECONDS = 8
SOURCE_TEXT_EXCERPT_CHARS = 3500
SOURCE_PROMPT_TEXT_CHARS = 5000
SOURCE_EXCERPT_CHARS = 700
WEB_DAILY_SOURCE_TYPE = "web_daily"
WEB_WEEKLY_SOURCE_TYPE = "web_weekly"
SCRAPINGDOG_NEWS_QUERY = (
    '("sales AI" OR "AI SDR" OR "AI sales agent" OR "sales automation" OR '
    '"revenue operations" OR RevOps OR "B2B sales")'
)
SCRAPINGDOG_NEWS_QUERIES = (
    SCRAPINGDOG_NEWS_QUERY,
    '("B2B outbound" OR "sales development") '
    '(deliverability OR prospecting OR "follow-up" OR messaging OR personalization OR qualification)',
    '(Salesforce OR HubSpot OR Apollo OR ZoomInfo OR Clay OR Outreach OR Salesloft) '
    '(AI OR agent OR signals OR intent) (sales OR outbound OR pipeline OR CRM)',
    '("B2B sales" OR "enterprise sales") '
    '(discovery OR "objection handling" OR qualification OR demos OR "sales calls")',
    '(site:techcrunch.com OR site:venturebeat.com OR site:news.crunchbase.com OR site:martech.org) '
    '("sales AI" OR RevOps OR CRM OR GTM OR outbound)',
    '(site:salesforce.com/news OR site:hubspot.com/company-news OR site:outreach.io/resources/blog '
    'OR site:salesloft.com/resources/blog) (AI OR agent OR CRM OR sales OR outbound)',
    '("intent data" OR "buyer signals" OR "account intelligence" OR "data enrichment") '
    '(sales OR outbound OR "lead generation" OR GTM)',
    '(GTM OR "go-to-market" OR RevOps OR CRM) ("AI agent" OR "AI agents" OR automation) '
    '(pipeline OR outbound OR "sales workflow")',
    '("lead generation" OR "lead gen" OR prospecting OR outbound) '
    '("AI" OR agent OR automation) ("buyer intent" OR "account signals" OR "data enrichment" OR "account context")',
    '("B2B sales" OR "B2B marketing" OR "revenue teams") '
    '("AI agents" OR "AI automation" OR "buyer intent" OR "account signals")',
    '("AI-native CRM" OR "agentic CRM" OR "GTM AI") '
    '(outbound OR pipeline OR "sales workflow" OR "revenue team")',
    '("AI outbound" OR "AI sales agent" OR "sales development") '
    '("meetings" OR pipeline OR prospects OR "buyer signals")',
    '("revenue intelligence" OR RevTech OR "go-to-market data") '
    '(AI OR agents OR automation OR "buyer intent")',
    '("demand generation" OR "revenue marketing") '
    '(AI OR "buyer signals" OR "intent data" OR "account signals")',
    '("lead scoring" OR "predictive lead scoring" OR "account scoring") '
    '(AI OR "intent data" OR "buyer intent" OR "sales automation")',
    '("GTM engineer" OR "go-to-market engineer" OR "revenue operations") '
    '(automation OR AI OR agents OR outbound)',
)
SCRAPINGDOG_NEWS_QUERIES_PER_WINDOW = 6
SCRAPINGDOG_MIN_NEWS_QUERY_LANES = 4
DEFAULT_WEB_TRENDS_PER_WINDOW = 10
WEB_DAILY_FRESHNESS_DAYS = 2
WEB_WEEKLY_FRESHNESS_DAYS = 8
NEWS_MIN_DISTINCT_PUBLISHERS = 5
TREND_RELEVANCE_KEEP_THRESHOLD = 5
TREND_REQUIRED_RELEVANCE_TERMS = {
    "account signal",
    "account signals",
    "ai sales",
    "ai sdr",
    "b2b",
    "buyer intent",
    "buyer signal",
    "buyer signals",
    "buyer timing",
    "buying intent",
    "buying window",
    "crm",
    "data enrichment",
    "go-to-market",
    "gtm",
    "intent data",
    "lead gen",
    "lead generation",
    "outbound",
    "pipeline",
    "prospecting",
    "revops",
    "sales pipeline",
    "sales ai",
    "sales automation",
    "sales engagement",
    "sales workflow",
    "sdr",
}
TREND_AMBIGUOUS_REQUIRED_RELEVANCE_TERMS = {
    "crm",
    "pipeline",
}
TREND_SUPPORTING_RELEVANCE_TERMS = {
    "b2b",
    "go-to-market",
    "gtm",
    "lead gen",
    "lead generation",
    "outbound",
    "prospecting",
    "revenue",
    "revops",
    "sales",
    "sdr",
}
TREND_RELEVANCE_WEIGHTS = {
    "account context": 5,
    "account signal": 5,
    "account signals": 5,
    "ai sales": 5,
    "ai sdr": 6,
    "b2b": 2,
    "buyer context": 5,
    "buyer intent": 6,
    "buyer signal": 5,
    "buyer signals": 5,
    "buyer timing": 6,
    "buying intent": 6,
    "buying window": 5,
    "buying windows": 5,
    "crm": 3,
    "data enrichment": 4,
    "go-to-market": 4,
    "gtm": 4,
    "hubspot": 2,
    "intent data": 6,
    "lead gen": 5,
    "lead generation": 5,
    "outbound": 5,
    "pipeline": 4,
    "prospecting": 4,
    "revops": 4,
    "sales": 3,
    "sales ai": 5,
    "sales automation": 5,
    "sales engagement": 5,
    "sales workflow": 4,
    "salesforce": 2,
    "sdr": 4,
    "sequence": 2,
    "timing signal": 5,
    "timing signals": 5,
    "workflow timing": 4,
    "ai": 1,
    "artificial intelligence": 1,
    "automation": 1,
    "generative": 1,
}
TREND_NEGATIVE_WEIGHTS = {
    "actor": 4,
    "album": 4,
    "basketball": 4,
    "best ai sales tools": 8,
    "budget deal": 4,
    "celebrity": 6,
    "consumer app": 5,
    "election": 5,
    "earnings call transcript": 8,
    "flight bookings": 8,
    "football": 4,
    "full evaluation": 8,
    "gossip": 6,
    "governor": 4,
    "hotel searches": 8,
    "iphone": 4,
    "movie": 5,
    "music": 4,
    "photo app": 5,
    "pricing": 8,
    "politics": 5,
    "recipe": 5,
    "review 2026": 8,
    "review and pricing": 10,
    "senate": 4,
    "sports": 5,
    "stock price": 3,
    "summer travel": 8,
    "top 10 best": 8,
    "travel": 6,
    "wanderlust": 8,
    "which is better": 8,
    "world cup": 8,
}
TREND_HARD_REJECT_TERMS = {
    "best ai sales tools",
    "earnings call transcript",
    "flight bookings",
    "full evaluation",
    "hotel searches",
    "review and pricing",
    "summer travel",
    "top 10 best",
    "wanderlust",
    "which is better",
    "world cup",
}
TREND_FINANCE_REJECT_TERMS = {
    "after hours trading",
    "best brokers",
    "bonds",
    "buy stock",
    "compound interest",
    "dividend",
    "etf",
    "etfs",
    "financial dictionary",
    "how to invest",
    "index funds",
    "investing",
    "jim cramer",
    "nasdaq",
    "nyse",
    "stock",
    "stock market",
    "stock price",
    "stock split",
    "stock splits",
    "stocks to buy",
    "wall street",
}
TREND_FINANCE_SOURCE_TERMS = {
    "barron's",
    "investorplace",
    "marketwatch",
    "motley fool",
    "seeking alpha",
    "the motley fool",
    "yahoo finance",
}
TREND_RELEVANCE_TERMS = {
    *TREND_REQUIRED_RELEVANCE_TERMS,
    *TREND_RELEVANCE_WEIGHTS,
    "ai",
    "a.i.",
    "artificial intelligence",
    "agent",
    "agents",
    "automation",
    "b2b",
    "chatgpt",
    "gemini",
    "generative",
    "gtm",
    "hubspot",
    "lead gen",
    "lead generation",
    "openai",
    "outbound",
    "pipeline",
    "sales",
    "salesforce",
    "sdr",
}
FetchArticles = Callable[..., list[dict[str, object]]]
FetchSourcePage = Callable[[str], str]


@dataclass(frozen=True)
class WebTrendGroups:
    daily: list[TrendItem]
    weekly: list[TrendItem]
    errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    candidates_found: int = 0
    trends_stored: int = 0
    enriched: int = 0
    partial: int = 0
    failed: int = 0


@dataclass(frozen=True)
class TrendScoutProfile:
    user_name: str = "Gavin"
    workspace_name: str = "Leadpoet"
    positioning_rules: list[str] = field(default_factory=list)
    voice_rules: list[str] = field(default_factory=list)
    context_sections: list[str] = field(default_factory=list)

    @property
    def focus_summary(self) -> str:
        pieces = [
            *self.positioning_rules[:4],
            *self.context_sections[:6],
        ]
        return "\n".join(f"- {piece}" for piece in pieces if piece) or "- No saved context yet."

    @property
    def keep_guidance(self) -> str:
        return (
            "B2B GTM, outbound, lead generation, AI SDRs, sales workflows, buyer intent, "
            "account signals, pipeline prioritization, CRM/RevOps, data enrichment, buyer timing, "
            "or a directly relevant saved company priority."
        )

    @property
    def default_angle(self) -> str:
        if self.workspace_name == "Leadpoet":
            return (
                "Use the source to teach one concrete GTM mechanism, tradeoff, or decision rule. "
                "Connect it to Leadpoet only when that connection materially improves the reader's understanding."
            )
        return (
            f"Explain the concrete mechanism or reader lesson in this signal. Connect it to {self.workspace_name} "
            "only when the saved positioning makes the lesson more specific."
        )


@dataclass(frozen=True)
class SourcePageDetails:
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    excerpt: str | None = None
    text_excerpt: str | None = None
    key_paragraphs: list[str] = field(default_factory=list)
    important_numbers: list[str] = field(default_factory=list)
    mentioned_names: list[str] = field(default_factory=list)
    error: str | None = None


def refresh_web_trends(
    store: SQLiteStore,
    *,
    fetch_articles: FetchArticles | None = None,
    limit: int = DEFAULT_WEB_TRENDS_PER_WINDOW,
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
    scrapingdog_timeout_seconds: int = SCRAPINGDOG_TIMEOUT_SECONDS,
    max_news_queries_per_window: int = SCRAPINGDOG_NEWS_QUERIES_PER_WINDOW,
    deadline_seconds: int | None = None,
) -> WebTrendGroups:
    profile = build_trend_scout_profile(store)
    if fetch_articles:
        return refresh_injected_web_trends(
            store,
            fetch_articles=fetch_articles,
            limit=limit,
            provider=provider,
            enrich=enrich,
            enrich_limit=enrich_limit,
            fetch_source_page=fetch_source_page,
            profile=profile,
        )

    errors: list[str] = []
    sources: list[str] = []
    stats = empty_refresh_stats()
    deadline = trend_refresh_deadline(deadline_seconds)
    api_key = scrapingdog_api_key()
    if not api_key:
        errors.append(MISSING_SCRAPINGDOG_TRENDS_MESSAGE)
        return WebTrendGroups(daily=[], weekly=[], errors=errors, sources=sources, **stats)
    current = datetime.now(timezone.utc)
    news_articles = fetch_scrapingdog_news_pool(
        api_key=api_key,
        profile=profile,
        limit=limit,
        errors=errors,
        sources=sources,
        timeout_seconds=scrapingdog_timeout_seconds,
        max_queries=max_news_queries_per_window,
        deadline=deadline,
        current=current,
    )
    stats["candidates_found"] += len(news_articles)
    daily = refresh_web_trends_for_period(
        store,
        period="daily",
        source_type=WEB_DAILY_SOURCE_TYPE,
        scrapingdog_hours=24,
        limit=limit,
        scrapingdog_api_key=api_key or None,
        errors=errors,
        sources=sources,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        profile=profile,
        scrapingdog_timeout_seconds=scrapingdog_timeout_seconds,
        deadline=deadline,
        news_articles=news_articles,
        current=current,
        freshness_days=WEB_DAILY_FRESHNESS_DAYS,
    )
    daily_urls = {canonical_source_url(trend.source_url) for trend in daily if canonical_source_url(trend.source_url)}
    weekly = refresh_web_trends_for_period(
        store,
        period="weekly",
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        scrapingdog_hours=168,
        limit=limit,
        scrapingdog_api_key=api_key or None,
        errors=errors,
        sources=sources,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        profile=profile,
        exclude_source_urls=daily_urls,
        scrapingdog_timeout_seconds=scrapingdog_timeout_seconds,
        deadline=deadline,
        news_articles=news_articles,
        current=current,
        freshness_days=WEB_WEEKLY_FRESHNESS_DAYS,
    )
    return WebTrendGroups(daily=daily, weekly=weekly, errors=errors, sources=unique_texts(sources), **stats)


def trend_refresh_deadline(deadline_seconds: int | None) -> float | None:
    if deadline_seconds is None or deadline_seconds <= 0:
        return None
    return time.monotonic() + deadline_seconds


def trend_refresh_call_timeout(deadline: float | None, requested_timeout: int) -> int:
    requested_timeout = max(1, requested_timeout)
    if deadline is None:
        return requested_timeout
    remaining = deadline - time.monotonic() - 1.0
    if remaining < 1:
        return 0
    return max(1, min(requested_timeout, int(remaining)))


def scrapingdog_api_key() -> str:
    return clean_text(os.getenv(SCRAPINGDOG_API_KEY_ENV)) or clean_text(os.getenv(SCRAPINGDOG_API_KEY_ALT_ENV))


def scrapingdog_configured() -> bool:
    return bool(scrapingdog_api_key())


def build_trend_scout_profile(store: SQLiteStore) -> TrendScoutProfile:
    positioning_rules = load_positioning_rules(store)
    voice_rules = load_voice_rules_override(store) or []
    context_sections = [
        f"{section.kind}/{section.name}: {truncate_text(section.content, 520)}"
        for section in store.list_context_sections(
            kinds={"writer", "product", "audience", "positioning", "proof", "priority", "guardrail", "sales_llm_vision", "bittensor"},
            active_only=True,
        )
        if clean_text(section.content)
    ]
    workspace_name = workspace_name_from_text("\n".join([*positioning_rules, *context_sections]))
    if not workspace_name:
        workspace_name = "Leadpoet" if store.workspace_id == DEFAULT_WORKSPACE_ID else "this workspace"
    user_name = "Gavin" if store.workspace_id == DEFAULT_WORKSPACE_ID else "the writer"
    return TrendScoutProfile(
        user_name=user_name,
        workspace_name=workspace_name,
        positioning_rules=positioning_rules,
        voice_rules=voice_rules,
        context_sections=context_sections,
    )


def workspace_name_from_text(text: str) -> str | None:
    stopwords = {
        "A",
        "AI",
        "An",
        "Bittensor",
        "Context",
        "Facts",
        "Gavin",
        "Koinoniko",
        "Lead",
        "Other",
        "Sales",
        "The",
        "This",
        "When",
    }
    patterns = [
        r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})\s+(?:is|owns|helps|turns|gives|makes|builds|provides)\b",
        r"\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})'s\s+(?:core|unique|main|primary|product|platform|wedge|advantage)\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = " ".join(match.group(1).split())
            words = candidate.split()
            if candidate and len(words) <= 3 and not any(word in stopwords for word in words):
                return candidate
    if re.search(r"\bLeadpoet\b", text):
        return "Leadpoet"
    return None


def scrapingdog_news_query(profile: TrendScoutProfile) -> str:
    return scrapingdog_news_queries(profile)[0]


def scrapingdog_news_queries(profile: TrendScoutProfile) -> list[str]:
    queries = list(SCRAPINGDOG_NEWS_QUERIES)
    if profile.workspace_name == "Leadpoet":
        return queries
    return unique_texts(
        [
            *queries,
            f'"{profile.workspace_name}" ("sales" OR GTM OR outbound OR "buyer intent" OR "account signals")',
            f'("{profile.workspace_name}" OR "account intelligence" OR "buyer timing" OR "account signals") '
            '("AI" OR sales OR outbound OR GTM)',
        ]
    )


def scrapingdog_news_windows(period: str) -> list[str]:
    if period == "daily":
        return ["qdr:d"]
    if period == "weekly":
        return ["qdr:w"]
    return ["qdr:w"]


def fetch_scrapingdog_news_pool(
    *,
    api_key: str,
    profile: TrendScoutProfile,
    limit: int,
    errors: list[str],
    sources: list[str],
    timeout_seconds: int,
    max_queries: int,
    deadline: float | None,
    current: datetime,
) -> list[dict[str, object]]:
    articles: list[dict[str, object]] = []
    news_errors: list[str] = []
    fetched_news = False
    stopped_for_budget = False
    queries = scrapingdog_news_queries(profile)[: max(0, max_queries)]
    minimum_query_lanes = min(len(queries), SCRAPINGDOG_MIN_NEWS_QUERY_LANES)
    for index, query in enumerate(queries):
        if index >= minimum_query_lanes and news_pool_has_coverage(articles, limit=limit, current=current):
            break
        call_timeout = trend_refresh_call_timeout(deadline, timeout_seconds)
        if call_timeout <= 0:
            stopped_for_budget = True
            break
        try:
            results = fetch_scrapingdog_google_news(
                api_key=api_key,
                tbs=scrapingdog_news_windows("weekly")[0],
                limit=25,
                query=query,
                timeout_seconds=call_timeout,
            )
            articles.extend(scrapingdog_news_articles(results))
            fetched_news = True
        except Exception as exc:  # noqa: BLE001 - one search lane should not block the rest.
            news_errors.append(str(exc))
    if fetched_news:
        sources.append("Scrapingdog Google News")
    if stopped_for_budget:
        errors.append("Scrapingdog Google News stopped because refresh time budget was exhausted.")
    elif news_errors:
        errors.append(f"Scrapingdog Google News failed for one or more query lanes: {news_errors[0]}")
    return articles


def news_pool_has_coverage(
    articles: list[dict[str, object]],
    *,
    limit: int,
    current: datetime,
) -> bool:
    if limit <= 0:
        return True
    weekly_articles = [
        article
        for article in articles
        if article_is_recent(article, current=current, days=WEB_WEEKLY_FRESHNESS_DAYS)
    ]
    daily_articles = [
        article
        for article in weekly_articles
        if article_is_recent(article, current=current, days=WEB_DAILY_FRESHNESS_DAYS)
    ]
    daily = select_web_trend_articles(daily_articles, limit=limit)
    weekly = select_web_trend_articles(weekly_articles, limit=limit * 2)
    distinct_publishers = {
        article_source_key(article)
        for article in weekly
        if article_source_key(article) != "unknown"
    }
    publisher_target = min(NEWS_MIN_DISTINCT_PUBLISHERS, limit)
    return (
        len(daily) >= limit
        and len(weekly) >= limit * 2
        and len(distinct_publishers) >= publisher_target
    )


def refresh_injected_web_trends(
    store: SQLiteStore,
    *,
    fetch_articles: FetchArticles,
    limit: int = DEFAULT_WEB_TRENDS_PER_WINDOW,
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
    profile: TrendScoutProfile | None = None,
) -> WebTrendGroups:
    profile = profile or build_trend_scout_profile(store)
    stats = empty_refresh_stats()
    daily = save_web_trend_articles(
        store,
        articles=fetch_articles(timespan="1d", limit=limit * 4),
        source_type=WEB_DAILY_SOURCE_TYPE,
        limit=limit,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        profile=profile,
        count_candidates=True,
    )
    weekly = save_web_trend_articles(
        store,
        articles=fetch_articles(timespan="7d", limit=limit * 4),
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        limit=limit,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        profile=profile,
        count_candidates=True,
    )
    return WebTrendGroups(daily=daily, weekly=weekly, sources=["Injected test source"], **stats)


def refresh_web_trends_for_period(
    store: SQLiteStore,
    *,
    period: str,
    source_type: str,
    scrapingdog_hours: int,
    limit: int,
    scrapingdog_api_key: str | None,
    errors: list[str],
    sources: list[str],
    stats: dict[str, int],
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
    profile: TrendScoutProfile | None = None,
    exclude_source_urls: set[str] | None = None,
    scrapingdog_timeout_seconds: int = SCRAPINGDOG_TIMEOUT_SECONDS,
    deadline: float | None = None,
    news_articles: list[dict[str, object]] | None = None,
    current: datetime | None = None,
    freshness_days: int = WEB_WEEKLY_FRESHNESS_DAYS,
) -> list[TrendItem]:
    profile = profile or build_trend_scout_profile(store)
    current = current or datetime.now(timezone.utc)
    articles = [
        article
        for article in news_articles or []
        if article_is_recent(article, current=current, days=freshness_days)
    ]
    trend_articles: list[dict[str, object]] = []
    if scrapingdog_api_key:
        call_timeout = trend_refresh_call_timeout(deadline, scrapingdog_timeout_seconds)
        if call_timeout <= 0:
            errors.append(f"{period} Scrapingdog Google Trends skipped because refresh time budget was exhausted.")
        else:
            try:
                trend_articles = scrapingdog_trending_articles(
                    fetch_scrapingdog_trending_now(
                        api_key=scrapingdog_api_key,
                        hours=scrapingdog_hours,
                        limit=50,
                        timeout_seconds=call_timeout,
                    ),
                    hours=scrapingdog_hours,
                )
                sources.append("Scrapingdog Google Trends")
            except Exception as exc:  # noqa: BLE001 - third-party trend source can fail independently.
                errors.append(f"{period} Scrapingdog Google Trends failed: {exc}")

    articles.extend(trend_articles)
    stats["candidates_found"] += len(trend_articles)
    selected = select_web_trend_articles(
        articles,
        limit=limit,
        exclude_source_urls=exclude_source_urls,
    )
    return save_web_trend_articles(
        store,
        articles=selected,
        source_type=source_type,
        limit=limit,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        profile=profile,
        exclude_source_urls=exclude_source_urls,
    )


def fetch_scrapingdog_trending_now(
    *,
    api_key: str,
    hours: int,
    limit: int = 50,
    geo: str = "US",
    language: str = "en",
    timeout_seconds: int = SCRAPINGDOG_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    payload = fetch_json(
        SCRAPINGDOG_TRENDS_URL,
        {
            "api_key": api_key,
            "geo": geo,
            "language": language,
            "hours": str(hours),
        },
        timeout=timeout_seconds,
    )
    searches = payload.get("trending_searches", [])
    if not isinstance(searches, list):
        return []
    return [item for item in searches[:limit] if isinstance(item, dict)]


def fetch_scrapingdog_google_news(
    *,
    api_key: str,
    tbs: str,
    limit: int = 25,
    country: str = "us",
    language: str = "en",
    query: str | None = None,
    timeout_seconds: int = SCRAPINGDOG_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    payload = fetch_json(
        SCRAPINGDOG_NEWS_URL,
        {
            "api_key": api_key,
            "query": query or SCRAPINGDOG_NEWS_QUERY,
            "results": str(limit),
            "country": country,
            "language": language,
            "tbs": tbs,
        },
        timeout=timeout_seconds,
    )
    results = payload.get("news_results", [])
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def fetch_json(url: str, params: dict[str, str], *, timeout: int) -> dict[str, object]:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "KoinonikoTrendScout/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected JSON response")
    return payload


def scrapingdog_trending_articles(
    searches: list[dict[str, object]],
    *,
    hours: int,
) -> list[dict[str, object]]:
    articles: list[dict[str, object]] = []
    for item in searches:
        title = clean_text(item.get("title"))
        if not title:
            continue
        breakdown = item.get("trend_breakdown")
        breakdown_text = " ".join(clean_text(value) for value in breakdown if value) if isinstance(breakdown, list) else ""
        details = []
        search_volume = item.get("search_volume")
        increase = item.get("increase_percentage")
        if search_volume:
            details.append(f"{format_number(search_volume)} searches")
        if increase:
            details.append(f"{increase}% increase")
        descriptor = f" with {', '.join(details)}" if details else ""
        articles.append(
            {
                **item,
                "provider": "scrapingdog_trending_now",
                "title": title,
                "summary": f"Google Trends surfaced this search in the last {hours} hours{descriptor}.",
                "sourceCommonName": "Google Trends",
                "url": google_trends_url(title),
                "date": item.get("start_timestamp") or item.get("start_date"),
                "trend_breakdown_text": breakdown_text,
            }
        )
    return articles


def scrapingdog_news_articles(results: list[dict[str, object]]) -> list[dict[str, object]]:
    articles: list[dict[str, object]] = []
    for item in results:
        title = clean_text(item.get("title"))
        if not title:
            continue
        source = item.get("source")
        if isinstance(source, dict):
            source_name = clean_text(source.get("name"))
        else:
            source_name = clean_text(source)
        articles.append(
            {
                **item,
                "provider": "scrapingdog_google_news",
                "title": title,
                "summary": clean_text(item.get("snippet") or item.get("description")),
                "sourceCommonName": source_name or "Google News",
                "url": item.get("link") or item.get("url"),
                "date": item.get("date") or item.get("published_at") or item.get("lastUpdated"),
            }
        )
    return articles


def select_web_trend_articles(
    articles: list[dict[str, object]],
    *,
    limit: int,
    exclude_source_urls: set[str] | None = None,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    ranked: list[dict[str, object]] = []
    seen: set[str] = set()
    excluded = exclude_source_urls or set()
    for article in sorted(articles, key=web_trend_rank_key, reverse=True):
        if web_trend_relevance_score(article) < TREND_RELEVANCE_KEEP_THRESHOLD:
            continue
        title = clean_text(article.get("title"))
        url = clean_text(article.get("url") or article.get("link") or article.get("url_mobile"))
        if canonical_source_url(url) in excluded:
            continue
        dedupe_key = (url or title).lower()
        if not title or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        ranked.append(article)

    # Give each publisher one slot before filling the remaining slots by rank.
    # This prevents one syndicated outlet from crowding out independent coverage.
    selected: list[dict[str, object]] = []
    remaining: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for article in ranked:
        source_key = article_source_key(article)
        if source_key not in seen_sources:
            seen_sources.add(source_key)
            selected.append(article)
        else:
            remaining.append(article)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected.extend(remaining[: limit - len(selected)])
    return selected


def article_source_key(article: dict[str, object]) -> str:
    raw_source = (
        article.get("sourceCommonName")
        or article.get("source")
        or article.get("domain")
        or article.get("sourceCountry")
    )
    if isinstance(raw_source, dict):
        raw_source = raw_source.get("name") or raw_source.get("title")
    source = clean_text(raw_source).lower()
    if source:
        return re.sub(r"\s+", " ", source)
    url = clean_text(article.get("url") or article.get("link") or article.get("url_mobile"))
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.removeprefix("www.") or "unknown"


def article_published_at(article: dict[str, object]) -> datetime | None:
    raw_value = (
        article.get("seendate")
        or article.get("date")
        or article.get("published_at")
        or article.get("lastUpdated")
        or article.get("start_timestamp")
        or article.get("start_date")
    )
    if raw_value is None or clean_text(raw_value) == "":
        return None
    if isinstance(raw_value, (int, float)):
        timestamp = float(raw_value) / 1000 if float(raw_value) > 10_000_000_000 else float(raw_value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    clean_value = clean_text(raw_value)
    if len(clean_value) >= 14 and clean_value[:14].isdigit():
        return datetime.strptime(clean_value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return parse_trend_date(clean_value)


def article_is_recent(article: dict[str, object], *, current: datetime, days: int) -> bool:
    published_at = article_published_at(article)
    if published_at is None:
        return False
    return current - timedelta(days=days) <= published_at <= current + timedelta(hours=1)


def article_freshness_rank(article: dict[str, object]) -> int:
    published_at = article_published_at(article)
    return int(published_at.timestamp()) if published_at else 0


def web_trend_rank_key(article: dict[str, object]) -> tuple[int, int, int, int]:
    return (
        web_trend_relevance_score(article),
        article_freshness_rank(article),
        numeric_value(article.get("search_volume")),
        numeric_value(article.get("increase_percentage")),
    )


def web_trend_relevance_score(article: dict[str, object]) -> int:
    text = article_relevance_text(article)
    if article_looks_like_hard_reject(text):
        return 0
    if article_looks_like_investing_pick(article, text):
        return 0
    if not has_required_relevance_context(text):
        return 0
    positive = sum(
        weight
        for term, weight in TREND_RELEVANCE_WEIGHTS.items()
        if text_matches_term(text, term)
    )
    negative = sum(
        weight
        for term, weight in TREND_NEGATIVE_WEIGHTS.items()
        if text_matches_term(text, term)
    )
    return max(0, positive - negative)


def article_looks_like_hard_reject(text: str) -> bool:
    return any(text_matches_term(text, term) for term in TREND_HARD_REJECT_TERMS)


def article_looks_like_investing_pick(article: dict[str, object], text: str | None = None) -> bool:
    text = text or article_relevance_text(article)
    source = clean_text(
        article.get("sourceCommonName")
        or article.get("source")
        or article.get("domain")
        or article.get("sourceCountry")
    ).lower()
    has_finance_language = any(text_matches_term(text, term) for term in TREND_FINANCE_REJECT_TERMS)
    has_finance_source = any(term in source for term in TREND_FINANCE_SOURCE_TERMS)
    if not has_finance_language and not has_finance_source:
        return False
    protected_terms = TREND_REQUIRED_RELEVANCE_TERMS - TREND_AMBIGUOUS_REQUIRED_RELEVANCE_TERMS
    return not any(text_matches_term(text, term) for term in protected_terms)


def has_required_relevance_context(text: str) -> bool:
    strong_terms = TREND_REQUIRED_RELEVANCE_TERMS - TREND_AMBIGUOUS_REQUIRED_RELEVANCE_TERMS
    if any(text_matches_term(text, term) for term in strong_terms):
        return True
    has_ambiguous_term = any(text_matches_term(text, term) for term in TREND_AMBIGUOUS_REQUIRED_RELEVANCE_TERMS)
    has_supporting_term = any(text_matches_term(text, term) for term in TREND_SUPPORTING_RELEVANCE_TERMS)
    return has_ambiguous_term and has_supporting_term


def article_relevance_text(article: dict[str, object]) -> str:
    return " ".join(
        clean_text(value)
        for value in [
            article.get("title"),
            article.get("summary"),
            article.get("snippet"),
            article.get("description"),
            article.get("trend_breakdown_text"),
            article.get("sourceCommonName"),
        ]
    ).lower()


def text_matches_term(text: str, term: str) -> bool:
    if len(term) <= 3 or "." in term:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def canonical_source_url(value: object) -> str:
    text = clean_text(value).lower()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/")
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def save_web_trend_articles(
    store: SQLiteStore,
    *,
    articles: list[dict[str, object]],
    source_type: str,
    limit: int,
    stats: dict[str, int] | None = None,
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
    count_candidates: bool = True,
    profile: TrendScoutProfile | None = None,
    exclude_source_urls: set[str] | None = None,
) -> list[TrendItem]:
    profile = profile or build_trend_scout_profile(store)
    saved: list[TrendItem] = []
    seen: set[str] = set()
    excluded_urls = {canonical_source_url(url) for url in exclude_source_urls or set() if canonical_source_url(url)}
    if stats is not None and count_candidates:
        stats["candidates_found"] += len(articles)
    attempted_enrichments = 0
    max_enrichments = limit if enrich_limit is None else max(0, enrich_limit)
    for article in articles:
        title = clean_text(article.get("title"))
        url = clean_text(article.get("url") or article.get("url_mobile"))
        if not title:
            continue
        if canonical_source_url(url) in excluded_urls:
            continue
        if web_trend_relevance_score(article) < TREND_RELEVANCE_KEEP_THRESHOLD:
            continue
        dedupe_key = (url or title).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        trend = web_trend_from_article(article, source_type=source_type, profile=profile)
        if enrich and attempted_enrichments < max_enrichments and (provider or fetch_source_page):
            attempted_enrichments += 1
            trend = enrich_trend_source(
                trend,
                article,
                provider=provider,
                fetch_source_page=fetch_source_page or fetch_source_page_text,
                profile=profile,
            )
            if stats is not None:
                if trend.enrichment_status == "enriched":
                    stats["enriched"] += 1
                elif trend.enrichment_status == "failed":
                    stats["failed"] += 1
                elif trend.enrichment_status == "partial":
                    stats["partial"] += 1
        if trend.status != "unused":
            continue
        if trend_source_relevance_score(trend) < TREND_RELEVANCE_KEEP_THRESHOLD:
            if stats is not None:
                stats["failed"] += 1
            continue
        trend = store.save_trend_item(trend)
        if stats is not None:
            stats["trends_stored"] += 1
        saved.append(trend)
        if len(saved) >= limit:
            break
    return saved


def enrich_trend_source(
    trend: TrendItem,
    article: dict[str, object],
    *,
    provider: DraftProvider | None,
    fetch_source_page: FetchSourcePage,
    profile: TrendScoutProfile | None = None,
) -> TrendItem:
    profile = profile or TrendScoutProfile()
    page = SourcePageDetails()
    page_error = ""
    url = trend.source_url or clean_text(article.get("link") or article.get("url") or article.get("url_mobile"))
    if url and should_fetch_source_url(url):
        try:
            page = extract_source_page_details(fetch_source_page(url))
        except Exception as exc:  # noqa: BLE001 - one source page should not break trend refresh.
            page = SourcePageDetails(error=str(exc))
            page_error = f"source fetch failed: {exc}"
    elif url:
        page_error = "source URL is not an article page"
    else:
        page_error = "no source URL available"

    provider_error = ""
    digest: dict[str, object] = {}
    provider_digest = False
    if provider:
        try:
            raw_digest = generate_with_provider(
                provider,
                build_trend_digest_prompt(trend, article, page, profile=profile),
                variants=1,
            )[0]
            digest = parse_trend_digest(raw_digest)
            provider_digest = bool(digest)
        except Exception as exc:  # noqa: BLE001 - keep partial extraction if the model fails.
            provider_error = f"digest failed: {exc}"
    if not digest:
        digest = fallback_trend_digest(trend, article, page, profile=profile)

    if provider_digest and trend_digest_rejects(digest):
        reason = clean_text(digest.get("reject_reason") or digest.get("relevance_reason")) or "Rejected by Trend Scout relevance gate."
        return replace(
            trend,
            status="ignored",
            why_it_matters=reason,
            leadpoet_angle="",
            source_confidence="low",
            enriched_at=utc_now(),
            enrichment_status="rejected",
            enrichment_error=reason,
        )

    has_source_detail = bool(page.text_excerpt or page.key_paragraphs or page.title or page.excerpt)
    enrichment_error = "; ".join(error for error in [page_error, provider_error, page.error] if error) or None
    if provider_digest and has_source_detail and not page_error:
        status = "enriched"
    elif provider_digest or has_source_detail:
        status = "partial"
    else:
        status = "failed"

    hooks = normalize_string_list(digest.get("suggested_hooks"), limit=3)
    post_angles = normalize_string_list(digest.get("possible_post_angles") or digest.get("post_angles"), limit=5)
    key_points = normalize_string_list(
        digest.get("key_points")
        or [
            digest.get("what_happened"),
            digest.get("what_is_new"),
            digest.get("why_it_matters_to_gtm") or digest.get("why_it_matters_to_ai_sales_gtm"),
        ],
        limit=6,
    )
    claims = normalize_string_list(digest.get("important_claims") or page.key_paragraphs, limit=6)
    source_title = clean_text(page.title) or clean_text(article.get("title")) or trend.source_title
    source_author = clean_text(page.author) or clean_text(article.get("author")) or trend.source_author
    source_published_at = normalize_article_date(page.published_at or article.get("date") or trend.date)
    excerpt = truncate_text(page.excerpt or trend.source_excerpt or clean_text(article.get("summary") or article.get("snippet")), SOURCE_EXCERPT_CHARS)
    text_excerpt = truncate_text(page.text_excerpt or trend.source_text_excerpt or "", SOURCE_TEXT_EXCERPT_CHARS) or None
    confidence = clean_text(digest.get("source_confidence")) or source_confidence_for(status, provider_digest, has_source_detail)

    return replace(
        trend,
        summary=clean_text(digest.get("what_happened")) or trend.summary,
        why_it_matters=clean_text(digest.get("why_it_matters")) or trend.why_it_matters,
        leadpoet_angle=positioned_leadpoet_angle(digest.get("leadpoet_angle"), trend=trend, profile=profile),
        suggested_hooks="\n".join(hooks) if hooks else trend.suggested_hooks,
        source_title=source_title,
        source_author=source_author,
        source_published_at=source_published_at,
        source_excerpt=excerpt,
        source_text_excerpt=text_excerpt,
        key_points_json=json.dumps(key_points),
        important_claims_json=json.dumps(claims),
        post_angles_json=json.dumps(post_angles),
        source_confidence=confidence,
        enriched_at=utc_now(),
        enrichment_status=status,
        enrichment_error=enrichment_error,
    )


def positioned_leadpoet_angle(value: object, *, trend: TrendItem, profile: TrendScoutProfile) -> str:
    angle = clean_text(value) or trend.leadpoet_angle
    if leadpoet_angle_needs_positioning_rewrite(angle, profile=profile):
        return default_positioned_leadpoet_angle(profile)
    return angle


def leadpoet_angle_needs_positioning_rewrite(angle: str, *, profile: TrendScoutProfile) -> bool:
    lowered = angle.lower()
    risky_phrases = [
        "leverage insights from",
        "leverage similar",
        "learn from",
        "can leverage",
        "could leverage",
        "inspired by",
        "borrow from",
        "copy from",
        "copy their",
        "copying",
    ]
    if any(phrase in lowered for phrase in risky_phrases):
        return True
    workspace = profile.workspace_name.lower()
    if workspace and workspace not in lowered:
        return False
    return False


def default_positioned_leadpoet_angle(profile: TrendScoutProfile) -> str:
    if profile.workspace_name == "Leadpoet":
        return (
            "Use this source to explain one concrete GTM mechanism, tradeoff, or evaluation question. "
            "Keep the post product-free unless Leadpoet's saved context materially sharpens the lesson."
        )
    return (
        f"Use this source for one concrete reader lesson. Mention {profile.workspace_name} only when the saved "
        "positioning adds necessary specificity, not as a required conclusion."
    )


def trend_source_relevance_score(trend: TrendItem) -> int:
    return web_trend_relevance_score(
        {
            "title": trend.source_title or trend.title,
            "summary": " ".join(
                clean_text(value)
                for value in [
                    trend.summary,
                    trend.source_excerpt,
                    trend.source_text_excerpt,
                    json_list_text(trend.key_points_json),
                    json_list_text(trend.important_claims_json),
                ]
                if clean_text(value)
            ),
            "sourceCommonName": trend.source_author,
            "url": trend.source_url,
        }
    )


def fetch_source_page_text(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "KoinonikoTrendScout/1.0"},
    )
    try:
        with urlopen(request, timeout=SOURCE_FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("content-type", "")
            if content_type and "text/html" not in content_type and "application/xhtml" not in content_type:
                raise RuntimeError(f"unsupported content type: {content_type}")
            return response.read(1_000_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def should_fetch_source_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    return not any(blocked in host for blocked in ["trends.google.", "google.com"])


def extract_source_page_details(source_html: str) -> SourcePageDetails:
    parser = ArticleTextParser()
    parser.feed(source_html)
    parser.close()
    paragraphs = [paragraph for paragraph in unique_texts(parser.paragraphs) if len(paragraph) >= 40]
    text = " ".join(paragraphs)
    key_paragraphs = select_key_paragraphs(paragraphs)
    excerpt = truncate_text(" ".join(key_paragraphs or paragraphs[:2]), SOURCE_EXCERPT_CHARS)
    return SourcePageDetails(
        title=parser.title or meta_value(parser.meta, ["og:title", "twitter:title", "title"]),
        author=meta_value(parser.meta, ["author", "article:author", "byl", "parsely-author"]),
        published_at=meta_value(parser.meta, ["article:published_time", "datePublished", "date", "pubdate", "parsely-pub-date"]),
        excerpt=excerpt,
        text_excerpt=truncate_text(text, SOURCE_TEXT_EXCERPT_CHARS),
        key_paragraphs=key_paragraphs,
        important_numbers=important_numbers(text),
        mentioned_names=mentioned_names(text),
    )


class ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.paragraphs: list[str] = []
        self._capture_title = False
        self._capture_tag = ""
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "meta":
            key = clean_text(attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop"))
            content = clean_text(attr_map.get("content"))
            if key and content:
                self.meta[key] = content
        if self._skip_depth:
            return
        if tag == "title":
            self._capture_title = True
            self._parts = []
        elif tag in {"p", "h1", "h2"}:
            self._capture_tag = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title" and self._capture_title:
            self.title = clean_text(" ".join(self._parts))
            self._capture_title = False
            self._parts = []
            return
        if tag == self._capture_tag:
            text = clean_text(" ".join(self._parts))
            if text:
                self.paragraphs.append(text)
            self._capture_tag = ""
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._capture_title or self._capture_tag:
            self._parts.append(data)


def build_trend_digest_prompt(
    trend: TrendItem,
    article: dict[str, object],
    page: SourcePageDetails,
    *,
    profile: TrendScoutProfile | None = None,
) -> str:
    profile = profile or TrendScoutProfile()
    provider_metadata = {
        "title": trend.title,
        "source_url": trend.source_url,
        "source_name": trend.source_author,
        "source_type": trend.source_type,
        "provider_date": trend.date,
        "provider_snippet": trend.source_excerpt or clean_text(article.get("summary") or article.get("snippet")),
        "trend_breakdown": clean_text(article.get("trend_breakdown_text")),
    }
    source_details = {
        "article_title": page.title,
        "author": page.author,
        "published_at": page.published_at,
        "excerpt": page.excerpt,
        "key_paragraphs": page.key_paragraphs[:5],
        "important_numbers": page.important_numbers[:8],
        "product_company_names": page.mentioned_names[:10],
    }
    text_excerpt = truncate_text(page.text_excerpt or trend.source_text_excerpt or "", SOURCE_PROMPT_TEXT_CHARS)
    return f"""Create a concise Trend Scout digest for {profile.user_name} at {profile.workspace_name}.

First decide whether this source is actually worth surfacing for this workspace.

Saved workspace context:
{profile.focus_summary}

Keep only if it directly affects {profile.keep_guidance}

Reject if it is generic AI news, consumer tech, politics, entertainment, broad market chatter, or if the {profile.workspace_name} angle would need to be forced.

Use source details for understanding, not copying. Do not quote long passages.

Provider metadata:
{json.dumps(provider_metadata, indent=2)}

Extracted source details:
{json.dumps(source_details, indent=2)}

Source text excerpt:
{text_excerpt or "No source page text extracted."}

Return compact JSON only with these keys:
keep, relevance_score, relevance_reason, reject_reason, what_happened, what_is_new, why_it_matters, why_it_matters_to_gtm, leadpoet_angle, suggested_hooks, possible_post_angles, key_points, important_claims, source_confidence, caveats.

Rules:
- relevance_score is 0-10.
- If keep is false or relevance_score is below 6, set leadpoet_angle to an empty string and explain reject_reason.
- Do not invent a {profile.workspace_name} angle. Keep a source only when it has a concrete lesson for the saved audience across workflow, economics, data, buyer behavior, evaluation, team design, or execution.
- In leadpoet_angle, never make another tool, agent, or case study supply {profile.workspace_name}'s saved wedge.
- If another company is mentioned, extract one transferable mechanism, tradeoff, decision rule, or evidence boundary. A workspace/product connection is optional and should be omitted when the source is more useful as standalone market education.
- Do not route every source back to buyer timing, account context, intent signals, or "before copy." Those are available angles, not the default angle.
- Avoid wording like "{profile.workspace_name} can leverage insights from..." or "{profile.workspace_name} can learn from...". State the reader lesson directly.
- possible_post_angles must span different reader payoffs when the source supports them, such as mechanism explanation, diagnostic, decision framework, evidence unpacking, or practical guidance.
- Keep each value specific and concise.
- suggested_hooks and possible_post_angles must be arrays."""


def parse_trend_digest(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def trend_digest_rejects(digest: dict[str, object]) -> bool:
    keep = digest.get("keep")
    if isinstance(keep, bool):
        return not keep
    if isinstance(keep, str) and keep.strip().lower() in {"false", "no", "reject"}:
        return True
    relevance_score = numeric_value(digest.get("relevance_score"))
    return bool(relevance_score and relevance_score < 6)


def fallback_trend_digest(
    trend: TrendItem,
    article: dict[str, object],
    page: SourcePageDetails,
    *,
    profile: TrendScoutProfile,
) -> dict[str, object]:
    what_happened = page.excerpt or trend.summary
    claims = page.key_paragraphs[:4] or [trend.summary]
    numbers = page.important_numbers[:3]
    number_note = f" Notable numbers: {', '.join(numbers)}." if numbers else ""
    return {
        "what_happened": what_happened,
        "what_is_new": clean_text(article.get("trend_breakdown_text")) or trend.title,
        "why_it_matters": f"{trend.why_it_matters}{number_note}",
        "why_it_matters_to_gtm": (
            "Explain the concrete GTM effect: workflow, economics, data quality, buyer behavior, team design, "
            "evaluation criteria, or execution risk."
        ),
        "leadpoet_angle": trend.leadpoet_angle,
        "suggested_hooks": [trend.suggested_hooks] if trend.suggested_hooks else [suggested_hook_for_trend(trend.title, trend.topic)],
        "possible_post_angles": [default_post_angle_for_trend(trend.title, trend.topic, profile)],
        "key_points": normalize_string_list([what_happened, clean_text(article.get("trend_breakdown_text"))], limit=4),
        "important_claims": claims,
        "source_confidence": "medium" if page.text_excerpt else "low",
        "caveats": page.error or "",
    }


def select_key_paragraphs(paragraphs: list[str]) -> list[str]:
    scored: list[tuple[int, str]] = []
    for paragraph in paragraphs:
        lowered = paragraph.lower()
        score = sum(1 for term in TREND_RELEVANCE_TERMS if text_matches_term(lowered, term))
        if important_numbers(paragraph):
            score += 2
        if score:
            scored.append((score, truncate_text(paragraph, 360)))
    selected = [paragraph for _, paragraph in sorted(scored, key=lambda item: item[0], reverse=True)[:5]]
    return selected or [truncate_text(paragraph, 360) for paragraph in paragraphs[:4]]


def important_numbers(text: str) -> list[str]:
    matches = re.findall(
        r"(?:\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|billion|million|thousand|x|users|customers|companies|emails|leads|searches|ARR|revenue)?)",
        text,
        flags=re.I,
    )
    return unique_texts([clean_text(match) for match in matches if clean_text(match)])[:10]


def mentioned_names(text: str) -> list[str]:
    matches = re.findall(r"\b[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,3}\b", text)
    blocked = {"The", "This", "That", "For", "With", "But", "And", "CEO", "AI"}
    return unique_texts([match for match in matches if match not in blocked])[:12]


def meta_value(meta: dict[str, str], keys: list[str]) -> str | None:
    lowered = {key.lower(): value for key, value in meta.items()}
    for key in keys:
        value = clean_text(lowered.get(key.lower()))
        if value:
            return value
    return None


def normalize_string_list(value: object, *, limit: int) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"\n+|;|\s+\d+[.)]\s+", value)
    elif value:
        items = [value]
    else:
        items = []
    return unique_texts([clean_text(item) for item in items if clean_text(item)])[:limit]


def json_list_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return clean_text(value)
    if not isinstance(parsed, list):
        return clean_text(value)
    return " ".join(clean_text(item) for item in parsed if clean_text(item))


def source_confidence_for(status: str, provider_digest: bool, has_source_detail: bool) -> str:
    if status == "enriched" and provider_digest and has_source_detail:
        return "high"
    if provider_digest or has_source_detail:
        return "medium"
    return "low"


def truncate_text(value: object, limit: int) -> str:
    text = html.unescape(clean_text(value))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def empty_refresh_stats() -> dict[str, int]:
    return {
        "candidates_found": 0,
        "trends_stored": 0,
        "enriched": 0,
        "partial": 0,
        "failed": 0,
    }


def web_trend_groups(
    store: SQLiteStore,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_WEB_TRENDS_PER_WINDOW,
) -> WebTrendGroups:
    current = now or datetime.now(timezone.utc)
    trends = store.list_trends(status="unused", limit=250)
    daily = select_trend_group(
        trends,
        source_type=WEB_DAILY_SOURCE_TYPE,
        current=current,
        recent_days=WEB_DAILY_FRESHNESS_DAYS,
        limit=limit,
    )
    weekly = select_trend_group(
        trends,
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        current=current,
        recent_days=WEB_WEEKLY_FRESHNESS_DAYS,
        limit=limit,
    )
    return WebTrendGroups(daily=daily, weekly=weekly)


def select_trend_group(
    trends: list[TrendItem],
    *,
    source_type: str,
    current: datetime,
    recent_days: int,
    limit: int,
) -> list[TrendItem]:
    matching = [trend for trend in trends if trend.source_type == source_type]
    recent = [trend for trend in matching if trend_is_recent(trend, current, days=recent_days)]
    return recent[:limit]


def web_trend_from_article(
    article: dict[str, object],
    *,
    source_type: str,
    profile: TrendScoutProfile | None = None,
) -> TrendItem:
    profile = profile or TrendScoutProfile()
    title = clean_text(article.get("title")) or "Untitled trend"
    url = clean_text(article.get("url") or article.get("link") or article.get("url_mobile")) or None
    source = clean_text(
        article.get("sourceCommonName")
        or article.get("source")
        or article.get("domain")
        or article.get("sourceCountry")
        or "web"
    )
    provider = clean_text(article.get("provider")) or "web"
    topic = classify_topic(title)
    period = "daily" if source_type == WEB_DAILY_SOURCE_TYPE else "weekly"
    date_value = normalize_article_date(article.get("seendate") or article.get("date"))
    summary = clean_text(article.get("summary")) or (
        f"{source} is surfacing this as a top {period} signal across AI, sales, and lead generation."
    )
    return TrendItem(
        id=stable_web_trend_id(source_type, url or title),
        title=title,
        source_url=url,
        source_type=source_type,
        topic=topic,
        summary=summary,
        why_it_matters="This may shift buyer priorities, sales workflows, or the market timing behind AI-led pipeline work.",
        leadpoet_angle=profile.default_angle,
        suggested_hooks=suggested_hook_for_trend(title, topic),
        urgency="high" if source_type == WEB_DAILY_SOURCE_TYPE else "medium",
        date=date_value,
        status="unused",
        engagement_notes=f"Auto-pulled from {provider_label(provider)} as a top {period} web trend.",
        source_author=source,
        raw_text=json.dumps(article, sort_keys=True),
        source_title=title,
        source_published_at=date_value,
        source_excerpt=truncate_text(summary, SOURCE_EXCERPT_CHARS),
        source_text_excerpt=None,
        key_points_json=json.dumps(normalize_string_list([summary, article.get("trend_breakdown_text")], limit=4)),
        important_claims_json=json.dumps([]),
        post_angles_json=json.dumps([default_post_angle_for_trend(title, topic, profile)]),
        source_confidence="low",
        enrichment_status="not_attempted",
    )


def trend_is_recent(trend: TrendItem, now: datetime, *, days: int) -> bool:
    parsed = parse_trend_date(trend.date or trend.updated_at or trend.created_at)
    return parsed is not None and parsed >= now - timedelta(days=days)


def parse_trend_date(value: str | None) -> datetime | None:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in {"just now", "now"}:
        return datetime.now(timezone.utc)
    if lowered == "yesterday":
        return datetime.now(timezone.utc) - timedelta(days=1)
    relative = re.match(r"^(?:about\s+)?(?P<count>\d+|a|an)\s+(?P<unit>minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago$", lowered)
    if relative:
        raw_count = relative.group("count")
        count = 1 if raw_count in {"a", "an"} else int(raw_count)
        unit = relative.group("unit")
        if unit.startswith("minute"):
            return datetime.now(timezone.utc) - timedelta(minutes=count)
        if unit.startswith("hour"):
            return datetime.now(timezone.utc) - timedelta(hours=count)
        if unit.startswith("day"):
            return datetime.now(timezone.utc) - timedelta(days=count)
        if unit.startswith("week"):
            return datetime.now(timezone.utc) - timedelta(weeks=count)
        if unit.startswith("month"):
            return datetime.now(timezone.utc) - timedelta(days=30 * count)
        if unit.startswith("year"):
            return datetime.now(timezone.utc) - timedelta(days=365 * count)
    for candidate in [value, value.replace("Z", "+00:00")]:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def normalize_article_date(value: object | None) -> str:
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return parsed.isoformat()
    clean_value = clean_text(value)
    if clean_value and len(clean_value) >= 14 and clean_value[:14].isdigit():
        parsed = datetime.strptime(clean_value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    if clean_value:
        parsed = parse_trend_date(clean_value)
        if parsed:
            return parsed.isoformat()
    return utc_now()


def stable_web_trend_id(source_type: str, value: str) -> str:
    digest = hashlib.sha256(f"{source_type}:{value}".encode("utf-8")).hexdigest()[:18]
    return f"trend_{source_type}_{digest}"


def classify_topic(title: str) -> str:
    lowered = title.lower()
    if "lead gen" in lowered or "lead generation" in lowered or "pipeline" in lowered:
        return "lead generation"
    if "sales" in lowered or "sdr" in lowered or "gtm" in lowered:
        return "sales AI"
    return "AI"


def suggested_hook_for_topic(topic: str) -> str:
    if topic == "lead generation":
        return "A useful lead generation take should change what an operator does next."
    if topic == "sales AI":
        return "The useful sales AI question is what changed in the actual workflow."
    return "The useful AI signal is the concrete behavior, cost, or constraint that changed."


def suggested_hook_for_trend(title: str, topic: str) -> str:
    title_text = clean_text(title)
    if title_text:
        if topic == "lead generation":
            return f"{title_text}: what changes for the operator on Monday?"
        if topic == "sales AI":
            return f"{title_text}: separate the product claim from the workflow change."
        return f"{title_text}: what changed, what did not, and who should care?"
    return suggested_hook_for_topic(topic)


def default_post_angle_for_trend(title: str, topic: str, profile: TrendScoutProfile) -> str:
    proof = clean_text(title) or clean_text(topic) or "this source"
    if profile.workspace_name == "Leadpoet":
        return (
            f"Use {proof} to teach one concrete GTM mechanism, tradeoff, diagnostic, or decision rule. "
            "Do not force a Leadpoet or buyer-timing conclusion."
        )
    return (
        f"Use {proof} to teach one concrete mechanism or decision rule. Mention {profile.workspace_name} only if "
        "its saved context makes the lesson materially more specific."
    )


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def numeric_value(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = clean_text(value).replace(",", "").replace("+", "").replace("%", "")
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def format_number(value: object) -> str:
    number = numeric_value(value)
    return f"{number:,}" if number else clean_text(value)


def google_trends_url(query: str) -> str:
    return f"https://trends.google.com/trends/explore?{urlencode({'geo': 'US', 'q': query})}"


def provider_label(provider: str) -> str:
    if provider == "scrapingdog_trending_now":
        return "Scrapingdog Google Trends"
    if provider == "scrapingdog_google_news":
        return "Scrapingdog Google News"
    return "Web"


def unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
