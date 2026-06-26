from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from leadpoet_social_brain.core.models import TrendItem, utc_now
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
WEB_DAILY_SOURCE_TYPE = "web_daily"
WEB_WEEKLY_SOURCE_TYPE = "web_weekly"
WEB_TREND_QUERY = (
    '("artificial intelligence" OR AI OR "AI agents" OR "generative AI") '
    '("sales" OR "lead generation" OR "lead gen" OR GTM OR "sales automation" OR "AI SDR")'
)
FetchArticles = Callable[..., list[dict[str, object]]]


@dataclass(frozen=True)
class WebTrendGroups:
    daily: list[TrendItem]
    weekly: list[TrendItem]


def refresh_web_trends(
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
    return WebTrendGroups(daily=daily, weekly=weekly)


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
    url = clean_text(article.get("url") or article.get("url_mobile")) or None
    source = clean_text(
        article.get("sourceCommonName")
        or article.get("domain")
        or article.get("sourceCountry")
        or "web"
    )
    topic = classify_topic(title)
    period = "daily" if source_type == WEB_DAILY_SOURCE_TYPE else "weekly"
    date_value = normalize_article_date(clean_text(article.get("seendate") or article.get("date")))
    return TrendItem(
        id=stable_web_trend_id(source_type, url or title),
        title=title,
        source_url=url,
        source_type=source_type,
        topic=topic,
        summary=f"{source} is surfacing this as a top {period} signal across AI, sales, and lead generation.",
        why_it_matters="This may shift buyer priorities, sales workflows, or the market timing behind AI-led pipeline work.",
        leadpoet_angle="Tie the signal back to buyer timing, proprietary context, and why generic sales automation is not enough.",
        suggested_hooks=suggested_hook_for_topic(topic),
        urgency="high" if source_type == WEB_DAILY_SOURCE_TYPE else "medium",
        date=date_value,
        status="unused",
        engagement_notes=f"Auto-pulled from GDELT as a top {period} web trend.",
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


def normalize_article_date(value: str | None) -> str:
    if value and len(value) >= 14 and value[:14].isdigit():
        parsed = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    if value:
        parsed = parse_trend_date(value)
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
