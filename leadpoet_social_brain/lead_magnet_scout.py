from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from leadpoet_social_brain.core.models import LeadMagnetResearchRun, LeadMagnetSource, utc_now
from leadpoet_social_brain.pattern_scout import fetch_scrapingdog_pattern_candidates
from leadpoet_social_brain.platforms.x_api import TWEET_FIELDS, XApiClient
from leadpoet_social_brain.providers import DraftProvider
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


LEAD_MAGNET_FORMATS = {"prompt_library", "playbook", "template_pack", "scorecard"}
LEAD_MAGNET_TERMS = (
    "prompt library",
    "prompt repo",
    "prompt vault",
    "prompt pack",
    "notion template",
    "notion repo",
    "playbook",
    "checklist",
    "swipe file",
    "template pack",
    "scorecard",
    "calculator",
    "workflow guide",
    "field guide",
    "comment to get",
    "comment and i'll send",
    "free template",
    "free guide",
    "download the",
)
SALES_TERMS = (
    "sales",
    "gtm",
    "go-to-market",
    "revops",
    "revenue operations",
    "prospecting",
    "outbound",
    "lead generation",
    "lead gen",
    "pipeline",
    "sdr",
    "account research",
    "buyer intent",
    "sales tech",
    "sales automation",
    "cold email",
)
TRACKING_QUERY_PREFIXES = ("utm_", "mc_", "ref_")
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "ref", "source", "trk", "tracking"}
ResearchCandidateFetcher = Callable[..., list[dict[str, object]]]
X_LEAD_MAGNET_SEARCH_ENDPOINT_ENV = "X_LEAD_MAGNET_SEARCH_ENDPOINT"
X_LEAD_MAGNET_QUERY = (
    '("prompt library" OR "prompt pack" OR "notion template" OR playbook OR checklist OR scorecard) '
    '(sales OR GTM OR RevOps OR outbound OR prospecting OR "lead generation") -is:retweet lang:en'
)


@dataclass(frozen=True)
class LeadMagnetResearchResult:
    run: LeadMagnetResearchRun
    sources: list[LeadMagnetSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates_found: int = 0


def research_lead_magnets(
    store: SQLiteStore,
    provider: DraftProvider | None = None,
    *,
    candidates: list[dict[str, object]] | None = None,
    fetch_candidates: ResearchCandidateFetcher | None = None,
    window_days: int = 30,
    limit: int = 20,
    query: str | None = None,
    now: str | datetime | None = None,
    period_key: str | None = None,
    initial_warnings: list[str] | None = None,
) -> LeadMagnetResearchResult:
    """Collect and rank recent sales/tech lead magnets without inventing performance.

    Research stays deterministic so a web refresh can finish within its request
    deadline. AI synthesis happens later when a lead magnet is generated.
    """

    current = parse_datetime(now) or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    window_days = max(1, min(int(window_days), 90))
    cutoff = current - timedelta(days=window_days)
    started_at = utc_now()
    manual_key_seed = f"{query or ''}:{started_at}"
    run_key = period_key or (
        f"manual:{current.date().isoformat()}:"
        f"{hashlib.sha256(manual_key_seed.encode()).hexdigest()[:12]}"
    )
    run = LeadMagnetResearchRun(
        window_start=cutoff.replace(microsecond=0).isoformat(),
        window_end=current.replace(microsecond=0).isoformat(),
        period_key=run_key,
        query=query or "sales and sales-tech lead magnets",
        provider=provider.__class__.__name__ if provider else "deterministic",
        status="running",
        started_at=started_at,
    )
    run = store.save_lead_magnet_research_run(run)
    warnings: list[str] = list(initial_warnings or [])
    discovered: list[dict[str, object]] = []
    try:
        if candidates is not None:
            discovered = list(candidates)
        else:
            discovered.extend(stored_pattern_candidates(store))
            try:
                if fetch_candidates is None:
                    discovered.extend(
                        default_lead_magnet_candidate_fetcher(
                            limit=max(limit * 2, 20),
                            query=query,
                            warnings=warnings,
                        )
                    )
                else:
                    discovered.extend(fetch_candidates(limit=max(limit * 2, 20), query=query))
            except Exception as exc:  # noqa: BLE001 - stored/manual evidence remains usable.
                warnings.append(f"Live lead-magnet discovery was unavailable: {exc}")

        saved: list[LeadMagnetSource] = []
        seen: set[str] = set()
        skipped_missing_date = 0
        skipped_irrelevant = 0
        for candidate in discovered:
            canonical = canonicalize_url(clean_text(candidate.get("source_url") or candidate.get("url") or candidate.get("link")))
            fingerprint = canonical or candidate_fingerprint(candidate)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            published = candidate_published_at(candidate, now=current)
            published_dt = parse_datetime(published)
            if published_dt is None:
                skipped_missing_date += 1
                continue
            if published_dt < cutoff or published_dt > current + timedelta(days=1):
                continue
            text = candidate_text(candidate)
            if not is_sales_lead_magnet(text):
                skipped_irrelevant += 1
                continue
            source = source_from_candidate(
                candidate,
                classification={},
                canonical_url=canonical,
                published_at=published_dt.replace(microsecond=0).isoformat(),
                now=current,
                workspace_id=store.workspace_id,
            )
            saved.append(store.save_lead_magnet_source(source))

        saved.sort(key=lambda item: (item.performance_score, item.published_at or ""), reverse=True)
        saved = saved[: max(1, limit)]
        metric_backed = sum(1 for item in saved if item.evidence_strength == "metric_backed")
        if skipped_missing_date:
            warnings.append(
                f"Skipped {skipped_missing_date} candidate(s) without a verifiable publish date; they cannot prove the {window_days}-day window."
            )
        if skipped_irrelevant:
            warnings.append(f"Skipped {skipped_irrelevant} candidate(s) that were not both sales-related and lead-magnet-like.")
        if saved and not metric_backed:
            warnings.append(
                "No source had observed engagement metrics. Results are qualitative inspiration, not proof of high performance."
            )
        elif metric_backed < min(3, len(saved)):
            warnings.append(
                f"Only {metric_backed} source(s) had observed engagement metrics; treat cross-source conclusions as directional."
            )
        completed = LeadMagnetResearchRun(
            **{
                **run.__dict__,
                "status": "partial" if warnings else "completed",
                "candidates_found": len(discovered),
                "sources_qualified": len(saved),
                "source_ids": [source.id for source in saved],
                "warnings": warnings,
                "completed_at": utc_now(),
            }
        )
        completed = store.save_lead_magnet_research_run(completed)
        return LeadMagnetResearchResult(
            run=completed,
            sources=saved,
            warnings=warnings,
            candidates_found=len(discovered),
        )
    except Exception as exc:
        failed = LeadMagnetResearchRun(
            **{
                **run.__dict__,
                "status": "failed",
                "candidates_found": len(discovered),
                "warnings": warnings,
                "error_message": str(exc),
                "completed_at": utc_now(),
            }
        )
        store.save_lead_magnet_research_run(failed)
        raise


def default_lead_magnet_candidate_fetcher(
    *,
    limit: int = 20,
    query: str | None = None,
    warnings: list[str] | None = None,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    errors: list[str] = []
    if os.getenv("X_BEARER_TOKEN"):
        try:
            candidates.extend(fetch_x_lead_magnet_candidates(limit=limit, query=query))
        except Exception as exc:  # noqa: BLE001 - qualitative discovery can still run.
            errors.append(f"X lead-magnet search failed: {exc}")
    if len(candidates) < limit:
        try:
            # ScrapingDog is a qualitative fallback only when X did not provide
            # enough candidates. Avoid paying its multi-query latency on the
            # successful X path.
            candidates.extend(
                fetch_scrapingdog_pattern_candidates(
                    limit=limit - len(candidates),
                    window="monthly",
                    context_terms=list(SALES_TERMS[:8]),
                )
            )
        except Exception as exc:  # noqa: BLE001 - X or stored evidence may still be usable.
            errors.append(str(exc))
    if not candidates and errors:
        raise ValueError("; ".join(unique_texts(errors)))
    if errors and warnings is not None:
        warnings.extend(unique_texts(errors))
    if query:
        query_terms = set(clean_text(query).lower().split())
        candidates.sort(key=lambda row: len(query_terms & set(candidate_text(row).lower().split())), reverse=True)
    return candidates[:limit]


def fetch_x_lead_magnet_candidates(
    *,
    limit: int = 20,
    query: str | None = None,
    client: XApiClient | None = None,
    now: str | datetime | None = None,
    window_days: int = 30,
    endpoint: str | None = None,
) -> list[dict[str, object]]:
    """Search X for metric-backed lead magnets when the workspace has search access.

    `recent` search covers the most recent seven days, which is a valid subset of
    the rolling 30-day analysis. Set `X_LEAD_MAGNET_SEARCH_ENDPOINT=all` when the
    X project has full-archive access so the whole window is queried.
    """

    current = (parse_datetime(now) or datetime.now(timezone.utc)).astimezone(timezone.utc)
    search_mode = clean_text(endpoint or os.getenv(X_LEAD_MAGNET_SEARCH_ENDPOINT_ENV) or "recent").lower()
    if search_mode not in {"recent", "all"}:
        raise ValueError(f"{X_LEAD_MAGNET_SEARCH_ENDPOINT_ENV} must be 'recent' or 'all'.")
    x_client = client or XApiClient.from_env()
    requested_start = current - timedelta(days=max(1, min(int(window_days), 30)))
    # X rejects search requests whose end_time is less than ten seconds behind
    # the request. Keep a larger buffer for both endpoints so clock skew and
    # request latency cannot turn an otherwise valid refresh into a 400.
    search_end = current - timedelta(seconds=30)
    start = requested_start if search_mode == "all" else max(requested_start, search_end - timedelta(days=6, hours=23))
    search_query = clean_text(query) or X_LEAD_MAGNET_QUERY
    if query and "-is:retweet" not in search_query:
        search_query = f"({search_query}) -is:retweet lang:en"
    path = "/tweets/search/all" if search_mode == "all" else "/tweets/search/recent"
    candidates: list[dict[str, object]] = []
    next_token: str | None = None
    while len(candidates) < max(1, limit):
        remaining = max(1, limit - len(candidates))
        params = {
            "query": search_query,
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": "id,name,username,public_metrics",
            "max_results": str(min(100, max(10, remaining))),
            "start_time": start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "end_time": search_end.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        if next_token:
            params["next_token"] = next_token
        response = x_client.get_json(path, params)
        users = {
            str(user.get("id")): user
            for user in (response.get("includes") or {}).get("users", [])
            if isinstance(user, dict) and user.get("id")
        }
        tweets = [tweet for tweet in response.get("data") or [] if isinstance(tweet, dict)]
        for tweet in tweets:
            metrics = tweet.get("public_metrics") if isinstance(tweet.get("public_metrics"), dict) else {}
            author = users.get(str(tweet.get("author_id") or ""), {})
            author_metrics = author.get("public_metrics") if isinstance(author.get("public_metrics"), dict) else {}
            tweet_id = clean_text(tweet.get("id"))
            username = clean_text(author.get("username"))
            text = clean_text(tweet.get("text"))
            if not tweet_id or not is_sales_lead_magnet(text):
                continue
            candidates.append(
                {
                    "title": summarize_candidate_title(text),
                    "source_url": f"https://x.com/{username or 'i/web'}/status/{tweet_id}",
                    "platform": "x",
                    "author": username or clean_optional(author.get("name")),
                    "published_at": tweet.get("created_at"),
                    "source_text": text,
                    "likes": optional_int(metrics.get("like_count")),
                    "replies": optional_int(metrics.get("reply_count")),
                    "reposts": optional_int(metrics.get("retweet_count")),
                    "bookmarks": optional_int(metrics.get("bookmark_count")),
                    "impressions": optional_int(metrics.get("impression_count")),
                    "follower_count": optional_int(author_metrics.get("followers_count")),
                    "metric_observed_at": current.replace(microsecond=0).isoformat(),
                    "raw_text": json.dumps({"tweet": tweet, "author": author}, sort_keys=True, default=str),
                }
            )
            if len(candidates) >= limit:
                break
        next_token = clean_optional((response.get("meta") or {}).get("next_token"))
        if not tweets or not next_token:
            break
    return candidates[:limit]


def stored_pattern_candidates(store: SQLiteStore) -> list[dict[str, object]]:
    patterns = [
        *store.list_viral_patterns(status="saved", limit=100),
        *store.list_viral_patterns(status="active", limit=100),
    ]
    candidates: list[dict[str, object]] = []
    for pattern in patterns:
        text = " ".join(
            value
            for value in [pattern.source_text, pattern.source_summary, pattern.pattern_name, pattern.structure]
            if value
        )
        if not is_sales_lead_magnet(text):
            continue
        candidates.append(
            {
                "source_url": pattern.source_url,
                "platform": pattern.platform,
                "author": pattern.source_author,
                "title": pattern.pattern_name or pattern.source_summary,
                "source_text": pattern.source_text,
                "source_summary": pattern.source_summary,
                "published_at": pattern.collected_at,
                "format": infer_format(text),
                "why_it_worked": pattern.why_it_worked,
                "likes": pattern.likes,
                "replies": pattern.replies,
                "reposts": pattern.reposts,
                "bookmarks": pattern.bookmarks,
                "impressions": pattern.impressions,
                "metric_observed_at": pattern.collected_at,
                "raw_text": pattern.raw_text,
            }
        )
    return candidates


def source_from_candidate(
    candidate: dict[str, object],
    *,
    classification: dict[str, object],
    canonical_url: str,
    published_at: str,
    now: datetime,
    workspace_id: str,
) -> LeadMagnetSource:
    text = candidate_text(candidate)
    likes = optional_int(candidate.get("likes"))
    replies = optional_int(candidate.get("replies") or candidate.get("comments"))
    reposts = optional_int(candidate.get("reposts") or candidate.get("shares"))
    bookmarks = optional_int(candidate.get("bookmarks") or candidate.get("saves"))
    impressions = optional_int(candidate.get("impressions") or candidate.get("views"))
    follower_count = optional_int(candidate.get("follower_count") or candidate.get("followers"))
    score, evidence = performance_score(
        likes=likes,
        replies=replies,
        reposts=reposts,
        bookmarks=bookmarks,
        impressions=impressions,
        follower_count=follower_count,
        published_at=published_at,
        now=now,
    )
    title = clean_text(candidate.get("title") or candidate.get("source_summary") or classification.get("title"))
    return LeadMagnetSource(
        id=stable_source_id(f"{workspace_id}:{canonical_url or candidate_fingerprint(candidate)}"),
        title=title or "Observed lead magnet",
        source_url=clean_optional(candidate.get("source_url") or candidate.get("url") or candidate.get("link")),
        canonical_url=canonical_url or None,
        platform=clean_text(candidate.get("platform")) or platform_for_url(canonical_url),
        author=clean_optional(candidate.get("author") or candidate.get("source_author") or candidate.get("source")),
        published_at=published_at,
        collected_at=utc_now(),
        format=normalize_format(classification.get("format") or candidate.get("format") or infer_format(text)),
        sales_topic=clean_optional(classification.get("sales_topic") or candidate.get("sales_topic") or infer_sales_topic(text)),
        hook=clean_optional(classification.get("hook") or candidate.get("hook")),
        promise=clean_optional(classification.get("promise") or candidate.get("promise") or title),
        audience=clean_optional(classification.get("audience") or candidate.get("audience")),
        delivery_mechanism=clean_optional(
            classification.get("delivery_mechanism") or candidate.get("delivery_mechanism") or infer_delivery(text)
        ),
        cta=clean_optional(classification.get("cta") or candidate.get("cta")),
        leadpoet_integration=clean_optional(classification.get("product_placement") or candidate.get("leadpoet_integration")),
        why_it_worked=clean_optional(classification.get("why_it_worked") or candidate.get("why_it_worked")),
        likes=likes,
        replies=replies,
        reposts=reposts,
        bookmarks=bookmarks,
        impressions=impressions,
        follower_count=follower_count,
        metric_observed_at=clean_optional(candidate.get("metric_observed_at")) or (utc_now() if evidence == "metric_backed" else None),
        performance_score=score,
        evidence_strength=evidence,
        raw_text=clean_optional(candidate.get("raw_text")) or json.dumps(candidate, sort_keys=True, default=str),
        status="active",
    )


def performance_score(
    *,
    likes: int | None,
    replies: int | None,
    reposts: int | None,
    bookmarks: int | None,
    impressions: int | None,
    follower_count: int | None,
    published_at: str,
    now: datetime,
) -> tuple[float, str]:
    metrics = [likes, replies, reposts, bookmarks, impressions]
    if not any(value is not None for value in metrics):
        return 0.0, "qualitative"
    engagements = (likes or 0) + (replies or 0) * 3 + (reposts or 0) * 2 + (bookmarks or 0) * 4
    reach = impressions or follower_count or 0
    normalized = engagements / reach if reach > 0 else 0.0
    age_days = max(0.0, (now - (parse_datetime(published_at) or now)).total_seconds() / 86400)
    recency = max(0.25, 1.0 - age_days / 45.0)
    score = (math.log1p(max(0, engagements)) * 10.0 + min(normalized, 1.0) * 100.0) * recency
    return round(score, 3), "metric_backed"


def is_sales_lead_magnet(text: str) -> bool:
    lowered = clean_text(text).lower()
    return any(term in lowered for term in LEAD_MAGNET_TERMS) and any(term in lowered for term in SALES_TERMS)


def canonicalize_url(value: str) -> str:
    if not value:
        return ""
    try:
        split = urlsplit(value)
    except ValueError:
        return value.strip()
    query = [
        (key, val)
        for key, val in parse_qsl(split.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith(TRACKING_QUERY_PREFIXES)
    ]
    host = split.netloc.lower().removeprefix("www.")
    path = split.path.rstrip("/") or "/"
    return urlunsplit((split.scheme.lower() or "https", host, path, urlencode(sorted(query)), ""))


def candidate_published_at(candidate: dict[str, object], *, now: datetime) -> str | None:
    raw_candidate = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    raw = clean_text(
        candidate.get("published_at")
        or candidate.get("source_published_at")
        or candidate.get("date")
        or raw_candidate.get("published_at")
        or raw_candidate.get("date")
        or raw_candidate.get("time")
    )
    parsed = parse_datetime(raw)
    if parsed:
        return parsed.isoformat()
    match = re.fullmatch(r"(\d+)\s+(minute|hour|day|week)s?\s+ago", raw.lower())
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2)
    delta = {
        "minute": timedelta(minutes=count),
        "hour": timedelta(hours=count),
        "day": timedelta(days=count),
        "week": timedelta(weeks=count),
    }[unit]
    return (now - delta).replace(microsecond=0).isoformat()


def parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    cleaned = clean_text(value)
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def candidate_text(candidate: dict[str, object]) -> str:
    return " ".join(
        clean_text(candidate.get(key))
        for key in ("title", "source_summary", "summary", "source_text", "text", "snippet", "description")
        if clean_text(candidate.get(key))
    )


def summarize_candidate_title(text: str, *, max_length: int = 110) -> str:
    cleaned = clean_text(text)
    first_line = re.split(r"[\r\n]+", cleaned, maxsplit=1)[0]
    if len(first_line) <= max_length:
        return first_line
    return first_line[: max_length - 3].rstrip() + "..."


def infer_format(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ("scorecard", "calculator", "assessment", "diagnostic")):
        return "scorecard"
    if any(term in lowered for term in ("prompt", "swipe file")):
        return "prompt_library"
    if any(term in lowered for term in ("template", "checklist")):
        return "template_pack"
    return "playbook"


def normalize_format(value: object) -> str:
    cleaned = clean_text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {"prompt_repo": "prompt_library", "prompt_vault": "prompt_library", "checklist": "template_pack"}
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned if cleaned in LEAD_MAGNET_FORMATS else "playbook"


def infer_sales_topic(text: str) -> str:
    lowered = text.lower()
    for term in SALES_TERMS:
        if term in lowered:
            return term
    return "sales"


def infer_delivery(text: str) -> str:
    lowered = text.lower()
    if "comment" in lowered and ("send" in lowered or "get" in lowered):
        return "comment-to-receive"
    if "notion" in lowered:
        return "notion"
    if "download" in lowered:
        return "download"
    return "public-resource"


def platform_for_url(value: str) -> str:
    host = urlsplit(value).netloc.lower() if value else ""
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return "x"
    return "web"


def stable_source_id(value: str) -> str:
    return f"leadsrc_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def candidate_fingerprint(candidate: dict[str, object]) -> str:
    return hashlib.sha256(candidate_text(candidate).lower().encode("utf-8")).hexdigest()


def parse_json_object(raw: str) -> dict[str, object] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
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


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clean_optional(value: object) -> str | None:
    value = clean_text(value)
    return value or None


def optional_int(value: object) -> int | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return max(0, int(float(cleaned)))
    except ValueError:
        return None


def unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique.append(cleaned)
    return unique
