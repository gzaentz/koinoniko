from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import base64
import hashlib
import hmac
import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from leadpoet_social_brain.core.models import EngagementMetrics, SocialPost, utc_now
from leadpoet_social_brain.ingestion import ingest_x_post
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


TWEET_FIELDS = ",".join(
    [
        "id",
        "text",
        "created_at",
        "public_metrics",
        "author_id",
        "conversation_id",
        "referenced_tweets",
        "entities",
        "lang",
    ]
)

MISSING_X_WRITE_CREDENTIALS_MESSAGE = (
    "No X write credentials configured. Set X write auth credentials before posting."
)


@dataclass(frozen=True)
class NormalizedXApiPost:
    x_post_id: str
    text: str
    url: str
    date: str | None
    author: str
    topic: str
    post_type: str
    metrics: EngagementMetrics | None = None
    imitate: bool = False


@dataclass(frozen=True)
class XApiSyncResult:
    fetched: int = 0
    imported: int = 0
    would_import: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    topics: Counter[str] = field(default_factory=Counter)
    post_types: Counter[str] = field(default_factory=Counter)
    dry_run_posts: list[NormalizedXApiPost] = field(default_factory=list)


@dataclass(frozen=True)
class XMetricsMover:
    post_id: str
    url: str | None
    total_engagement_delta: int
    impression_delta: int | None


@dataclass(frozen=True)
class XMetricsSyncResult:
    checked: int = 0
    updated: int = 0
    would_update: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    top_movers: list[XMetricsMover] = field(default_factory=list)


@dataclass(frozen=True)
class XPostedTweet:
    id: str
    text: str
    url: str


class XApiClient:
    def __init__(
        self,
        bearer_token: str,
        *,
        base_url: str = "https://api.twitter.com/2",
        request_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ):
        if not bearer_token:
            raise ValueError("X_BEARER_TOKEN is required for X API access.")
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")
        self._request_json = request_json

    @classmethod
    def from_env(cls) -> "XApiClient":
        return cls(os.getenv("X_BEARER_TOKEN", ""))

    def resolve_user_id(self, username: str) -> str:
        clean_username = normalize_username(username)
        response = self.get_json(
            f"/users/by/username/{urllib.parse.quote(clean_username)}",
            {"user.fields": "id,username,name"},
        )
        user_id = str((response.get("data") or {}).get("id") or "").strip()
        if not user_id:
            raise ValueError(f"Could not resolve X username '{clean_username}'.")
        return user_id

    def iter_user_posts(
        self,
        user_id: str,
        *,
        limit: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        exclude_replies: bool = True,
        exclude_retweets: bool = True,
    ) -> Iterable[dict[str, Any]]:
        yielded = 0
        next_token: str | None = None
        while yielded < limit:
            remaining = limit - yielded
            params = {
                "tweet.fields": TWEET_FIELDS,
                "max_results": str(min(100, max(5, remaining))),
            }
            excluded = []
            if exclude_replies:
                excluded.append("replies")
            if exclude_retweets:
                excluded.append("retweets")
            if excluded:
                params["exclude"] = ",".join(excluded)
            if start_time:
                params["start_time"] = start_time
            if end_time:
                params["end_time"] = end_time
            if next_token:
                params["pagination_token"] = next_token

            response = self.get_json(f"/users/{urllib.parse.quote(str(user_id))}/tweets", params)
            tweets = response.get("data") or []
            for tweet in tweets:
                if yielded >= limit:
                    break
                yielded += 1
                yield tweet
            next_token = str((response.get("meta") or {}).get("next_token") or "").strip() or None
            if not next_token or not tweets:
                break

    def fetch_tweets_by_ids(self, tweet_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not tweet_ids:
            return {}
        response = self.get_json(
            "/tweets",
            {
                "ids": ",".join(tweet_ids),
                "tweet.fields": "id,public_metrics",
            },
        )
        return {
            str(tweet.get("id")): tweet
            for tweet in response.get("data") or []
            if tweet.get("id")
        }

    def get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self._request_json:
            return self._request_json(path, params)
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "leadpoet-social-brain/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"X API request failed ({exc.code}): {body}") from exc


class XWriteClient:
    def __init__(
        self,
        *,
        oauth2_access_token: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
        base_url: str = "https://api.twitter.com/2",
        request_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ):
        self.oauth2_access_token = oauth2_access_token
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.base_url = base_url.rstrip("/")
        self._request_json = request_json
        if not self.oauth2_access_token and not self._has_oauth1_credentials():
            raise ValueError(MISSING_X_WRITE_CREDENTIALS_MESSAGE)

    @classmethod
    def from_env(cls) -> "XWriteClient":
        return cls(
            oauth2_access_token=os.getenv("X_OAUTH2_ACCESS_TOKEN") or None,
            api_key=os.getenv("X_API_KEY") or None,
            api_secret=os.getenv("X_API_SECRET") or None,
            access_token=os.getenv("X_ACCESS_TOKEN") or None,
            access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET") or None,
        )

    def post_tweet(self, text: str) -> XPostedTweet:
        response = self.post_json("/tweets", {"text": text})
        data = response.get("data") or {}
        tweet_id = str(data.get("id") or "").strip()
        if not tweet_id:
            raise RuntimeError(f"X API post response did not include a tweet id: {response}")
        posted_text = str(data.get("text") or text)
        return XPostedTweet(
            id=tweet_id,
            text=posted_text,
            url=f"https://x.com/i/web/status/{tweet_id}",
        )

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._request_json:
            return self._request_json(path, payload, self._headers(path))
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                **self._headers(path),
                "Content-Type": "application/json",
                "User-Agent": "leadpoet-social-brain/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"X API post failed ({exc.code}): {body_text}") from exc

    def _headers(self, path: str) -> dict[str, str]:
        if self.oauth2_access_token:
            return {"Authorization": f"Bearer {self.oauth2_access_token}"}
        return {"Authorization": self._oauth1_header("POST", f"{self.base_url}{path}")}

    def _has_oauth1_credentials(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_token_secret])

    def _oauth1_header(self, method: str, url: str) -> str:
        oauth_params = {
            "oauth_consumer_key": self.api_key or "",
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token or "",
            "oauth_version": "1.0",
        }
        signature_base = "&".join(
            [
                method.upper(),
                _oauth_quote(url),
                _oauth_quote(_normalized_oauth_params(oauth_params)),
            ]
        )
        signing_key = f"{_oauth_quote(self.api_secret or '')}&{_oauth_quote(self.access_token_secret or '')}"
        signature = base64.b64encode(
            hmac.new(signing_key.encode("utf-8"), signature_base.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        oauth_params["oauth_signature"] = signature
        header_params = ", ".join(
            f'{_oauth_quote(key)}="{_oauth_quote(value)}"'
            for key, value in sorted(oauth_params.items())
        )
        return f"OAuth {header_params}"


def sync_x_user_posts(
    store: SQLiteStore,
    client: XApiClient,
    *,
    username: str,
    limit: int,
    default_topic: str,
    default_post_type: str,
    start_time: str | None = None,
    end_time: str | None = None,
    exclude_replies: bool = True,
    exclude_retweets: bool = True,
    dry_run: bool = False,
    imitate: bool = False,
) -> XApiSyncResult:
    user_id = client.resolve_user_id(username)
    fetched = 0
    imported = 0
    would_import = 0
    skipped = 0
    duplicates = 0
    errors: list[str] = []
    topics: Counter[str] = Counter()
    post_types: Counter[str] = Counter()
    dry_run_posts: list[NormalizedXApiPost] = []

    for tweet in client.iter_user_posts(
        user_id,
        limit=limit,
        start_time=start_time,
        end_time=end_time,
        exclude_replies=exclude_replies,
        exclude_retweets=exclude_retweets,
    ):
        fetched += 1
        try:
            if should_skip_referenced_tweet(
                tweet,
                exclude_replies=exclude_replies,
                exclude_retweets=exclude_retweets,
            ):
                skipped += 1
                continue
            normalized = normalize_x_api_post(
                tweet,
                username=username,
                default_topic=default_topic,
                default_post_type=default_post_type,
                imitate=imitate,
            )
            if is_duplicate_x_api_post(store, normalized):
                duplicates += 1
                skipped += 1
                continue
            if dry_run:
                would_import += 1
                dry_run_posts.append(normalized)
                topics[normalized.topic] += 1
                post_types[normalized.post_type] += 1
                continue
            post = ingest_x_post(
                store,
                text=normalized.text,
                url=normalized.url,
                date=normalized.date,
                author=normalized.author,
                topic=normalized.topic,
                post_type=normalized.post_type,
                metrics=normalized.metrics,
                imitate=normalized.imitate,
            )
            imported += 1
            topics[post.topic] += 1
            post_types[post.post_type] += 1
        except Exception as exc:  # noqa: BLE001 - sync should collect per-post errors.
            skipped += 1
            tweet_id = str(tweet.get("id") or "unknown")
            errors.append(f"tweet {tweet_id}: {exc}")

    return XApiSyncResult(
        fetched=fetched,
        imported=imported,
        would_import=would_import,
        skipped=skipped,
        duplicates=duplicates,
        errors=errors,
        topics=topics,
        post_types=post_types,
        dry_run_posts=dry_run_posts,
    )


def sync_x_metrics(
    store: SQLiteStore,
    client: XApiClient,
    *,
    post_id: str | None = None,
    x_post_id: str | None = None,
    since_days: int | None = None,
    limit: int = 100,
    dry_run: bool = False,
) -> XMetricsSyncResult:
    candidates, unresolved = x_metric_candidates(
        store,
        post_id=post_id,
        x_post_id=x_post_id,
        since_days=since_days,
        limit=limit,
    )
    checked = len(candidates) + unresolved
    updated = 0
    would_update = 0
    skipped = unresolved
    errors: list[str] = []
    movers: list[XMetricsMover] = []
    latest_before = store.latest_metrics_by_post_id([post.id for post, _ in candidates])

    for batch in _chunks(candidates, 100):
        ids = [tweet_id for _, tweet_id in batch]
        try:
            tweets_by_id = client.fetch_tweets_by_ids(ids)
        except Exception as exc:  # noqa: BLE001 - surface one error per failed batch.
            skipped += len(batch)
            errors.append(f"batch {','.join(ids)}: {exc}")
            continue
        collected_at = utc_now()
        for post, tweet_id in batch:
            tweet = tweets_by_id.get(tweet_id)
            if not tweet:
                skipped += 1
                errors.append(f"post {post.id}: no X API metrics returned for tweet {tweet_id}")
                continue
            metrics = metrics_from_x_public_metrics(tweet.get("public_metrics") or {}, measured_at=collected_at)
            if not metrics:
                skipped += 1
                errors.append(f"post {post.id}: X API metrics missing for tweet {tweet_id}")
                continue
            previous = latest_before.get(post.id)
            mover = metric_mover(post, metrics, previous)
            if mover:
                movers.append(mover)
            if dry_run:
                would_update += 1
                continue
            store.add_metrics(post.id, metrics)
            updated += 1

    return XMetricsSyncResult(
        checked=checked,
        updated=updated,
        would_update=would_update,
        skipped=skipped,
        errors=errors,
        top_movers=sorted(
            movers,
            key=lambda mover: mover.total_engagement_delta,
            reverse=True,
        )[:5],
    )


def x_metric_candidates(
    store: SQLiteStore,
    *,
    post_id: str | None = None,
    x_post_id: str | None = None,
    since_days: int | None = None,
    limit: int = 100,
) -> tuple[list[tuple[SocialPost, str]], int]:
    posts: list[SocialPost] = []
    if post_id:
        post = store.get_post(post_id)
        if post:
            posts = [post]
    elif x_post_id:
        post = store.get_post_by_x_post_id(x_post_id)
        if post:
            posts = [post]
    else:
        posts = store.list_posts(platform="x", limit=limit)
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days is not None else None
    candidates: list[tuple[SocialPost, str]] = []
    seen: set[str] = set()
    unresolved = 0
    for post in posts:
        if post.platform != "x":
            continue
        if cutoff and not _post_is_recent_enough(post, cutoff):
            continue
        tweet_id = x_post_id or parse_x_post_id(post.url or "")
        if not tweet_id:
            unresolved += 1
            continue
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        candidates.append((post, tweet_id))
    return candidates[:limit], unresolved


def normalize_x_api_post(
    tweet: dict[str, Any],
    *,
    username: str,
    default_topic: str,
    default_post_type: str,
    imitate: bool = False,
) -> NormalizedXApiPost:
    x_post_id = str(tweet.get("id") or "").strip()
    if not x_post_id:
        raise ValueError("X API tweet is missing id.")
    clean_username = normalize_username(username)
    return NormalizedXApiPost(
        x_post_id=x_post_id,
        text=str(tweet.get("text") or "").strip(),
        url=f"https://x.com/{clean_username}/status/{x_post_id}",
        date=str(tweet.get("created_at") or "").strip() or None,
        author=clean_username,
        topic=default_topic,
        post_type=default_post_type,
        metrics=metrics_from_x_public_metrics(tweet.get("public_metrics") or {}),
        imitate=imitate,
    )


def metrics_from_x_public_metrics(
    public_metrics: dict[str, Any],
    *,
    measured_at: str | None = None,
) -> EngagementMetrics | None:
    values = {
        "impressions": _optional_int(public_metrics.get("impression_count")),
        "likes": _optional_int(public_metrics.get("like_count")),
        "replies": _optional_int(public_metrics.get("reply_count")),
        "reposts": _optional_int(public_metrics.get("retweet_count")),
        "quotes": _optional_int(public_metrics.get("quote_count")),
        "bookmarks": _optional_int(public_metrics.get("bookmark_count")),
    }
    if all(value is None for value in values.values()):
        return None
    return EngagementMetrics(**values, measured_at=measured_at or utc_now())


def is_duplicate_x_api_post(store: SQLiteStore, normalized: NormalizedXApiPost) -> bool:
    return bool(
        store.get_post_by_url(normalized.url)
        or store.get_post_by_x_post_id(normalized.x_post_id)
    )


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@")


def should_skip_referenced_tweet(
    tweet: dict[str, Any],
    *,
    exclude_replies: bool,
    exclude_retweets: bool,
) -> bool:
    reference_types = {
        reference.get("type")
        for reference in tweet.get("referenced_tweets") or []
        if reference.get("type")
    }
    if exclude_retweets and "retweeted" in reference_types:
        return True
    if exclude_replies and "replied_to" in reference_types:
        return True
    return False


def parse_x_post_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    try:
        status_index = parts.index("status")
    except ValueError:
        return None
    if status_index + 1 >= len(parts):
        return None
    tweet_id = parts[status_index + 1].strip()
    return tweet_id or None


def metric_mover(
    post: SocialPost,
    current: EngagementMetrics,
    previous: EngagementMetrics | None,
) -> XMetricsMover | None:
    if not previous:
        return None
    total_delta = current.total_engagements() - previous.total_engagements()
    if current.impressions is not None and previous.impressions is not None:
        impression_delta = current.impressions - previous.impressions
    else:
        impression_delta = None
    if total_delta <= 0 and (impression_delta is None or impression_delta <= 0):
        return None
    return XMetricsMover(
        post_id=post.id,
        url=post.url,
        total_engagement_delta=total_delta,
        impression_delta=impression_delta,
    )


def _post_is_recent_enough(post: SocialPost, cutoff: datetime) -> bool:
    for value in [post.published_at, post.created_at]:
        parsed = _parse_datetime(value)
        if parsed:
            return parsed >= cutoff
    return False


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _chunks(items, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _oauth_quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~")


def _normalized_oauth_params(params: dict[str, str]) -> str:
    return "&".join(
        f"{_oauth_quote(key)}={_oauth_quote(value)}"
        for key, value in sorted(params.items())
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
