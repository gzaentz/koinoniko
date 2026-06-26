from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.content_queue import (
    accept_queued_post_version,
    approve_queue_item,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    revise_queued_post,
)
from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    QueuedPostVersion,
    TrendItem,
    new_id,
    utc_now,
)
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.platforms.x_api import (
    XApiClient,
    XWriteClient,
    sync_x_metrics,
)
from leadpoet_social_brain.providers import load_provider_from_env
from leadpoet_social_brain.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore, resolve_db_path


STATIC_DIR = Path(__file__).with_name("static")
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
AUTH_USERNAME_ENV = "SOCIAL_BRAIN_AUTH_USERNAME"
AUTH_PASSWORD_ENV = "SOCIAL_BRAIN_AUTH_PASSWORD"
AUTH_SECRET_ENV = "SOCIAL_BRAIN_AUTH_SECRET"
AUTH_COOKIE_SECURE_ENV = "SOCIAL_BRAIN_AUTH_COOKIE_SECURE"
DEFAULT_AUTH_USERNAME = "gavinz444"
SESSION_COOKIE = "social_brain_session"
SESSION_TTL_SECONDS = 60 * 60 * 12
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_CONFIG_MESSAGE = "Social Brain login is not configured. Set SOCIAL_BRAIN_AUTH_PASSWORD."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Leadpoet Social Brain local UI")
    parser.add_argument(
        "--db",
        help=(
            "Path to SQLite database. Defaults to SOCIAL_BRAIN_DB, "
            f"then {DEFAULT_DB_PATH}."
        ),
    )
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    parser.add_argument("--env-file", default=".env", help="Optional local env file to load.")
    args = parser.parse_args(argv)
    load_local_env(args.env_file)
    serve(db_path=resolve_db_path(args.db), host=args.host, port=args.port)
    return 0


def serve(*, db_path: Path, host: str, port: int) -> None:
    handler = make_handler(db_path)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Social Brain UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Social Brain UI.")
    finally:
        server.server_close()


def make_handler(db_path: Path):
    class SocialBrainHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook.
            if self.path.startswith("/login"):
                if request_is_authenticated(self.headers):
                    self._redirect("/")
                else:
                    self._serve_login()
                return
            if not self._require_auth():
                return
            if self.path == "/" or self.path.startswith("/static/"):
                self._serve_static()
                return
            self._serve_api("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook.
            if self.path.startswith("/login"):
                self._handle_login()
                return
            if self.path.startswith("/logout"):
                self._redirect("/login", extra_headers=[expired_session_cookie_header()])
                return
            if not self._require_auth():
                return
            self._serve_api("POST")

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib hook.
            if not self._require_auth():
                return
            self._serve_api("PATCH")

        def _require_auth(self) -> bool:
            if not auth_is_configured():
                if self.path.startswith("/api/"):
                    self._serve_json(503, {"error": AUTH_CONFIG_MESSAGE})
                else:
                    self._redirect("/login")
                return False
            if request_is_authenticated(self.headers):
                return True
            if self.path.startswith("/api/"):
                self._serve_json(401, {"error": AUTH_REQUIRED_MESSAGE})
            else:
                self._redirect("/login")
            return False

        def _serve_api(self, method: str) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            try:
                status, payload = handle_api_request(method, self.path, body, db_path=db_path)
            except Exception as exc:  # noqa: BLE001 - local UI should return readable JSON errors.
                status, payload = 500, {"error": str(exc)}
            self._serve_json(status, payload)

        def _serve_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_response(status)
            for key, value in JSON_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def _serve_static(self) -> None:
            requested = "index.html" if self.path == "/" else self.path.removeprefix("/static/")
            path = (STATIC_DIR / requested).resolve()
            if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(path.read_bytes())

        def _handle_login(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)).decode("utf-8")
            fields = parse_qs(body)
            username = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
            if verify_credentials(username, password):
                token = create_session_cookie_value(auth_username())
                self._redirect("/", extra_headers=[session_cookie_header(token)])
                return
            self._serve_login(error="Invalid username or password.")

        def _serve_login(self, error: str | None = None) -> None:
            content = login_page(error=error).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _redirect(self, location: str, *, extra_headers: list[tuple[str, str]] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            for key, value in extra_headers or []:
                self.send_header(key, value)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature.
            return

    return SocialBrainHandler


def auth_username() -> str:
    return os.getenv(AUTH_USERNAME_ENV, DEFAULT_AUTH_USERNAME)


def auth_password() -> str | None:
    value = os.getenv(AUTH_PASSWORD_ENV)
    return value if value else None


def auth_secret() -> str | None:
    return os.getenv(AUTH_SECRET_ENV) or auth_password()


def auth_is_configured() -> bool:
    return bool(auth_password())


def verify_credentials(username: str, password: str) -> bool:
    configured_password = auth_password()
    if not configured_password:
        return False
    return hmac.compare_digest(username, auth_username()) and hmac.compare_digest(password, configured_password)


def create_session_cookie_value(username: str) -> str:
    secret = auth_secret()
    if not secret:
        raise ValueError(AUTH_CONFIG_MESSAGE)
    payload = {
        "u": username,
        "e": int(time.time()) + SESSION_TTL_SECONDS,
        "n": secrets.token_urlsafe(16),
    }
    payload_text = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload_text}.{session_signature(payload_text, secret)}"


def validate_session_cookie_value(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    secret = auth_secret()
    if not secret:
        return False
    payload_text, signature = value.rsplit(".", 1)
    expected = session_signature(payload_text, secret)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = payload_text + ("=" * (-len(payload_text) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return False
    return (
        payload.get("u") == auth_username()
        and isinstance(payload.get("e"), int)
        and payload["e"] >= int(time.time())
    )


def request_is_authenticated(headers: Mapping[str, str]) -> bool:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except CookieError:
        return False
    morsel = cookie.get(SESSION_COOKIE)
    return validate_session_cookie_value(morsel.value if morsel else None)


def session_signature(payload_text: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_text.encode("ascii"), hashlib.sha256).hexdigest()


def session_cookie_header(token: str) -> tuple[str, str]:
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_TTL_SECONDS}",
    ]
    if auth_cookie_secure():
        parts.append("Secure")
    return "Set-Cookie", "; ".join(parts)


def expired_session_cookie_header() -> tuple[str, str]:
    return "Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def auth_cookie_secure() -> bool:
    configured = os.getenv(AUTH_COOKIE_SECURE_ENV)
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def login_page(*, error: str | None = None) -> str:
    configured = auth_is_configured()
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    config_html = (
        ""
        if configured
        else f'<p class="error">{html.escape(AUTH_CONFIG_MESSAGE)}</p>'
    )
    disabled = "" if configured else "disabled"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Social Brain Login</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #fff;
        color: #111;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(420px, calc(100vw - 32px));
        border: 1px solid #d9d9d9;
        border-radius: 8px;
        padding: 28px;
        display: grid;
        gap: 18px;
      }}
      h1 {{ margin: 0; font-size: 24px; }}
      p {{ margin: 0; color: #666; }}
      label {{
        display: grid;
        gap: 6px;
        color: #666;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
      }}
      input {{
        width: 100%;
        border: 1px solid #d9d9d9;
        border-radius: 6px;
        padding: 10px;
        font: inherit;
        color: #111;
      }}
      button {{
        min-height: 40px;
        border: 1px solid #111;
        border-radius: 6px;
        background: #111;
        color: #fff;
        font: inherit;
        cursor: pointer;
      }}
      button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
      .error {{
        color: #111;
        border: 1px solid #111;
        padding: 10px;
        border-radius: 6px;
      }}
    </style>
  </head>
  <body>
    <main>
      <div>
        <h1>Social Brain</h1>
        <p>Sign in to continue.</p>
      </div>
      {config_html}
      {error_html}
      <form method="post" action="/login">
        <label>Username<input name="username" autocomplete="username" required {disabled}></label>
        <label>Password<input name="password" type="password" autocomplete="current-password" required {disabled}></label>
        <button type="submit" {disabled}>Sign in</button>
      </form>
    </main>
  </body>
</html>"""


def handle_api_request(
    method: str,
    raw_path: str,
    body: bytes | str | None = None,
    *,
    db_path: str | Path | None = None,
) -> tuple[int, dict[str, Any]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    payload = parse_json_body(body)
    store = SQLiteStore(db_path)
    try:
        return route_api(store, method.upper(), path, query, payload)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()


def route_api(
    store: SQLiteStore,
    method: str,
    path: str,
    query: dict[str, list[str]],
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    parts = [part for part in path.split("/") if part]
    if not parts or parts[0] != "api":
        return 404, {"error": "Not found."}

    if method == "GET" and parts == ["api", "status"]:
        return 200, status_payload()
    if method == "GET" and parts == ["api", "dashboard"]:
        return 200, dashboard_payload(store)
    if method == "GET" and parts == ["api", "queue"]:
        items = store.list_content_queue(
            platform="x",
            status=one(query, "status"),
            limit=int(one(query, "limit") or 100),
        )
        return 200, {"items": [queue_item_payload(store, item) for item in items]}
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "queue"]:
        item = store.get_content_queue_item(parts[2])
        if not item:
            return 404, {"error": "Queued post not found."}
        return 200, {"item": queue_item_payload(store, item, include_versions=True)}
    if method == "PATCH" and len(parts) == 3 and parts[:2] == ["api", "queue"]:
        return 200, {"item": queue_item_payload(store, update_queue_item(store, parts[2], payload), include_versions=True)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "approve":
        return 200, {"item": queue_item_payload(store, approve_queue_item(store, parts[2]))}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "skip":
        return 200, {"item": queue_item_payload(store, set_queue_status(store, parts[2], "skipped"))}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "needs-review":
        return 200, {"item": queue_item_payload(store, set_queue_status(store, parts[2], "needs_review"))}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "post-now":
        item = require_queue_item_for_web(store, parts[2])
        client = XWriteClient.from_env()
        result = post_queue_item_x(store, client, item=item, author=str(payload.get("author") or "Gavin"))
        status = 200 if result.error is None else 400
        return status, {"result": publish_result_payload(result)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "revise":
        provider = load_provider_from_env()
        if not provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        versions = revise_queued_post(
            store,
            provider,
            queue_id=parts[2],
            instruction=str(payload.get("instruction") or ""),
            variants=int(payload.get("variants") or 3),
        )
        return 200, {"versions": [queue_version_payload(version) for version in versions]}
    if method == "POST" and len(parts) == 5 and parts[:2] == ["api", "queue"] and parts[3] == "versions":
        item = accept_queued_post_version(store, version_id=parts[4])
        return 200, {"item": queue_item_payload(store, item, include_versions=True)}
    if method == "POST" and parts == ["api", "plan-week"]:
        provider = load_provider_from_env()
        if not provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        generator = SocialBrainGenerator(store, provider=provider)
        result = plan_week_x(
            store,
            generator,
            week_start=str(payload.get("week_start") or next_monday()),
            posts=int(payload.get("posts") or 5),
            use_trends=bool(payload.get("use_trends", False)),
            trend_window_days=int(payload.get("trend_window_days") or 14),
            topic_mix=clean_optional(payload.get("topic_mix")),
            cadence=str(payload.get("cadence") or "weekday_mornings"),
            default_post_type=str(payload.get("default_post_type") or "founder_insight"),
            variants_per_slot=int(payload.get("variants_per_slot") or 1),
            allow_evergreen=bool(payload.get("allow_evergreen", True)),
        )
        return 200, {
            "queued": [queue_item_payload(store, item) for item in result.queued],
            "warnings": result.warnings,
        }
    if method == "POST" and parts == ["api", "publish-due"]:
        client = XWriteClient.from_env()
        results = publish_due_x(
            store,
            client,
            now=clean_optional(payload.get("now")),
            limit=int(payload.get("limit") or 20),
            author=str(payload.get("author") or "Gavin"),
        )
        return 200, {"results": [publish_result_payload(result) for result in results]}
    if method == "GET" and parts == ["api", "analytics"]:
        return 200, analytics_payload(store)
    if method == "POST" and parts == ["api", "sync-metrics"]:
        client = XApiClient.from_env()
        result = sync_x_metrics(
            store,
            client,
            post_id=clean_optional(payload.get("post_id")),
            x_post_id=clean_optional(payload.get("x_post_id")),
            since_days=optional_int(payload.get("since_days")),
            limit=int(payload.get("limit") or 100),
            dry_run=bool(payload.get("dry_run", False)),
        )
        return 200, {"result": asdict(result)}
    if method == "GET" and parts == ["api", "trends"]:
        trends = store.list_trends(
            topic=one(query, "topic"),
            status=one(query, "status"),
            urgency=one(query, "urgency"),
            limit=int(one(query, "limit") or 50),
        )
        return 200, {"items": [trend_payload(trend) for trend in trends]}
    if method == "POST" and parts == ["api", "trends"]:
        trend = store.save_trend_item(trend_from_payload(payload))
        return 200, {"item": trend_payload(trend)}
    if method == "PATCH" and len(parts) == 3 and parts[:2] == ["api", "trends"]:
        existing = store.get_trend_item(parts[2])
        if not existing:
            return 404, {"error": "Trend not found."}
        trend = store.save_trend_item(trend_from_payload(payload, existing=existing))
        return 200, {"item": trend_payload(trend)}
    return 404, {"error": "Not found."}


def update_queue_item(store: SQLiteStore, queue_id: str, payload: dict[str, Any]) -> ContentQueueItem:
    item = require_queue_item_for_web(store, queue_id)
    new_text = value_or_current(payload, "text", item.text)
    if new_text != item.text:
        store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=item.id,
                text=item.text,
                edit_type="manual",
                edit_instruction="previous text before manual edit",
            )
        )
    status = value_or_current(payload, "status", item.status)
    approved_at = item.approved_at
    if status != "approved":
        approved_at = None
    updated = ContentQueueItem(
        **{
            **item.__dict__,
            "text": new_text,
            "topic": value_or_current(payload, "topic", item.topic),
            "post_type": value_or_current(payload, "post_type", item.post_type),
            "angle": value_or_current(payload, "angle", item.angle),
            "scheduled_at": value_or_current(payload, "scheduled_at", item.scheduled_at),
            "reviewer_notes": value_or_current(payload, "reviewer_notes", item.reviewer_notes),
            "status": status,
            "approved_at": approved_at,
            "failure_reason": None if status != "failed" else item.failure_reason,
        }
    )
    return store.save_content_queue_item(updated)


def set_queue_status(store: SQLiteStore, queue_id: str, status: str) -> ContentQueueItem:
    item = require_queue_item_for_web(store, queue_id)
    return store.save_content_queue_item(
        ContentQueueItem(
            **{
                **item.__dict__,
                "status": status,
                "approved_at": None if status != "approved" else item.approved_at,
                "failure_reason": None if status != "failed" else item.failure_reason,
            }
        )
    )


def require_queue_item_for_web(store: SQLiteStore, queue_id: str) -> ContentQueueItem:
    item = store.get_content_queue_item(queue_id)
    if not item:
        raise ValueError(f"No queued post found for id {queue_id}")
    return item


def dashboard_payload(store: SQLiteStore) -> dict[str, Any]:
    queue = store.list_content_queue(platform="x", limit=500)
    counts: dict[str, int] = {}
    for item in queue:
        counts[item.status] = counts.get(item.status, 0) + 1
    due = [
        item for item in queue
        if item.status == "approved" and parse_dt(item.scheduled_at) <= datetime.now(timezone.utc)
    ][:10]
    analytics = build_analytics_summary(store)
    return {
        "status": status_payload(),
        "queue_counts": counts,
        "due": [queue_item_payload(store, item) for item in due],
        "recent_queue": [queue_item_payload(store, item) for item in queue[:10]],
        "analytics_hints": analytics.hints,
        "best_examples": [post_payload(post) for post in analytics.best_examples[:5]],
    }


def status_payload() -> dict[str, Any]:
    return {
        "provider_configured": bool(os.getenv("SOCIAL_BRAIN_PROVIDER")),
        "x_read_configured": bool(os.getenv("X_BEARER_TOKEN")),
        "x_write_configured": has_x_write_credentials(),
    }


def analytics_payload(store: SQLiteStore) -> dict[str, Any]:
    summary = build_analytics_summary(store)
    return {
        "by_post_type": [performance_payload(row) for row in summary.by_post_type],
        "by_topic": [performance_payload(row) for row in summary.by_topic],
        "by_pattern": [performance_payload(row) for row in summary.by_pattern],
        "by_hook": [performance_payload(row) for row in summary.by_hook],
        "by_structure": [performance_payload(row) for row in summary.by_structure],
        "by_tone": [performance_payload(row) for row in summary.by_tone],
        "by_cta": [performance_payload(row) for row in summary.by_cta],
        "by_formatting": [performance_payload(row) for row in summary.by_formatting],
        "by_trend_topic": [performance_payload(row) for row in summary.by_trend_topic],
        "by_trend_source_type": [performance_payload(row) for row in summary.by_trend_source_type],
        "by_trend_urgency": [performance_payload(row) for row in summary.by_trend_urgency],
        "by_used_trend": [performance_payload(row) for row in summary.by_used_trend],
        "best_examples": [post_payload(post) for post in summary.best_examples],
        "hints": summary.hints,
    }


def queue_item_payload(store: SQLiteStore, item: ContentQueueItem, *, include_versions: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "platform": item.platform,
        "draft_id": item.draft_id,
        "text": item.text,
        "topic": item.topic,
        "post_type": item.post_type,
        "angle": item.angle,
        "trend_ids": item.trend_ids,
        "scheduled_at": item.scheduled_at,
        "status": item.status,
        "reviewer_notes": item.reviewer_notes,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "approved_at": item.approved_at,
        "posted_at": item.posted_at,
        "x_post_id": item.x_post_id,
        "x_url": item.x_url,
        "failure_reason": item.failure_reason,
    }
    if include_versions:
        payload["versions"] = [
            queue_version_payload(version)
            for version in store.list_queued_post_versions(item.id, limit=20)
        ]
    return payload


def queue_version_payload(version: QueuedPostVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "queue_id": version.queue_id,
        "text": version.text,
        "edit_type": version.edit_type,
        "edit_instruction": version.edit_instruction,
        "created_at": version.created_at,
    }


def publish_result_payload(result) -> dict[str, Any]:
    return {
        "queue_item": {
            "id": result.queue_item.id,
            "status": result.queue_item.status,
            "x_post_id": result.queue_item.x_post_id,
            "x_url": result.queue_item.x_url,
            "failure_reason": result.queue_item.failure_reason,
        },
        "posted": asdict(result.posted) if result.posted else None,
        "error": result.error,
    }


def trend_payload(trend: TrendItem) -> dict[str, Any]:
    return {
        "id": trend.id,
        "title": trend.title,
        "source_url": trend.source_url,
        "source_type": trend.source_type,
        "topic": trend.topic,
        "summary": trend.summary,
        "why_it_matters": trend.why_it_matters,
        "leadpoet_angle": trend.leadpoet_angle,
        "suggested_hooks": trend.suggested_hooks,
        "urgency": trend.urgency,
        "date": trend.date,
        "status": trend.status,
        "engagement_notes": trend.engagement_notes,
        "source_author": trend.source_author,
        "raw_text": trend.raw_text,
        "created_at": trend.created_at,
        "updated_at": trend.updated_at,
    }


def post_payload(post) -> dict[str, Any]:
    return {
        "id": post.id,
        "text": post.text,
        "url": post.url,
        "published_at": post.published_at,
        "author": post.author,
        "topic": post.topic,
        "post_type": post.post_type,
        "status": post.status,
    }


def performance_payload(row) -> dict[str, Any]:
    return {
        "key": row.key,
        "count": row.count,
        "avg_engagement_rate": row.avg_engagement_rate,
        "avg_total_engagements": row.avg_total_engagements,
        "avg_impressions": row.avg_impressions,
        "avg_bookmarks": row.avg_bookmarks,
        "avg_replies": row.avg_replies,
        "avg_reposts": row.avg_reposts,
        "top_post_id": row.top_post_id,
    }


def trend_from_payload(payload: dict[str, Any], *, existing: TrendItem | None = None) -> TrendItem:
    return TrendItem(
        id=existing.id if existing else str(payload.get("id") or "").strip() or new_id(),
        title=value_or_existing(payload.get("title"), existing.title if existing else "Untitled trend"),
        source_url=value_or_existing(payload.get("source_url"), existing.source_url if existing else None),
        source_type=value_or_existing(payload.get("source_type"), existing.source_type if existing else "other"),
        topic=value_or_existing(payload.get("topic"), existing.topic if existing else "Leadpoet"),
        summary=value_or_existing(payload.get("summary"), existing.summary if existing else ""),
        why_it_matters=value_or_existing(payload.get("why_it_matters"), existing.why_it_matters if existing else ""),
        leadpoet_angle=value_or_existing(payload.get("leadpoet_angle"), existing.leadpoet_angle if existing else ""),
        suggested_hooks=value_or_existing(payload.get("suggested_hooks"), existing.suggested_hooks if existing else None),
        urgency=value_or_existing(payload.get("urgency"), existing.urgency if existing else "medium"),
        date=value_or_existing(payload.get("date"), existing.date if existing else None),
        status=value_or_existing(payload.get("status"), existing.status if existing else "unused"),
        engagement_notes=value_or_existing(payload.get("engagement_notes"), existing.engagement_notes if existing else None),
        source_author=value_or_existing(payload.get("source_author"), existing.source_author if existing else None),
        raw_text=value_or_existing(payload.get("raw_text"), existing.raw_text if existing else None),
        created_at=existing.created_at if existing else utc_now(),
        updated_at=existing.updated_at if existing else utc_now(),
    )


def parse_json_body(body: bytes | str | None) -> dict[str, Any]:
    if not body:
        return {}
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    if not body.strip():
        return {}
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object.")
    return parsed


def load_local_env(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def has_x_write_credentials() -> bool:
    return bool(os.getenv("X_OAUTH2_ACCESS_TOKEN")) or all(
        [
            os.getenv("X_API_KEY"),
            os.getenv("X_API_SECRET"),
            os.getenv("X_ACCESS_TOKEN"),
            os.getenv("X_ACCESS_TOKEN_SECRET"),
        ]
    )


def one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def clean_optional(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def optional_int(value: object) -> int | None:
    return int(value) if value not in {None, ""} else None


def value_or_current(payload: dict[str, Any], key: str, current):
    return current if key not in payload else payload[key]


def value_or_existing(value: object, existing):
    cleaned = clean_optional(value)
    return existing if cleaned is None else cleaned


def next_monday() -> str:
    today = date.today()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    return (today + timedelta(days=days_until_monday)).isoformat()


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
