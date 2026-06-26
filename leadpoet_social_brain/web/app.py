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
from leadpoet_social_brain.categorization import XPostLLMCategorizer
from leadpoet_social_brain.content_queue import (
    accept_queued_post_version,
    approve_queue_item,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    revise_queued_post,
    weekly_report_text,
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
    DEFAULT_IMPORT_POST_TYPE,
    DEFAULT_IMPORT_TOPIC,
    UNCATEGORIZED_TOPIC,
    XApiClient,
    XWriteClient,
    sync_x_metrics,
    sync_x_user_posts,
)
from leadpoet_social_brain.providers import load_provider_from_env
from leadpoet_social_brain.storage.sqlite_store import (
    DEFAULT_DB_PATH,
    DEFAULT_WEB_AUTH_USERNAME,
    SQLiteStore,
    resolve_db_path,
)


STATIC_DIR = Path(__file__).with_name("static")
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
AUTH_COOKIE_SECURE_ENV = "SOCIAL_BRAIN_AUTH_COOKIE_SECURE"
TASK_TOKEN_ENV = "SOCIAL_BRAIN_TASK_TOKEN"
LLM_MODEL_SETTING = "llm_model"
DEFAULT_AUTH_USERNAME = DEFAULT_WEB_AUTH_USERNAME
SESSION_COOKIE = "social_brain_session"
SESSION_TTL_SECONDS = 60 * 60 * 12
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_CONFIG_MESSAGE = "Social Brain login is not configured. Run set-web-login once."
TASK_AUTH_CONFIG_MESSAGE = "Social Brain task token is not configured. Set SOCIAL_BRAIN_TASK_TOKEN."
TASK_AUTH_REQUIRED_MESSAGE = "Task authentication required."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Leadpoet Social Brain local UI")
    parser.add_argument(
        "--db",
        help=(
            "Path to SQLite database. Defaults to SOCIAL_BRAIN_DB, "
            "then RAILWAY_VOLUME_MOUNT_PATH/social_brain.sqlite3, "
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
    print(f"Social Brain DB path: {db_path}")
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
                store = SQLiteStore(db_path)
                try:
                    if request_is_authenticated(self.headers, store):
                        self._redirect("/")
                    else:
                        self._serve_login(configured=auth_is_configured(store))
                finally:
                    store.close()
                return
            if self.path.startswith("/api/tasks/"):
                if not self._require_task_auth():
                    return
                self._serve_api("GET")
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
            if self.path.startswith("/api/tasks/"):
                if not self._require_task_auth():
                    return
                self._serve_api("POST")
                return
            if not self._require_auth():
                return
            self._serve_api("POST")

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib hook.
            if not self._require_auth():
                return
            self._serve_api("PATCH")

        def _require_auth(self) -> bool:
            store = SQLiteStore(db_path)
            try:
                if not auth_is_configured(store):
                    if self.path.startswith("/api/"):
                        self._serve_json(503, {"error": AUTH_CONFIG_MESSAGE})
                    else:
                        self._redirect("/login")
                    return False
                if request_is_authenticated(self.headers, store):
                    return True
                if self.path.startswith("/api/"):
                    self._serve_json(401, {"error": AUTH_REQUIRED_MESSAGE})
                else:
                    self._redirect("/login")
                return False
            finally:
                store.close()

        def _require_task_auth(self) -> bool:
            if not os.getenv(TASK_TOKEN_ENV):
                self._serve_json(503, {"error": TASK_AUTH_CONFIG_MESSAGE})
                return False
            if request_has_task_token(self.headers):
                return True
            self._serve_json(401, {"error": TASK_AUTH_REQUIRED_MESSAGE})
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
            store = SQLiteStore(db_path)
            try:
                if verify_credentials(store, username, password):
                    token = create_session_cookie_value(store, username)
                    self._redirect("/", extra_headers=[session_cookie_header(token)])
                    return
                self._serve_login(
                    error="Invalid username or password.",
                    configured=auth_is_configured(store),
                )
            finally:
                store.close()

        def _serve_login(self, error: str | None = None, *, configured: bool = False) -> None:
            content = login_page(error=error, configured=configured).encode("utf-8")
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


def auth_is_configured(store: SQLiteStore) -> bool:
    return store.has_web_auth_user()


def verify_credentials(store: SQLiteStore, username: str, password: str) -> bool:
    return store.verify_web_auth_user(username, password)


def create_session_cookie_value(store: SQLiteStore, username: str) -> str:
    user = store.get_web_auth_user(username)
    if not user:
        raise ValueError(AUTH_CONFIG_MESSAGE)
    payload = {
        "u": user.username,
        "e": int(time.time()) + SESSION_TTL_SECONDS,
        "n": secrets.token_urlsafe(16),
    }
    payload_text = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload_text}.{session_signature(payload_text, user.password_hash)}"


def validate_session_cookie_value(store: SQLiteStore, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    payload_text, signature = value.rsplit(".", 1)
    try:
        padded = payload_text + ("=" * (-len(payload_text) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return False
    user = store.get_web_auth_user(str(payload.get("u") or ""))
    if not user:
        return False
    expected = session_signature(payload_text, user.password_hash)
    if not hmac.compare_digest(signature, expected):
        return False
    return (
        payload.get("u") == user.username
        and isinstance(payload.get("e"), int)
        and payload["e"] >= int(time.time())
    )


def request_is_authenticated(headers: Mapping[str, str], store: SQLiteStore) -> bool:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except CookieError:
        return False
    morsel = cookie.get(SESSION_COOKIE)
    return validate_session_cookie_value(store, morsel.value if morsel else None)


def request_has_task_token(headers: Mapping[str, str]) -> bool:
    expected = os.getenv(TASK_TOKEN_ENV, "")
    if not expected:
        return False
    candidate = task_token_from_headers(headers)
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def task_token_from_headers(headers: Mapping[str, str]) -> str:
    authorization = headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return headers.get("X-Social-Brain-Task-Token", "").strip()


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


def login_page(*, error: str | None = None, configured: bool = False) -> str:
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
    <title>Koinoniko Login</title>
    <link rel="icon" type="image/svg+xml" href="/static/koinoniko-favicon.svg">
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
        <h1>Koinoniko</h1>
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
        return 200, status_payload(store)
    if method == "GET" and parts == ["api", "account"]:
        return 200, account_payload(store)
    if method == "PATCH" and parts == ["api", "account"]:
        store.save_app_setting(LLM_MODEL_SETTING, clean_optional(payload.get("llm_model")))
        return 200, account_payload(store)
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
        provider = load_configured_provider(store)
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
        provider = load_configured_provider(store)
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
        return 200, publish_due_payload(store, payload)
    if method == "GET" and parts == ["api", "analytics"]:
        return 200, analytics_payload(store)
    if method == "POST" and parts == ["api", "sync-metrics"]:
        return 200, sync_metrics_payload(store, payload)
    if method == "POST" and parts == ["api", "tasks", "publish-due-x"]:
        return 200, publish_due_payload(store, payload)
    if method == "POST" and parts == ["api", "tasks", "sync-x-metrics"]:
        return 200, sync_metrics_payload(store, payload)
    if method == "POST" and parts == ["api", "tasks", "sync-x-user"]:
        return 200, sync_x_user_payload(store, payload)
    if method == "POST" and parts == ["api", "tasks", "weekly-report-x"]:
        return 200, weekly_report_payload(store, payload)
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


def publish_due_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    client = XWriteClient.from_env()
    results = publish_due_x(
        store,
        client,
        now=clean_optional(payload.get("now")),
        limit=int(payload.get("limit") or 20),
        author=str(payload.get("author") or "Gavin"),
    )
    return {"results": [publish_result_payload(result) for result in results]}


def sync_metrics_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
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
    return {"result": asdict(result)}


def sync_x_user_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    username = clean_optional(payload.get("username"))
    if not username:
        raise ValueError("username is required.")
    auto_categorize = bool(payload.get("auto_categorize", False))
    categorizer = None
    if auto_categorize:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        categorizer = XPostLLMCategorizer(provider).categorize
    default_topic = clean_optional(payload.get("default_topic")) or (
        UNCATEGORIZED_TOPIC if auto_categorize else DEFAULT_IMPORT_TOPIC
    )
    client = XApiClient.from_env()
    result = sync_x_user_posts(
        store,
        client,
        username=username,
        limit=int(payload.get("limit") or 100),
        default_topic=default_topic,
        default_post_type=clean_optional(payload.get("default_post_type")) or DEFAULT_IMPORT_POST_TYPE,
        start_time=clean_optional(payload.get("start_time")),
        end_time=clean_optional(payload.get("end_time")),
        exclude_replies=bool(payload.get("exclude_replies", True)),
        exclude_retweets=bool(payload.get("exclude_retweets", True)),
        dry_run=bool(payload.get("dry_run", False)),
        imitate=bool(payload.get("imitate", False)),
        categorizer=categorizer,
    )
    return {"result": sync_x_user_result_payload(result)}


def sync_x_user_result_payload(result) -> dict[str, Any]:
    return {
        "fetched": result.fetched,
        "imported": result.imported,
        "would_import": result.would_import,
        "skipped": result.skipped,
        "duplicates": result.duplicates,
        "errors": result.errors,
        "topics": dict(result.topics),
        "post_types": dict(result.post_types),
        "dry_run_posts": [
            {
                "x_post_id": post.x_post_id,
                "url": post.url,
                "date": post.date,
                "topic": post.topic,
                "post_type": post.post_type,
                "text": post.text,
            }
            for post in result.dry_run_posts[:10]
        ],
    }


def weekly_report_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "report": weekly_report_text(
            store,
            since_days=int(payload.get("since_days") or 7),
        )
    }


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
        "status": status_payload(store),
        "queue_counts": counts,
        "due": [queue_item_payload(store, item) for item in due],
        "recent_queue": [queue_item_payload(store, item) for item in queue[:10]],
        "analytics_hints": analytics.hints,
        "best_examples": [post_payload(post) for post in analytics.best_examples[:5]],
    }


def status_payload(store: SQLiteStore | None = None) -> dict[str, Any]:
    model = configured_llm_model(store)
    saved_model = store.get_app_setting(LLM_MODEL_SETTING) if store else None
    if saved_model:
        model_source = "account"
    elif os.getenv("SOCIAL_BRAIN_MODEL"):
        model_source = "SOCIAL_BRAIN_MODEL"
    elif os.getenv("OPENROUTER_MODEL"):
        model_source = "OPENROUTER_MODEL"
    else:
        model_source = "missing"
    return {
        "provider_configured": bool(os.getenv("SOCIAL_BRAIN_PROVIDER")),
        "provider_path": os.getenv("SOCIAL_BRAIN_PROVIDER") or "",
        "openrouter_api_key_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "llm_model": model or "",
        "llm_model_source": model_source,
        "x_read_configured": bool(os.getenv("X_BEARER_TOKEN")),
        "x_read_env_var": "X_BEARER_TOKEN",
        "x_write_configured": has_x_write_credentials(),
        "x_write_env_vars": [
            "X_OAUTH2_ACCESS_TOKEN",
            "or X_API_KEY + X_API_SECRET + X_ACCESS_TOKEN + X_ACCESS_TOKEN_SECRET",
        ],
        "task_trigger_configured": bool(os.getenv(TASK_TOKEN_ENV)),
    }


def account_payload(store: SQLiteStore) -> dict[str, Any]:
    return {
        "status": status_payload(store),
        "settings": {
            "llm_model": store.get_app_setting(LLM_MODEL_SETTING) or "",
            "effective_llm_model": configured_llm_model(store) or "",
        },
    }


def configured_llm_model(store: SQLiteStore | None = None) -> str | None:
    if store:
        saved = clean_optional(store.get_app_setting(LLM_MODEL_SETTING))
        if saved:
            return saved
    return (
        clean_optional(os.getenv("SOCIAL_BRAIN_MODEL"))
        or clean_optional(os.getenv("OPENROUTER_MODEL"))
    )


def load_configured_provider(store: SQLiteStore) -> Any:
    return load_provider_from_env(model_override=configured_llm_model(store))


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
