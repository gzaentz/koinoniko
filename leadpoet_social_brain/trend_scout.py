from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from leadpoet_social_brain.core.models import TrendItem, utc_now
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SCRAPINGDOG_API_KEY_ENV = "SCRAPINGDOG_API_KEY"
SCRAPINGDOG_API_KEY_ALT_ENV = "SCRAPING_DOG_API_KEY"
SCRAPINGDOG_TRENDS_URL = "https://api.scrapingdog.com/google_trends/trending_now"
SCRAPINGDOG_NEWS_URL = "https://api.scrapingdog.com/google_news"
SCRAPINGDOG_TIMEOUT_SECONDS = 45
WEB_DAILY_SOURCE_TYPE = "web_daily"
WEB_WEEKLY_SOURCE_TYPE = "web_weekly"
WEB_TREND_QUERY = (
    '("artificial intelligence" OR AI OR "AI agents" OR "generative AI") '
    '("sales" OR "lead generation" OR "lead gen" OR GTM OR "sales automation" OR "AI SDR")'
)
SCRAPINGDOG_NEWS_QUERY = (
    '("AI agents" OR "generative AI" OR "artificial intelligence" OR "AI SDR") '
    '("sales" OR "lead generation" OR "lead gen" OR GTM OR "sales automation" OR pipeline)'
)
TREND_RELEVANCE_TERMS = {
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


@dataclass(frozen=True)
class WebTrendGroups:
    daily: list[TrendItem]
    weekly: list[TrendItem]
    errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def refresh_web_trends(
    store: SQLiteStore,
    *,
    fetch_articles: FetchArticles | None = None,
    limit: int = 5,
) -> WebTrendGroups:
    if fetch_articles:
        return refresh_gdelt_web_trends(store, fetch_articles=fetch_articles, limit=limit)

    errors: list[str] = []
    sources: list[str] = []
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
    )
    return WebTrendGroups(daily=daily, weekly=weekly, errors=errors, sources=unique_texts(sources))


def scrapingdog_api_key() -> str:
    return clean_text(os.getenv(SCRAPINGDOG_API_KEY_ENV)) or clean_text(os.getenv(SCRAPINGDOG_API_KEY_ALT_ENV))


def scrapingdog_configured() -> bool:
    return bool(scrapingdog_api_key())


def refresh_gdelt_web_trends(
    store: SQLiteStore,
    *,
    fetch_articles: FetchArticles | None = None,
    limit: int = 5,
) -> WebTrendGroups:
    fetch = fetch_articles or fetch_gdelt_articles
    daily = save_web_trend_articles(
        store,
        articles=fetch(timespan="1d", limit=limit * 4),
        source_type=WEB_DAILY_SOURCE_TYPE,
        limit=limit,
    )
    weekly = save_web_trend_articles(
        store,
        articles=fetch(timespan="7d", limit=limit * 4),
        source_type=WEB_WEEKLY_SOURCE_TYPE,
        limit=limit,
    )
    return WebTrendGroups(daily=daily, weekly=weekly, sources=["GDELT"])


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

    selected = select_web_trend_articles(articles, limit=limit)
    if len(selected) < limit:
        try:
            selected.extend(
                article
                for article in gdelt_articles(fetch_gdelt_articles(timespan=gdelt_timespan, limit=limit * 4))
                if article not in selected
            )
            sources.append("GDELT")
        except Exception as exc:  # noqa: BLE001 - no-key fallback can also throttle.
            errors.append(f"{period} GDELT fallback failed: {exc}")
    return save_web_trend_articles(store, articles=selected, source_type=source_type, limit=limit)


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
        if web_trend_relevance_score(article) <= 0:
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
    text = " ".join(
        clean_text(value)
        for value in [
            article.get("title"),
            article.get("summary"),
            article.get("snippet"),
            article.get("description"),
            article.get("trend_breakdown_text"),
        ]
    ).lower()
    return sum(1 for term in TREND_RELEVANCE_TERMS if text_matches_term(text, term))


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
) -> list[TrendItem]:
    saved: list[TrendItem] = []
    seen: set[str] = set()
    for article in articles:
        title = clean_text(article.get("title"))
        url = clean_text(article.get("url") or article.get("url_mobile"))
        if not title:
            continue
        dedupe_key = (url or title).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        trend = store.save_trend_item(web_trend_from_article(article, source_type=source_type))
        saved.append(trend)
        if len(saved) >= limit:
            break
    return saved


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
