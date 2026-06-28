from __future__ import annotations

import hashlib
import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from leadpoet_social_brain.core.models import TrendItem, utc_now
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SCRAPINGDOG_API_KEY_ENV = "SCRAPINGDOG_API_KEY"
SCRAPINGDOG_API_KEY_ALT_ENV = "SCRAPING_DOG_API_KEY"
SCRAPINGDOG_TRENDS_URL = "https://api.scrapingdog.com/google_trends/trending_now"
SCRAPINGDOG_NEWS_URL = "https://api.scrapingdog.com/google_news"
SCRAPINGDOG_TIMEOUT_SECONDS = 45
SOURCE_FETCH_TIMEOUT_SECONDS = 8
SOURCE_TEXT_EXCERPT_CHARS = 3500
SOURCE_PROMPT_TEXT_CHARS = 5000
SOURCE_EXCERPT_CHARS = 700
WEB_DAILY_SOURCE_TYPE = "web_daily"
WEB_WEEKLY_SOURCE_TYPE = "web_weekly"
WEB_TREND_QUERY = (
    '("AI SDR" OR "sales AI" OR "outbound AI" OR "GTM AI" OR "sales automation" '
    'OR "sales engagement" OR "lead generation" OR "lead gen" OR "buyer intent" '
    'OR "intent data" OR "account signals" OR RevOps OR CRM)'
)
SCRAPINGDOG_NEWS_QUERY = (
    '("AI SDR" OR "sales AI" OR "outbound AI" OR "GTM AI" OR "sales automation" '
    'OR "sales engagement" OR "lead generation" OR "lead gen" OR "buyer intent" '
    'OR "intent data" OR "account signals" OR RevOps OR CRM OR pipeline)'
)
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
    "sales",
    "sales ai",
    "sales automation",
    "sales engagement",
    "sales workflow",
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
    "budget deal": 4,
    "celebrity": 6,
    "consumer app": 5,
    "election": 5,
    "football": 4,
    "gossip": 6,
    "governor": 4,
    "iphone": 4,
    "movie": 5,
    "music": 4,
    "photo app": 5,
    "politics": 5,
    "recipe": 5,
    "senate": 4,
    "sports": 5,
    "stock price": 3,
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
    limit: int = 5,
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
) -> WebTrendGroups:
    if fetch_articles:
        return refresh_gdelt_web_trends(
            store,
            fetch_articles=fetch_articles,
            limit=limit,
            provider=provider,
            enrich=enrich,
            enrich_limit=enrich_limit,
            fetch_source_page=fetch_source_page,
        )

    errors: list[str] = []
    sources: list[str] = []
    stats = empty_refresh_stats()
    api_key = scrapingdog_api_key()
    daily = refresh_web_trends_for_period(
        store,
        period="daily",
        source_type=WEB_DAILY_SOURCE_TYPE,
        scrapingdog_hours=24,
        gdelt_timespan="1d",
        limit=limit,
        scrapingdog_api_key=api_key or None,
        errors=errors,
        sources=sources,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
    )
    weekly = refresh_web_trends_for_period(
        store,
        period="weekly",
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        scrapingdog_hours=168,
        gdelt_timespan="7d",
        limit=limit,
        scrapingdog_api_key=api_key or None,
        errors=errors,
        sources=sources,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
    )
    return WebTrendGroups(daily=daily, weekly=weekly, errors=errors, sources=unique_texts(sources), **stats)


def scrapingdog_api_key() -> str:
    return clean_text(os.getenv(SCRAPINGDOG_API_KEY_ENV)) or clean_text(os.getenv(SCRAPINGDOG_API_KEY_ALT_ENV))


def scrapingdog_configured() -> bool:
    return bool(scrapingdog_api_key())


def refresh_gdelt_web_trends(
    store: SQLiteStore,
    *,
    fetch_articles: FetchArticles | None = None,
    limit: int = 5,
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
) -> WebTrendGroups:
    fetch = fetch_articles or fetch_gdelt_articles
    stats = empty_refresh_stats()
    daily = save_web_trend_articles(
        store,
        articles=fetch(timespan="1d", limit=limit * 4),
        source_type=WEB_DAILY_SOURCE_TYPE,
        limit=limit,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        count_candidates=True,
    )
    weekly = save_web_trend_articles(
        store,
        articles=fetch(timespan="7d", limit=limit * 4),
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        limit=limit,
        stats=stats,
        provider=provider,
        enrich=enrich,
        enrich_limit=enrich_limit,
        fetch_source_page=fetch_source_page,
        count_candidates=True,
    )
    return WebTrendGroups(daily=daily, weekly=weekly, sources=["GDELT"], **stats)


def refresh_web_trends_for_period(
    store: SQLiteStore,
    *,
    period: str,
    source_type: str,
    scrapingdog_hours: int,
    gdelt_timespan: str,
    limit: int,
    scrapingdog_api_key: str | None,
    errors: list[str],
    sources: list[str],
    stats: dict[str, int],
    provider: DraftProvider | None = None,
    enrich: bool = True,
    enrich_limit: int | None = None,
    fetch_source_page: FetchSourcePage | None = None,
) -> list[TrendItem]:
    articles: list[dict[str, object]] = []
    if scrapingdog_api_key:
        try:
            articles.extend(
                scrapingdog_trending_articles(
                    fetch_scrapingdog_trending_now(
                        api_key=scrapingdog_api_key,
                        hours=scrapingdog_hours,
                        limit=50,
                    ),
                    hours=scrapingdog_hours,
                )
            )
            sources.append("Scrapingdog Google Trends")
        except Exception as exc:  # noqa: BLE001 - third-party trend source can fail independently.
            errors.append(f"{period} Scrapingdog Google Trends failed: {exc}")

        if len(select_web_trend_articles(articles, limit=limit)) < limit:
            try:
                articles.extend(
                    scrapingdog_news_articles(
                        fetch_scrapingdog_google_news(
                            api_key=scrapingdog_api_key,
                            tbs="qdr:d" if period == "daily" else "qdr:w",
                            limit=25,
                        )
                    )
                )
                sources.append("Scrapingdog Google News")
            except Exception as exc:  # noqa: BLE001 - third-party trend source can fail independently.
                errors.append(f"{period} Scrapingdog Google News failed: {exc}")

    stats["candidates_found"] += len(articles)
    selected = select_web_trend_articles(articles, limit=limit)
    if len(selected) < limit:
        try:
            fallback_articles = gdelt_articles(fetch_gdelt_articles(timespan=gdelt_timespan, limit=limit * 4))
            stats["candidates_found"] += len(fallback_articles)
            articles.extend(article for article in fallback_articles if article not in articles)
            selected = select_web_trend_articles(articles, limit=limit)
            sources.append("GDELT")
        except Exception as exc:  # noqa: BLE001 - no-key fallback can also throttle.
            errors.append(f"{period} GDELT fallback failed: {exc}")
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
    )


def fetch_scrapingdog_trending_now(
    *,
    api_key: str,
    hours: int,
    limit: int = 50,
    geo: str = "US",
    language: str = "en",
) -> list[dict[str, object]]:
    payload = fetch_json(
        SCRAPINGDOG_TRENDS_URL,
        {
            "api_key": api_key,
            "geo": geo,
            "language": language,
            "hours": str(hours),
        },
        timeout=SCRAPINGDOG_TIMEOUT_SECONDS,
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
) -> list[dict[str, object]]:
    payload = fetch_json(
        SCRAPINGDOG_NEWS_URL,
        {
            "api_key": api_key,
            "query": SCRAPINGDOG_NEWS_QUERY,
            "results": str(limit),
            "country": country,
            "language": language,
            "tbs": tbs,
        },
        timeout=SCRAPINGDOG_TIMEOUT_SECONDS,
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


def fetch_gdelt_articles(*, timespan: str, limit: int = 20) -> list[dict[str, object]]:
    query = urlencode(
        {
            "query": WEB_TREND_QUERY,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(limit),
            "sort": "hybridrel",
            "timespan": timespan,
        }
    )
    request = Request(
        f"{GDELT_DOC_API_URL}?{query}",
        headers={"User-Agent": "KoinonikoTrendScout/1.0"},
    )
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []
    return [article for article in articles if isinstance(article, dict)]


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


def gdelt_articles(articles: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{**article, "provider": "gdelt"} for article in articles]


def select_web_trend_articles(
    articles: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    for article in sorted(articles, key=web_trend_rank_key, reverse=True):
        if web_trend_relevance_score(article) < TREND_RELEVANCE_KEEP_THRESHOLD:
            continue
        title = clean_text(article.get("title"))
        url = clean_text(article.get("url") or article.get("link") or article.get("url_mobile"))
        dedupe_key = (url or title).lower()
        if not title or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def web_trend_rank_key(article: dict[str, object]) -> tuple[int, int, int]:
    return (
        web_trend_relevance_score(article),
        numeric_value(article.get("search_volume")),
        numeric_value(article.get("increase_percentage")),
    )


def web_trend_relevance_score(article: dict[str, object]) -> int:
    text = article_relevance_text(article)
    if not any(text_matches_term(text, term) for term in TREND_REQUIRED_RELEVANCE_TERMS):
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
) -> list[TrendItem]:
    saved: list[TrendItem] = []
    seen: set[str] = set()
    if stats is not None and count_candidates:
        stats["candidates_found"] += len(articles)
    attempted_enrichments = 0
    max_enrichments = limit if enrich_limit is None else max(0, enrich_limit)
    for article in articles:
        title = clean_text(article.get("title"))
        url = clean_text(article.get("url") or article.get("url_mobile"))
        if not title:
            continue
        if web_trend_relevance_score(article) < TREND_RELEVANCE_KEEP_THRESHOLD:
            continue
        dedupe_key = (url or title).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        trend = web_trend_from_article(article, source_type=source_type)
        if enrich and attempted_enrichments < max_enrichments and (provider or fetch_source_page):
            attempted_enrichments += 1
            trend = enrich_trend_source(
                trend,
                article,
                provider=provider,
                fetch_source_page=fetch_source_page or fetch_source_page_text,
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
) -> TrendItem:
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
                build_trend_digest_prompt(trend, article, page),
                variants=1,
            )[0]
            digest = parse_trend_digest(raw_digest)
            provider_digest = bool(digest)
        except Exception as exc:  # noqa: BLE001 - keep partial extraction if the model fails.
            provider_error = f"digest failed: {exc}"
    if not digest:
        digest = fallback_trend_digest(trend, article, page)

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
        leadpoet_angle=clean_text(digest.get("leadpoet_angle")) or trend.leadpoet_angle,
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
        elif tag in {"p", "h1", "h2", "li"}:
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


def build_trend_digest_prompt(trend: TrendItem, article: dict[str, object], page: SourcePageDetails) -> str:
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
    return f"""Create a concise Trend Scout digest for Gavin at Leadpoet.

First decide whether this source is actually worth surfacing for Leadpoet.

Keep only if it directly affects B2B GTM, outbound, lead generation, AI SDRs, sales workflows, buyer intent, account signals, pipeline prioritization, CRM/RevOps, data enrichment, or buyer timing.

Reject if it is generic AI news, consumer tech, politics, entertainment, broad market chatter, or if the Leadpoet angle would need to be forced.

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
- Do not invent a Leadpoet angle. If the source does not connect naturally to buyer timing, proprietary context, account signals, or sales workflow timing, reject it.
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


def fallback_trend_digest(trend: TrendItem, article: dict[str, object], page: SourcePageDetails) -> dict[str, object]:
    what_happened = page.excerpt or trend.summary
    claims = page.key_paragraphs[:4] or [trend.summary]
    numbers = page.important_numbers[:3]
    number_note = f" Notable numbers: {', '.join(numbers)}." if numbers else ""
    return {
        "what_happened": what_happened,
        "what_is_new": clean_text(article.get("trend_breakdown_text")) or trend.title,
        "why_it_matters": f"{trend.why_it_matters}{number_note}",
        "why_it_matters_to_gtm": "This matters for sales and lead generation if it changes buyer timing, prioritization, or the context reps need before outreach.",
        "leadpoet_angle": trend.leadpoet_angle,
        "suggested_hooks": [trend.suggested_hooks] if trend.suggested_hooks else [suggested_hook_for_topic(trend.topic)],
        "possible_post_angles": [trend.leadpoet_angle],
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


def web_trend_groups(store: SQLiteStore, *, now: datetime | None = None, limit: int = 5) -> WebTrendGroups:
    current = now or datetime.now(timezone.utc)
    trends = store.list_trends(status="unused", limit=250)
    daily = [
        trend for trend in trends
        if trend.source_type == WEB_DAILY_SOURCE_TYPE and trend_is_recent(trend, current, days=2)
    ][:limit]
    weekly = [
        trend for trend in trends
        if trend.source_type == WEB_WEEKLY_SOURCE_TYPE and trend_is_recent(trend, current, days=8)
    ][:limit]
    return WebTrendGroups(daily=daily, weekly=weekly)


def web_trend_from_article(article: dict[str, object], *, source_type: str) -> TrendItem:
    title = clean_text(article.get("title")) or "Untitled trend"
    url = clean_text(article.get("url") or article.get("link") or article.get("url_mobile")) or None
    source = clean_text(
        article.get("sourceCommonName")
        or article.get("source")
        or article.get("domain")
        or article.get("sourceCountry")
        or "web"
    )
    provider = clean_text(article.get("provider")) or "gdelt"
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
        leadpoet_angle="Tie the signal back to buyer timing, proprietary context, and why generic sales automation is not enough.",
        suggested_hooks=suggested_hook_for_topic(topic),
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
        post_angles_json=json.dumps([f"Use this to talk about {topic} through the lens of buyer timing."]),
        source_confidence="low",
        enrichment_status="not_attempted",
    )


def trend_is_recent(trend: TrendItem, now: datetime, *, days: int) -> bool:
    parsed = parse_trend_date(trend.date or trend.updated_at or trend.created_at)
    return parsed is not None and parsed >= now - timedelta(days=days)


def parse_trend_date(value: str | None) -> datetime | None:
    if not value:
        return None
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
        return "Lead generation is becoming a timing problem, not just a volume problem."
    if topic == "sales AI":
        return "The sales AI debate is really a context debate."
    return "The AI trend that matters is the one that changes buyer behavior."


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
    return "GDELT"


def unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
