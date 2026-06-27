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
from collections import Counter
from http.cookies import CookieError, SimpleCookie
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.categorization import XPostLLMCategorizer
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.content_queue import (
    accept_queued_post_version,
    approve_queue_item,
    create_post_x,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    revise_queued_post,
    weekly_report_text,
)
from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    PostNote,
    QueuedPostVersion,
    SocialPost,
    SyncMetadata,
    TrendItem,
    ViralPattern,
    new_id,
    utc_now,
)
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.pattern_scout import (
    PATTERN_REFRESH_MISSING_PROVIDER,
    PATTERN_REFRESH_MISSING_SCRAPINGDOG,
    add_pattern_example,
    pattern_groups,
    refresh_patterns,
)
from leadpoet_social_brain.platforms.x_api import (
    DEFAULT_IMPORT_POST_TYPE,
    DEFAULT_IMPORT_TOPIC,
    UNCATEGORIZED_TOPIC,
    XApiClient,
    XWriteClient,
    parse_x_post_id,
    sync_x_metrics,
    sync_x_user_posts,
)
from leadpoet_social_brain.providers import (
    LEGACY_MODEL_SETTING,
    MODEL_ENV_KEYS,
    MODEL_SETTING_KEYS,
    generate_with_provider,
    load_provider_from_env,
    resolve_model_routing,
)
from leadpoet_social_brain.storage.sqlite_store import (
    DEFAULT_DB_PATH,
    DEFAULT_WEB_AUTH_USERNAME,
    SQLiteStore,
    resolve_db_path,
)
from leadpoet_social_brain.trend_scout import refresh_web_trends, scrapingdog_configured, web_trend_groups


STATIC_DIR = Path(__file__).with_name("static")
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
AUTH_COOKIE_SECURE_ENV = "SOCIAL_BRAIN_AUTH_COOKIE_SECURE"
TASK_TOKEN_ENV = "SOCIAL_BRAIN_TASK_TOKEN"
TASK_SECRET_ENV = "TASK_SECRET"
LLM_MODEL_SETTING = MODEL_SETTING_KEYS["default"]
DEFAULT_AUTH_USERNAME = DEFAULT_WEB_AUTH_USERNAME
DEFAULT_X_SYNC_USERNAME = "gavinzaentz"
DEFAULT_X_POST_SYNC_LIMIT = 3200
DAILY_X_POST_SYNC_LIMIT = 25
X_POST_INCREMENTAL_LOOKBACK_MINUTES = 10
X_POSTS_SYNC_KEY = "x_posts_sync"
X_METRICS_SYNC_KEY = "x_metrics_sync"
X_DAILY_SYNC_KEY = "x_daily_sync"
TRENDS_SYNC_KEY = "trends_sync"
PATTERNS_SYNC_KEY = "patterns_sync"
VOICE_CONTEXT_NAME = "learned_from_high_performers"
DEFAULT_TOPIC_OPTIONS = [
    "Leadpoet",
    "sales LLMs",
    "Bittensor",
    "product",
    "AI SDRs",
    "lead generation",
    "GTM",
    "AI",
    "founder/company",
    "uncategorized",
]
DEFAULT_POST_TYPE_OPTIONS = [
    "founder_insight",
    "product_update",
    "bittensor",
    "bittensor_update",
    "investor_facing",
    "quote_tweet",
    "launch",
    "technical_explanation",
    "light_funny",
]
SESSION_COOKIE = "social_brain_session"
SESSION_TTL_SECONDS = 60 * 60 * 12
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_CONFIG_MESSAGE = "Social Brain login is not configured. Run set-web-login once."
TASK_AUTH_CONFIG_MESSAGE = "Social Brain task token is not configured. Set SOCIAL_BRAIN_TASK_TOKEN."
TASK_AUTH_REQUIRED_MESSAGE = "Task authentication required."
TASK_SECRET_CONFIG_MESSAGE = "Task secret is not configured. Set TASK_SECRET."
TASK_SECRET_REQUIRED_MESSAGE = "Task authentication required."
MISSING_X_READ_CREDENTIALS_MESSAGE = "X read credentials missing. Set X_BEARER_TOKEN."
CREATE_POST_MISSING_PROVIDER_MESSAGE = "No LLM provider configured. Set provider before creating posts."


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
            if self.path.startswith("/tasks/"):
                if not self._require_task_secret():
                    return
                self._serve_api("POST")
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

        def _require_task_secret(self) -> bool:
            if not os.getenv(TASK_SECRET_ENV):
                self._serve_json(503, {"error": TASK_SECRET_CONFIG_MESSAGE})
                return False
            if request_has_task_secret(self.headers):
                return True
            self._serve_json(401, {"error": TASK_SECRET_REQUIRED_MESSAGE})
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
            parsed_path = urlparse(self.path).path
            requested = "index.html" if parsed_path == "/" else parsed_path.removeprefix("/static/")
            path = (STATIC_DIR / requested).resolve()
            if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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


def request_has_task_secret(headers: Mapping[str, str]) -> bool:
    expected = os.getenv(TASK_SECRET_ENV, "")
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
    if method == "POST" and parts == ["tasks", "sync-x-posts"]:
        return 200, sync_x_posts_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "sync-x-metrics"]:
        return 200, sync_x_metrics_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "sync-x-all"]:
        return 200, sync_x_all_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "sync-x-daily"]:
        return 200, sync_x_daily_payload(store, payload)
    if method == "POST" and parts == ["tasks", "refresh-trends"]:
        return 200, refresh_trends_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "refresh-patterns"]:
        return 200, refresh_patterns_task_payload(store, payload)
    if not parts or parts[0] != "api":
        return 404, {"error": "Not found."}

    if method == "GET" and parts == ["api", "status"]:
        return 200, status_payload(store)
    if method == "GET" and parts == ["api", "account"]:
        return 200, account_payload(store)
    if method == "GET" and parts == ["api", "account", "status"]:
        return 200, account_status_payload(store)
    if method == "PATCH" and parts == ["api", "account"]:
        save_account_model_settings(store, payload)
        return 200, account_payload(store)
    if method == "POST" and parts == ["api", "account", "test-provider"]:
        return test_provider_response(store)
    if method == "GET" and parts == ["api", "dashboard"]:
        return 200, dashboard_payload(store)
    if method == "GET" and parts == ["api", "voice", "status"]:
        return 200, voice_status_payload(store)
    if method == "GET" and parts == ["api", "voice", "current"]:
        return 200, voice_current_payload(store)
    if method == "GET" and parts == ["api", "voice", "examples"]:
        return 200, {"items": voice_examples_payload(store)}
    if method == "GET" and parts == ["api", "voice", "suggestions"]:
        return 200, {"items": voice_suggestions_payload(store)}
    if method == "GET" and parts == ["api", "taxonomy", "topics"]:
        return 200, {"items": taxonomy_options_payload(store, "topic", DEFAULT_TOPIC_OPTIONS)}
    if method == "GET" and parts == ["api", "taxonomy", "post-types"]:
        return 200, {"items": taxonomy_options_payload(store, "post_type", DEFAULT_POST_TYPE_OPTIONS)}
    if method == "POST" and parts == ["api", "voice", "update"]:
        limit = int(payload.get("limit") or 12)
        section = ContextManager(store).rebuild_style_guide_from_examples(limit=limit)
        status = voice_status_payload(store)
        return 200, {
            "message": "Voice updated from selected examples.",
            "examples_used": status["examples_count"],
            "current": voice_current_payload(store, section=section),
            "status": voice_status_payload(store),
        }
    if method == "GET" and parts == ["api", "patterns"]:
        return 200, patterns_payload(store, query)
    if method == "POST" and parts == ["api", "patterns"]:
        return 200, {"pattern": pattern_payload(add_pattern_from_payload(store, payload))}
    if method == "POST" and parts == ["api", "patterns", "refresh"]:
        return refresh_patterns_api_response(store, payload)
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "patterns"]:
        pattern = store.get_viral_pattern(parts[2])
        if not pattern:
            return 404, {"error": "Pattern not found."}
        return 200, {"pattern": pattern_payload(pattern)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "patterns"] and parts[3] in {"archive", "ignore"}:
        pattern = store.mark_viral_pattern_status(parts[2], "archived" if parts[3] == "archive" else "ignored")
        if not pattern:
            return 404, {"error": "Pattern not found."}
        return 200, {"pattern": pattern_payload(pattern)}
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "posts"]:
        post = store.get_post(parts[2])
        if not post:
            return 404, {"error": "Post not found."}
        return 200, {"post": post_detail_payload(store, post)}
    if method == "PATCH" and len(parts) == 3 and parts[:2] == ["api", "posts"]:
        return update_post_payload(store, parts[2], payload)
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "posts"] and parts[3] == "voice":
        post = store.get_post(parts[2])
        if not post:
            return 404, {"error": "Post not found."}
        updated = save_post_voice_payload(store, post, payload)
        return 200, {
            "message": "Saved as a Voice Example." if updated["voice"]["is_voice_example"] else "Voice settings saved.",
            "post": updated,
            "voice_status": voice_status_payload(store),
        }
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
            best_version=bool(payload.get("best_version", False)),
        )
        return 200, {"versions": [queue_version_payload(version) for version in versions]}
    if method == "POST" and len(parts) == 5 and parts[:2] == ["api", "queue"] and parts[3] == "versions":
        item = accept_queued_post_version(store, version_id=parts[4])
        return 200, {"item": queue_item_payload(store, item, include_versions=True)}
    if method == "POST" and parts == ["api", "content", "create-post"]:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(CREATE_POST_MISSING_PROVIDER_MESSAGE)
        result = create_post_x(
            store,
            SocialBrainGenerator(store, provider=provider),
            instruction=str(payload.get("instruction") or ""),
            topic=str(payload.get("topic") or "Leadpoet"),
            post_type=str(payload.get("post_type") or "founder_insight"),
            use_trends=bool_from_payload(payload, "use_trends", False),
            best_version=bool_from_payload(payload, "best_version", False),
            scheduled_at=clean_optional(payload.get("scheduled_at")),
            extra_context=clean_optional(payload.get("extra_context")),
            use_patterns=bool_from_payload(payload, "use_patterns", False),
            pattern_ids=string_list_payload(payload.get("pattern_ids")),
        )
        item_payload = queue_item_payload(store, result.item)
        return 200, {
            "item": item_payload,
            "generated_text": result.draft.text,
            "queue_id": result.item.id,
            "status": result.item.status,
            "warnings": result.warnings,
        }
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
            best_version=bool(payload.get("best_version", False)),
            use_patterns=bool(payload.get("use_patterns", False)),
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
    if method == "POST" and parts == ["api", "sync-x-user"]:
        return 200, sync_x_user_payload(store, payload)
    if method == "POST" and parts == ["api", "account", "sync-x-posts"]:
        return 200, sync_x_posts_account_payload(store, payload)
    if method == "POST" and parts == ["api", "account", "sync-x-metrics"]:
        return 200, sync_x_metrics_account_payload(store, payload)
    if method == "POST" and parts == ["api", "account", "sync-x-all"]:
        return 200, sync_x_all_account_payload(store, payload)
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
    if method == "POST" and parts == ["api", "trends", "refresh"]:
        try:
            groups = refresh_web_trends(store)
        except Exception as exc:  # noqa: BLE001 - web trend sources can fail independently of the app.
            return 502, {"error": f"Could not refresh web trends: {exc}"}
        return 200, trend_refresh_payload(groups)
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


def sync_x_posts_account_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    result, metadata = run_x_posts_sync(
        store,
        payload,
        limit=int(payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
    )
    return {
        "result": result,
        "sync": sync_metadata_payload(metadata),
        "account": account_status_payload(store),
    }


def sync_x_metrics_account_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    result, metadata = run_x_metrics_sync(
        store,
        payload,
        limit=int(payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
    )
    return {
        "result": result,
        "sync": sync_metadata_payload(metadata),
        "account": account_status_payload(store),
    }


def sync_x_all_account_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    posts_result, posts_sync = run_x_posts_sync(
        store,
        payload,
        limit=int(payload.get("post_limit") or payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
    )
    metrics_result, metrics_sync = run_x_metrics_sync(
        store,
        payload,
        limit=int(payload.get("metrics_limit") or payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
    )
    return {
        "posts": posts_result,
        "metrics": metrics_result,
        "sync": {
            X_POSTS_SYNC_KEY: sync_metadata_payload(posts_sync),
            X_METRICS_SYNC_KEY: sync_metadata_payload(metrics_sync),
        },
        "account": account_status_payload(store),
    }


def sync_x_posts_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    result, metadata = run_x_posts_sync(
        store,
        payload,
        limit=int(payload.get("limit") or DAILY_X_POST_SYNC_LIMIT),
    )
    return {
        "posts_fetched": result.get("fetched", 0),
        "new_posts_imported": result.get("imported", 0),
        "duplicates_skipped": result.get("duplicates", 0),
        "errors": result.get("errors", []),
        "completed_at": metadata.last_completed_at,
        "sync": sync_metadata_payload(metadata),
    }


def sync_x_metrics_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    result, metadata = run_x_metrics_sync(
        store,
        payload,
        limit=int(payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
        since_days=optional_int(payload.get("since_days")) if "since_days" in payload else 90,
    )
    return {
        "metrics_updated": result.get("updated", 0),
        "posts_checked": result.get("checked", 0),
        "skipped": result.get("skipped", 0),
        "errors": result.get("errors", []),
        "completed_at": metadata.last_completed_at,
        "sync": sync_metadata_payload(metadata),
    }


def sync_x_all_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    posts_result, _ = run_x_posts_sync(
        store,
        payload,
        limit=int(payload.get("post_limit") or payload.get("limit") or DAILY_X_POST_SYNC_LIMIT),
    )
    metrics_result, metrics_sync = run_x_metrics_sync(
        store,
        payload,
        limit=int(payload.get("metrics_limit") or payload.get("limit") or DEFAULT_X_POST_SYNC_LIMIT),
        since_days=optional_int(payload.get("since_days")) if "since_days" in payload else 90,
    )
    errors = [
        *posts_result.get("errors", []),
        *metrics_result.get("errors", []),
    ]
    return {
        "posts_fetched": posts_result.get("fetched", 0),
        "new_posts_imported": posts_result.get("imported", 0),
        "duplicates_skipped": posts_result.get("duplicates", 0),
        "metrics_updated": metrics_result.get("updated", 0),
        "errors": errors,
        "completed_at": metrics_sync.last_completed_at,
    }


def sync_x_daily_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        posts_result, _ = run_x_posts_sync(
            store,
            payload,
            limit=int(payload.get("post_limit") or DAILY_X_POST_SYNC_LIMIT),
        )
        metrics_result, _ = run_x_metrics_sync(
            store,
            payload,
            limit=int(payload.get("metrics_limit") or DAILY_X_POST_SYNC_LIMIT),
            since_days=optional_int(payload.get("since_days")) or 30,
        )
        completed_at = utc_now()
        errors = [
            *posts_result.get("errors", []),
            *metrics_result.get("errors", []),
        ]
        summary = {
            "posts_fetched": posts_result.get("fetched", 0),
            "new_posts_imported": posts_result.get("imported", 0),
            "duplicates_skipped": posts_result.get("duplicates", 0),
            "metrics_updated": metrics_result.get("updated", 0),
            "errors": errors,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=X_DAILY_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if errors else "success",
                summary=summary,
                error_message="\n".join(errors[:5]) if errors else None,
            )
        )
        return {
            **summary,
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - task should persist failure summary.
        completed_at = utc_now()
        summary = {
            "posts_fetched": 0,
            "new_posts_imported": 0,
            "duplicates_skipped": 0,
            "metrics_updated": 0,
            "errors": [str(exc)],
            "completed_at": completed_at,
        }
        store.save_sync_metadata(
            SyncMetadata(
                key=X_DAILY_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary=summary,
                error_message=str(exc),
            )
        )
        raise


def refresh_trends_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        groups = refresh_web_trends(store, limit=int(payload.get("limit") or 5))
        completed_at = utc_now()
        errors = list(groups.errors)
        summary = {
            "daily_trends": len(groups.daily),
            "weekly_trends": len(groups.weekly),
            "sources": groups.sources,
            "errors": errors,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=TRENDS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if errors else "success",
                summary=summary,
                error_message="\n".join(errors[:5]) if errors else None,
            )
        )
        return {
            **summary,
            "trend_groups": trend_groups_payload(groups),
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - task should persist failure summary.
        completed_at = utc_now()
        summary = {
            "daily_trends": 0,
            "weekly_trends": 0,
            "sources": [],
            "errors": [str(exc)],
            "completed_at": completed_at,
        }
        store.save_sync_metadata(
            SyncMetadata(
                key=TRENDS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary=summary,
                error_message=str(exc),
            )
        )
        raise


def refresh_patterns_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(PATTERN_REFRESH_MISSING_PROVIDER)
        result = refresh_patterns(
            store,
            provider,
            limit=int(payload.get("limit") or 5),
            window=str(payload.get("window") or "daily"),
        )
        completed_at = utc_now()
        summary = {
            "fetched": result.fetched,
            "stored": result.stored,
            "skipped": result.skipped,
            "errors": result.errors,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=PATTERNS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if result.errors else "success",
                summary=summary,
                error_message="\n".join(result.errors[:5]) if result.errors else None,
            )
        )
        return {
            **summary,
            "patterns": [pattern_payload(pattern) for pattern in result.patterns],
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - task should persist failure summary.
        completed_at = utc_now()
        summary = {
            "fetched": 0,
            "stored": 0,
            "skipped": 0,
            "errors": [str(exc)],
            "completed_at": completed_at,
        }
        store.save_sync_metadata(
            SyncMetadata(
                key=PATTERNS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary=summary,
                error_message=str(exc),
            )
        )
        raise


def run_x_posts_sync(
    store: SQLiteStore,
    payload: dict[str, Any],
    *,
    limit: int,
) -> tuple[dict[str, Any], SyncMetadata]:
    started_at = utc_now()
    try:
        require_x_read_credentials()
        auto_categorize = bool_from_payload(payload, "auto_categorize", False)
        categorizer = None
        if auto_categorize:
            provider = load_configured_provider(store)
            if not provider:
                raise ValueError(MISSING_PROVIDER_MESSAGE)
            categorizer = XPostLLMCategorizer(provider).categorize
        since_id, start_time = x_posts_incremental_cutoff(store, payload)
        result = sync_x_user_posts(
            store,
            XApiClient.from_env(),
            username=clean_optional(payload.get("username")) or DEFAULT_X_SYNC_USERNAME,
            limit=limit,
            default_topic=clean_optional(payload.get("default_topic")) or (
                UNCATEGORIZED_TOPIC if auto_categorize else DEFAULT_IMPORT_TOPIC
            ),
            default_post_type=clean_optional(payload.get("default_post_type")) or DEFAULT_IMPORT_POST_TYPE,
            since_id=since_id,
            start_time=start_time,
            end_time=clean_optional(payload.get("end_time")),
            exclude_replies=bool_from_payload(payload, "exclude_replies", True),
            exclude_retweets=bool_from_payload(payload, "exclude_retweets", True),
            dry_run=bool_from_payload(payload, "dry_run", False),
            imitate=bool_from_payload(payload, "imitate", False),
            categorizer=categorizer,
        )
        summary = sync_x_user_result_payload(result)
        summary["incremental"] = bool(since_id or start_time)
        if since_id:
            summary["since_id"] = since_id
        if start_time:
            summary["start_time"] = start_time
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=X_POSTS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=utc_now(),
                status="partial" if summary["errors"] else "success",
                summary=summary,
                error_message="\n".join(summary["errors"][:5]) if summary["errors"] else None,
            )
        )
        return summary, metadata
    except Exception as exc:  # noqa: BLE001 - store sync failure for account display.
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=X_POSTS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=utc_now(),
                status="failed",
                summary={},
                error_message=str(exc),
            )
        )
        raise ValueError(str(exc)) from exc


def x_posts_incremental_cutoff(store: SQLiteStore, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    explicit_since_id = clean_optional(payload.get("since_id"))
    explicit_start_time = clean_optional(payload.get("start_time"))
    if explicit_since_id or explicit_start_time or bool_from_payload(payload, "full_sync", False):
        return explicit_since_id, explicit_start_time

    latest = store.list_posts(platform="x", limit=1)
    if not latest:
        return None, None

    latest_post = latest[0]
    if latest_post.url:
        latest_x_post_id = parse_x_post_id(latest_post.url)
        if latest_x_post_id:
            return latest_x_post_id, None

    latest_at = parse_optional_dt(latest_post.published_at) or parse_optional_dt(latest_post.created_at)
    if not latest_at:
        return None, None
    start_time = latest_at - timedelta(minutes=X_POST_INCREMENTAL_LOOKBACK_MINUTES)
    return None, start_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_x_metrics_sync(
    store: SQLiteStore,
    payload: dict[str, Any],
    *,
    limit: int,
    since_days: int | None = None,
) -> tuple[dict[str, Any], SyncMetadata]:
    started_at = utc_now()
    try:
        require_x_read_credentials()
        result = sync_x_metrics(
            store,
            XApiClient.from_env(),
            post_id=clean_optional(payload.get("post_id")),
            x_post_id=clean_optional(payload.get("x_post_id")),
            since_days=since_days if since_days is not None else optional_int(payload.get("since_days")),
            limit=limit,
            dry_run=bool_from_payload(payload, "dry_run", False),
        )
        summary = asdict(result)
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=X_METRICS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=utc_now(),
                status="partial" if summary["errors"] else "success",
                summary=summary,
                error_message="\n".join(summary["errors"][:5]) if summary["errors"] else None,
            )
        )
        return summary, metadata
    except Exception as exc:  # noqa: BLE001 - store sync failure for account display.
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=X_METRICS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=utc_now(),
                status="failed",
                summary={},
                error_message=str(exc),
            )
        )
        raise ValueError(str(exc)) from exc


def require_x_read_credentials() -> None:
    if not os.getenv("X_BEARER_TOKEN"):
        raise ValueError(MISSING_X_READ_CREDENTIALS_MESSAGE)


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
        "recategorized": result.recategorized,
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


def voice_status_payload(store: SQLiteStore) -> dict[str, Any]:
    examples = voice_example_posts(store)
    current = current_voice_section(store)
    examples_count = len(examples)
    last_example_change = latest_voice_example_change(store, examples)
    if examples_count == 0:
        status = "needs_examples"
        label_text = "Needs examples"
        description = "No Voice Examples yet. Mark a few strong posts so Koinoniko can learn Gavin's voice."
    elif current is None:
        status = "not_updated_yet"
        label_text = "Not updated yet"
        description = "Voice Examples selected. Update Voice to use them in generation."
    elif last_example_change and current.updated_at and last_example_change > current.updated_at:
        status = "needs_update"
        label_text = "Needs update"
        description = "Voice Examples changed since the last Voice update."
    else:
        status = "ready"
        label_text = "Ready"
        description = "Koinoniko Voice is current."
    return {
        "status": status,
        "label": label_text,
        "description": description,
        "examples_count": examples_count,
        "last_updated_at": current.updated_at if current else None,
        "last_example_change_at": last_example_change,
    }


def voice_current_payload(store: SQLiteStore, *, section=None) -> dict[str, Any]:
    current = section or current_voice_section(store)
    if not current:
        return {
            "exists": False,
            "title": "Koinoniko Voice",
            "content": "",
            "sections": [],
            "updated_at": None,
            "empty_message": "No Voice yet. Mark a few strong posts as Voice Examples, then update Voice.",
        }
    return {
        "exists": True,
        "title": "Koinoniko Voice",
        "content": current.content,
        "sections": voice_content_sections(current.content),
        "updated_at": current.updated_at,
    }


def voice_examples_payload(store: SQLiteStore) -> list[dict[str, Any]]:
    posts = voice_example_posts(store)
    notes = store.notes_by_post_id([post.id for post in posts])
    metrics = store.latest_metrics_by_post_id([post.id for post in posts])
    return [
        voice_post_payload(post, notes.get(post.id), metrics.get(post.id))
        for post in posts
    ]


def voice_suggestions_payload(store: SQLiteStore, *, limit: int = 6) -> list[dict[str, Any]]:
    posts = store.list_posts(platform="x", limit=None)
    notes = store.notes_by_post_id([post.id for post in posts])
    metrics = store.latest_metrics_by_post_id([post.id for post in posts])
    candidates = [
        (voice_suggestion_score(metrics.get(post.id)), post)
        for post in posts
        if not (notes.get(post.id) and notes[post.id].imitate)
    ]
    candidates = [(score, post) for score, post in candidates if any(value > 0 for value in score)]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            **voice_post_payload(post, notes.get(post.id), metrics.get(post.id)),
            "reason": voice_suggestion_reason(metrics.get(post.id)),
        }
        for _, post in candidates[:limit]
    ]


def save_post_voice_payload(store: SQLiteStore, post: SocialPost, payload: dict[str, Any]) -> dict[str, Any]:
    existing = store.notes_by_post_id([post.id]).get(post.id)
    if "topic" in payload or "post_type" in payload:
        post = store.save_post(
            SocialPost(
                **{
                    **post.__dict__,
                    "topic": value_or_current(payload, "topic", post.topic),
                    "post_type": value_or_current(payload, "post_type", post.post_type),
                }
            )
        )
    note = store.save_note(
        PostNote(
            post_id=post.id,
            worked_reason=value_or_current(payload, "why_this_worked", existing.worked_reason if existing else None),
            pattern_name=value_or_current(payload, "pattern", existing.pattern_name if existing else None),
            hook=value_or_current(payload, "hook", existing.hook if existing else None),
            structure=value_or_current(payload, "structure", existing.structure if existing else None),
            tone=value_or_current(payload, "tone", existing.tone if existing else None),
            cta=value_or_current(payload, "cta", existing.cta if existing else None),
            formatting=value_or_current(payload, "formatting", existing.formatting if existing else None),
            imitate=bool_from_payload(payload, "voice_example", existing.imitate if existing else False),
        )
    )
    metrics = store.latest_metrics_by_post_id([post.id]).get(post.id)
    return post_detail_payload(store, post, note=note, metrics=metrics)


def post_detail_payload(
    store: SQLiteStore,
    post: SocialPost,
    *,
    note: PostNote | None = None,
    metrics: Any | None = None,
) -> dict[str, Any]:
    resolved_note = note if note is not None else store.notes_by_post_id([post.id]).get(post.id)
    resolved_metrics = metrics if metrics is not None else store.latest_metrics_by_post_id([post.id]).get(post.id)
    payload = recent_post_payload(post, resolved_metrics, resolved_note)
    payload["voice"] = voice_note_payload(resolved_note)
    return payload


def voice_example_posts(store: SQLiteStore) -> list[SocialPost]:
    posts = store.list_posts(platform="x", limit=None)
    notes = store.notes_by_post_id([post.id for post in posts])
    return [post for post in posts if notes.get(post.id) and notes[post.id].imitate]


def current_voice_section(store: SQLiteStore):
    sections = [
        section for section in store.list_context_sections(kinds={"style_guide"})
        if section.name == VOICE_CONTEXT_NAME
    ]
    return sections[0] if sections else None


def latest_voice_example_change(store: SQLiteStore, posts: list[SocialPost]) -> str | None:
    notes = store.notes_by_post_id([post.id for post in posts])
    timestamps = [
        value
        for post in posts
        for value in [post.updated_at, notes.get(post.id).updated_at if notes.get(post.id) else None]
        if value
    ]
    return max(timestamps) if timestamps else None


def voice_content_sections(content: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current_title = ""
    current_lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in {"Observed Gavin/Leadpoet Writing Guide", "Koinoniko Voice"}:
            continue
        if line.endswith(":"):
            if current_title or current_lines:
                sections.append({"title": current_title or "Koinoniko Voice", "items": current_lines})
            current_title = line[:-1]
            current_lines = []
            continue
        current_lines.append(line[2:] if line.startswith("- ") else line)
    if current_title or current_lines:
        sections.append({"title": current_title or "Koinoniko Voice", "items": current_lines})
    return sections


def voice_suggestion_score(metrics: Any | None) -> tuple[float, float, float, float]:
    if not metrics:
        return (0, 0, 0, 0)
    return (
        float(metrics.engagement_rate or 0),
        float(metrics.bookmarks or 0),
        float(metrics.replies or 0),
        float(metrics.impressions or 0),
    )


def voice_suggestion_reason(metrics: Any | None) -> str:
    if not metrics:
        return "Performance data available."
    if metrics.engagement_rate:
        return f"Strong engagement rate: {metrics.engagement_rate:.2%}."
    if metrics.bookmarks:
        return f"High bookmarks: {metrics.bookmarks}."
    if metrics.replies:
        return f"High replies: {metrics.replies}."
    if metrics.impressions:
        return f"High impressions: {metrics.impressions}."
    return "High-performing post."


def voice_post_payload(post: SocialPost, note: PostNote | None, metrics: Any | None) -> dict[str, Any]:
    payload = recent_post_payload(post, metrics, note)
    payload["why_this_worked"] = note.worked_reason if note else None
    return payload


def patterns_payload(store: SQLiteStore, query: dict[str, list[str]]) -> dict[str, Any]:
    patterns = store.list_viral_patterns(
        status=one(query, "status") or "active",
        window=one(query, "window"),
        limit=int(one(query, "limit") or 20),
    )
    return {"items": [pattern_payload(pattern) for pattern in patterns]}


def add_pattern_from_payload(store: SQLiteStore, payload: dict[str, Any]) -> ViralPattern:
    return add_pattern_example(
        store,
        source_url=clean_optional(payload.get("source_url")),
        source_author=clean_optional(payload.get("source_author")),
        source_text=clean_optional(payload.get("source_text")),
        source_summary=clean_optional(payload.get("source_summary")),
        topic=clean_optional(payload.get("topic")),
        category=clean_optional(payload.get("category")),
        pattern_name=clean_optional(payload.get("pattern_name")),
        hook_style=clean_optional(payload.get("hook_style")),
        structure=clean_optional(payload.get("structure")),
        pacing=clean_optional(payload.get("pacing")),
        tone=clean_optional(payload.get("tone")),
        formatting=clean_optional(payload.get("formatting")),
        why_it_worked=clean_optional(payload.get("why_it_worked")),
        adaptation_rule=clean_optional(payload.get("adaptation_rule")),
        engagement_notes=clean_optional(payload.get("engagement_notes")),
        likes=optional_int(payload.get("likes")),
        replies=optional_int(payload.get("replies")),
        reposts=optional_int(payload.get("reposts")),
        quotes=optional_int(payload.get("quotes")),
        bookmarks=optional_int(payload.get("bookmarks")),
        impressions=optional_int(payload.get("impressions")),
        collected_at=clean_optional(payload.get("collected_at")),
        window=clean_optional(payload.get("window")) or "manual",
        status=clean_optional(payload.get("status")) or "active",
    )


def refresh_patterns_api_response(store: SQLiteStore, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        return 200, refresh_patterns_task_payload(store, payload)
    except ValueError as exc:
        return 400, {"error": str(exc)}


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
    recent_posts = store.list_posts(platform="x", limit=None)
    latest_metrics = store.latest_metrics_by_post_id([post.id for post in recent_posts])
    notes = store.notes_by_post_id([post.id for post in recent_posts])
    trend_groups = web_trend_groups(store)
    trends = [*trend_groups.daily, *trend_groups.weekly]
    patterns = pattern_groups(store)
    return {
        "status": status_payload(store),
        "queue_counts": counts,
        "due": [queue_item_payload(store, item) for item in due],
        "recent_queue": [queue_item_payload(store, item) for item in queue[:10]],
        "recent_posts": [
            recent_post_payload(post, latest_metrics.get(post.id), notes.get(post.id))
            for post in recent_posts
        ],
        "trends": [trend_payload(trend) for trend in trends],
        "trend_groups": trend_groups_payload(trend_groups),
        "pattern_groups": pattern_groups_payload(patterns),
        "sync": account_sync_payload(store),
        "analytics_hints": analytics.hints,
        "best_examples": [post_payload(post) for post in analytics.best_examples[:5]],
    }


def taxonomy_options_payload(store: SQLiteStore, field: str, defaults: list[str]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()

    def add(value: object, *, increment: bool = True) -> None:
        text = clean_optional(value)
        if not text:
            return
        if increment:
            counts[text] += 1
        else:
            counts.setdefault(text, 0)

    for post in store.list_posts(platform="x", limit=None):
        add(getattr(post, field))
    for item in store.list_content_queue(platform="x", limit=500):
        add(getattr(item, field))
    for default in defaults:
        add(default, increment=False)

    return [
        {
            "value": value,
            "label": topic_label_for_payload(value) if field == "topic" else post_type_label_for_payload(value),
            "count": count,
        }
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]


def topic_label_for_payload(value: str) -> str:
    return "Uncategorized" if value.strip().lower() == "uncategorized" else value


def post_type_label_for_payload(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def update_post_payload(store: SQLiteStore, post_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    post = store.get_post(post_id)
    if not post:
        return 404, {"error": "Post not found."}
    topic = clean_optional(payload.get("topic"))
    post_type = clean_optional(payload.get("post_type"))
    if topic is None and post_type is None:
        return 400, {"error": "Topic or post type is required."}
    saved = store.save_post(
        replace(
            post,
            topic=topic or post.topic,
            post_type=post_type or post.post_type,
        )
    )
    return 200, {"post": post_detail_payload(store, saved)}


def status_payload(store: SQLiteStore | None = None) -> dict[str, Any]:
    model = configured_llm_model(store)
    saved_model = account_model_settings(store)["default"] if store else None
    if saved_model:
        model_source = "account"
    elif os.getenv(MODEL_ENV_KEYS["default"]):
        model_source = MODEL_ENV_KEYS["default"]
    elif os.getenv("SOCIAL_BRAIN_MODEL"):
        model_source = "SOCIAL_BRAIN_MODEL"
    elif os.getenv("OPENROUTER_MODEL"):
        model_source = "OPENROUTER_MODEL"
    else:
        model_source = "missing"
    payload = {
        "provider_configured": bool(os.getenv("SOCIAL_BRAIN_PROVIDER")),
        "provider_path": os.getenv("SOCIAL_BRAIN_PROVIDER") or "",
        "openrouter_api_key_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "llm_model": model or "",
        "llm_model_source": model_source,
        "model_routing": resolve_model_routing(store).as_dict() if store else {},
        "x_read_configured": bool(os.getenv("X_BEARER_TOKEN")),
        "x_write_configured": has_x_write_credentials(),
        "scrapingdog_configured": scrapingdog_configured(),
        "task_trigger_configured": bool(os.getenv(TASK_TOKEN_ENV)),
        "daily_sync_configured": bool(os.getenv(TASK_SECRET_ENV)),
    }
    payload["connections"] = account_connections_payload(payload)
    return payload


def account_payload(store: SQLiteStore) -> dict[str, Any]:
    routing = resolve_model_routing(store)
    status = status_payload(store)
    sync = account_sync_payload(store)
    return {
        "status": status,
        "connections": account_connections_payload(status),
        "sync": sync,
        "sync_status": account_sync_status_payload(sync),
        "security": account_security_payload(store),
        "stats": x_account_stats_payload(store),
        "settings": {
            "llm_model": account_model_settings(store)["default"],
            "effective_llm_model": routing.default or "",
            "models": account_model_settings(store),
            "effective_models": routing.as_dict(),
            "model_env_defaults": model_env_defaults(),
        },
    }


def account_status_payload(store: SQLiteStore) -> dict[str, Any]:
    status = status_payload(store)
    sync = account_sync_payload(store)
    return {
        "status": status,
        "connections": account_connections_payload(status),
        "sync": sync,
        "sync_status": account_sync_status_payload(sync),
        "security": account_security_payload(store),
        "stats": x_account_stats_payload(store),
    }


def account_connections_payload(status: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        "provider": connection_payload(
            "connected" if status.get("provider_configured") else "not_connected",
            "LLM Provider",
            "Used for post generation and AI rewrites.",
        ),
        "openrouter": connection_payload(
            "connected" if status.get("openrouter_api_key_configured") else "not_connected",
            "OpenRouter",
            "Routes model calls for Standard and Best Version generation.",
        ),
        "x_read": connection_payload(
            "connected" if status.get("x_read_configured") else "not_connected",
            "X Read",
            "Imports posts and refreshes public metrics.",
        ),
        "x_write": connection_payload(
            "connected" if status.get("x_write_configured") else "not_connected",
            "X Write",
            "Publishes approved scheduled posts.",
        ),
        "scrapingdog": connection_payload(
            "connected" if status.get("scrapingdog_configured") else "not_connected",
            "ScrapingDog",
            "Source used to refresh trends and Pattern Scout examples.",
        ),
        "daily_sync": connection_payload(
            "connected" if status.get("daily_sync_configured") else "not_configured",
            "Daily Sync",
            "Checks X for new posts and metrics through protected cron.",
        ),
    }


def connection_payload(status: str, label: str, description: str) -> dict[str, str]:
    return {
        "status": status,
        "label": label,
        "description": description,
    }


def account_sync_payload(store: SQLiteStore) -> dict[str, Any]:
    metadata = store.sync_metadata_by_key()
    return {
        X_POSTS_SYNC_KEY: sync_metadata_payload(metadata.get(X_POSTS_SYNC_KEY)),
        X_METRICS_SYNC_KEY: sync_metadata_payload(metadata.get(X_METRICS_SYNC_KEY)),
        X_DAILY_SYNC_KEY: sync_metadata_payload(metadata.get(X_DAILY_SYNC_KEY)),
        TRENDS_SYNC_KEY: sync_metadata_payload(metadata.get(TRENDS_SYNC_KEY)),
        PATTERNS_SYNC_KEY: sync_metadata_payload(metadata.get(PATTERNS_SYNC_KEY)),
    }


def account_sync_status_payload(sync: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_x_post_sync": sync[X_POSTS_SYNC_KEY]["last_completed_at"],
        "last_x_metrics_sync": sync[X_METRICS_SYNC_KEY]["last_completed_at"],
        "last_trend_refresh": sync[TRENDS_SYNC_KEY]["last_completed_at"],
        "last_pattern_refresh": sync[PATTERNS_SYNC_KEY]["last_completed_at"],
        "latest_error": latest_sync_error(sync),
    }


def latest_sync_error(sync: dict[str, Any]) -> str | None:
    for key in [PATTERNS_SYNC_KEY, TRENDS_SYNC_KEY, X_DAILY_SYNC_KEY, X_POSTS_SYNC_KEY, X_METRICS_SYNC_KEY]:
        item = sync.get(key) or {}
        if item.get("error_message"):
            return str(item["error_message"])
    return None


def account_security_payload(store: SQLiteStore) -> dict[str, Any]:
    return {
        "username": first_web_auth_username(store) or DEFAULT_AUTH_USERNAME,
        "login_configured": store.has_web_auth_user(),
    }


def first_web_auth_username(store: SQLiteStore) -> str | None:
    row = store.conn.execute(
        "SELECT username FROM web_auth_users ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return str(row["username"]) if row else None


def x_account_stats_payload(store: SQLiteStore) -> dict[str, Any]:
    latest_posts = store.list_posts(platform="x", limit=1)
    latest = latest_posts[0] if latest_posts else None
    return {
        "total_x_posts_stored": store.count_posts(platform="x"),
        "latest_imported_x_post_date": latest.published_at if latest else None,
    }


def save_account_model_settings(store: SQLiteStore, payload: dict[str, Any]) -> None:
    if bool(payload.get("reset_model_settings", False)):
        for key in [*MODEL_SETTING_KEYS.values(), LEGACY_MODEL_SETTING]:
            store.save_app_setting(key, None)
        return

    models = payload.get("models")
    if not isinstance(models, dict):
        models = {}
    if "llm_model" in payload:
        models = {**models, "default": payload.get("llm_model")}
    for role, key in MODEL_SETTING_KEYS.items():
        if role in models:
            store.save_app_setting(key, clean_optional(models.get(role)))
    if "default" in models:
        store.save_app_setting(LEGACY_MODEL_SETTING, None)


def account_model_settings(store: SQLiteStore | None) -> dict[str, str]:
    if not store:
        return {"default": "", "strategist": "", "natural_writer": "", "evaluator": ""}
    return {
        "default": (
            clean_optional(store.get_app_setting(MODEL_SETTING_KEYS["default"]))
            or clean_optional(store.get_app_setting(LEGACY_MODEL_SETTING))
            or ""
        ),
        "strategist": clean_optional(store.get_app_setting(MODEL_SETTING_KEYS["strategist"])) or "",
        "natural_writer": clean_optional(store.get_app_setting(MODEL_SETTING_KEYS["natural_writer"])) or "",
        "evaluator": clean_optional(store.get_app_setting(MODEL_SETTING_KEYS["evaluator"])) or "",
    }


def model_env_defaults() -> dict[str, str]:
    return {
        "default": (
            clean_optional(os.getenv(MODEL_ENV_KEYS["default"]))
            or clean_optional(os.getenv("SOCIAL_BRAIN_MODEL"))
            or clean_optional(os.getenv("OPENROUTER_MODEL"))
            or ""
        ),
        "strategist": clean_optional(os.getenv(MODEL_ENV_KEYS["strategist"])) or "",
        "natural_writer": clean_optional(os.getenv(MODEL_ENV_KEYS["natural_writer"])) or "",
        "evaluator": clean_optional(os.getenv(MODEL_ENV_KEYS["evaluator"])) or "",
    }


def test_provider_response(store: SQLiteStore) -> tuple[int, dict[str, Any]]:
    provider = load_configured_provider(store)
    if not provider:
        return 400, {"ok": False, "error": MISSING_PROVIDER_MESSAGE}
    routing = resolve_model_routing(store, provider)
    try:
        result = generate_with_provider(
            provider,
            "Provider smoke test. Reply with exactly: OK",
            variants=1,
            model=routing.default,
        )
    except Exception as exc:  # noqa: BLE001 - endpoint should show clear provider failure.
        return 400, {"ok": False, "error": str(exc), "model": routing.default or ""}
    return 200, {
        "ok": True,
        "message": "Provider test succeeded.",
        "model": routing.default or "",
        "sample": (result[0] if result else "")[:120],
    }


def sync_metadata_payload(metadata: SyncMetadata | None) -> dict[str, Any]:
    if not metadata:
        return {
            "last_started_at": None,
            "last_completed_at": None,
            "status": "not_run",
            "summary": {},
            "error_message": None,
        }
    return {
        "last_started_at": metadata.last_started_at,
        "last_completed_at": metadata.last_completed_at,
        "status": metadata.status,
        "summary": metadata.summary,
        "error_message": metadata.error_message,
    }


def configured_llm_model(store: SQLiteStore | None = None) -> str | None:
    return resolve_model_routing(store).default


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
    generation_metadata = queue_generation_metadata(store, item)
    payload = {
        "id": item.id,
        "platform": item.platform,
        "draft_id": item.draft_id,
        "text": item.text,
        "focus": post_summary(item.angle or item.text, max_length=80),
        "topic": item.topic,
        "post_type": item.post_type,
        "angle": item.angle,
        "trend_ids": item.trend_ids,
        "trend_sources": generation_metadata["trend_sources"],
        "trend_used": generation_metadata["trend_used"],
        "quality_mode": generation_metadata["quality_mode"],
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


def queue_generation_metadata(store: SQLiteStore, item: ContentQueueItem) -> dict[str, Any]:
    quality_mode = ""
    trend_sources: list[dict[str, str]] = []
    draft = store.get_generated_draft(item.draft_id) if item.draft_id else None
    snapshot = draft.context_snapshot if draft else {}
    if isinstance(snapshot, dict):
        quality_mode = str(snapshot.get("quality_mode") or "")
        selected_trends = snapshot.get("selected_trends") or []
        trend_sources = [
            {
                "id": str(trend.get("id") or ""),
                "title": str(trend.get("title") or ""),
                "topic": str(trend.get("topic") or ""),
                "source_type": str(trend.get("source_type") or ""),
                "urgency": str(trend.get("urgency") or ""),
            }
            for trend in selected_trends
            if isinstance(trend, dict) and trend.get("id")
        ]
    if not trend_sources and item.trend_ids:
        for trend_id in item.trend_ids:
            trend = store.get_trend_item(trend_id)
            if trend:
                trend_sources.append(
                    {
                        "id": trend.id,
                        "title": trend.title,
                        "topic": trend.topic,
                        "source_type": trend.source_type,
                        "urgency": trend.urgency,
                    }
                )
    return {
        "quality_mode": quality_mode,
        "trend_sources": trend_sources,
        "trend_used": ", ".join(trend["title"] for trend in trend_sources if trend.get("title")),
    }


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


def pattern_groups_payload(groups: dict[str, list[ViralPattern]]) -> dict[str, Any]:
    return {
        "daily": [pattern_payload(pattern) for pattern in groups.get("daily", [])],
        "weekly": [pattern_payload(pattern) for pattern in groups.get("weekly", [])],
    }


def pattern_payload(pattern: ViralPattern) -> dict[str, Any]:
    return {
        "id": pattern.id,
        "platform": pattern.platform,
        "source_url": pattern.source_url,
        "source_author": pattern.source_author,
        "source_text": pattern.source_text,
        "source_summary": pattern.source_summary,
        "topic": pattern.topic,
        "category": pattern.category,
        "pattern_name": pattern.pattern_name,
        "hook_style": pattern.hook_style,
        "structure": pattern.structure,
        "pacing": pattern.pacing,
        "tone": pattern.tone,
        "formatting": pattern.formatting,
        "why_it_worked": pattern.why_it_worked,
        "adaptation_rule": pattern.adaptation_rule,
        "engagement_notes": pattern.engagement_notes,
        "metrics": {
            "likes": pattern.likes,
            "replies": pattern.replies,
            "reposts": pattern.reposts,
            "quotes": pattern.quotes,
            "bookmarks": pattern.bookmarks,
            "impressions": pattern.impressions,
        },
        "collected_at": pattern.collected_at,
        "window": pattern.window,
        "status": pattern.status,
        "raw_text": pattern.raw_text,
        "created_at": pattern.created_at,
        "updated_at": pattern.updated_at,
    }


def post_payload(post) -> dict[str, Any]:
    return {
        "id": post.id,
        "text": post.text,
        "summary": post_summary(post.text),
        "url": post.url,
        "published_at": post.published_at,
        "author": post.author,
        "topic": post.topic,
        "post_type": post.post_type,
        "status": post.status,
    }


def recent_post_payload(post, metrics: Any | None, note: PostNote | None = None) -> dict[str, Any]:
    payload = post_payload(post)
    payload["metrics"] = metrics_payload(metrics)
    payload["voice"] = voice_note_payload(note)
    return payload


def voice_note_payload(note: PostNote | None) -> dict[str, Any]:
    return {
        "is_voice_example": bool(note.imitate) if note else False,
        "why_this_worked": note.worked_reason if note else None,
        "hook": note.hook if note else None,
        "structure": note.structure if note else None,
        "tone": note.tone if note else None,
        "cta": note.cta if note else None,
        "formatting": note.formatting if note else None,
        "pattern": note.pattern_name if note else None,
        "updated_at": note.updated_at if note else None,
    }


def metrics_payload(metrics: Any | None) -> dict[str, Any]:
    if not metrics:
        return {
            "impressions": None,
            "likes": None,
            "replies": None,
            "reposts": None,
            "quotes": None,
            "bookmarks": None,
            "engagement_rate": None,
            "measured_at": None,
        }
    return {
        "impressions": metrics.impressions,
        "likes": metrics.likes,
        "replies": metrics.replies,
        "reposts": metrics.reposts,
        "quotes": metrics.quotes,
        "bookmarks": metrics.bookmarks,
        "engagement_rate": metrics.engagement_rate,
        "measured_at": metrics.measured_at,
    }


def post_summary(text: str, *, max_length: int = 96) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return "Untitled post"
    first_sentence = cleaned.split(". ", 1)[0].strip()
    summary = first_sentence if len(first_sentence) >= 18 else cleaned
    if len(summary) <= max_length:
        return summary
    return f"{summary[: max_length - 1].rstrip()}..."


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


def trend_groups_payload(groups) -> dict[str, list[dict[str, Any]]]:
    return {
        "daily": [trend_payload(trend) for trend in groups.daily],
        "weekly": [trend_payload(trend) for trend in groups.weekly],
    }


def trend_refresh_payload(groups) -> dict[str, Any]:
    return {
        "trend_groups": trend_groups_payload(groups),
        "errors": list(getattr(groups, "errors", []) or []),
        "sources": list(getattr(groups, "sources", []) or []),
    }


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


def bool_from_payload(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def string_list_payload(value: object) -> list[str]:
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := clean_optional(item))]
    cleaned = clean_optional(value)
    return [cleaned] if cleaned else []


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


def parse_optional_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_dt(value)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
