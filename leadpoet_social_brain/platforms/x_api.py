from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from leadpoet_social_brain.core.models import EngagementMetrics, SocialPost
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


class XApiClient:
    def __init__(
        self,
        bearer_token: str,
        *,
        base_url: str = "https://api.twitter.com/2",
        request_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ):
        if not bearer_token:
            raise ValueError("X_BEARER_TOKEN is required for X API import.")
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


def metrics_from_x_public_metrics(public_metrics: dict[str, Any]) -> EngagementMetrics | None:
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
    return EngagementMetrics(**values)


def is_duplicate_x_api_post(store: SQLiteStore, normalized: NormalizedXApiPost) -> bool:
    return bool(
        store.get_post_by_url(normalized.url)
        or store.get_post_by_x_post_id(normalized.x_post_id)
    )


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
