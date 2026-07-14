from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
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
from leadpoet_social_brain.assistant_context import (
    ASSISTANT_MISSING_PROVIDER_MESSAGE,
    assistant_sources,
    assistant_suggested_actions,
    build_assistant_context,
    build_assistant_prompt,
    starter_prompts,
)
from leadpoet_social_brain.categorization import XPostLLMCategorizer
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.content_queue import (
    accept_queued_post_version,
    approve_queue_item,
    create_post_x,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    resolve_queue_edit_state,
    revise_queued_post,
    weekly_report_text,
)
from leadpoet_social_brain.core.models import (
    ContextSection,
    ContentQueueItem,
    DEFAULT_WORKSPACE_ID,
    LeadMagnet,
    LeadMagnetSource,
    LeadMagnetVersion,
    PostNote,
    QueuedPostVersion,
    SocialPost,
    SyncMetadata,
    TopicIdea,
    TrendItem,
    ViralPattern,
    WebAuthUser,
    new_id,
    utc_now,
)
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator, parse_json_object
from leadpoet_social_brain.lead_magnet_scout import research_lead_magnets
from leadpoet_social_brain.lead_magnet_suggestions import (
    LeadMagnetSuggestion,
    build_lead_magnet_suggestions,
)
from leadpoet_social_brain.lead_magnets import (
    approve_lead_magnet,
    archive_lead_magnet,
    generate_lead_magnet,
    render_lead_magnet_markdown,
    revise_lead_magnet,
)
from leadpoet_social_brain.notion_publisher import NotionPublishError, publish_to_notion
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
from leadpoet_social_brain.skills import load_skill
from leadpoet_social_brain.storage.sqlite_store import (
    DEFAULT_DB_PATH,
    DEFAULT_WEB_AUTH_USERNAME,
    SQLiteStore,
    resolve_db_path,
)
from leadpoet_social_brain.trend_scout import (
    DEFAULT_WEB_TRENDS_PER_WINDOW,
    refresh_web_trends,
    scrapingdog_configured,
    web_trend_groups,
)
from leadpoet_social_brain.topic_scout import DEFAULT_TOPIC_IDEA_LIMIT, refresh_topic_ideas
from leadpoet_social_brain.voice import (
    DEFAULT_AVOID_PHRASES,
    DEFAULT_VOICE_RULES,
    load_avoid_phrases,
    load_positioning_rules,
    load_review_rules,
    load_voice_rules_override,
    save_avoid_phrases,
    save_positioning_rules,
    save_review_rules,
    save_voice_rules_override,
)


STATIC_DIR = Path(__file__).with_name("static")
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
AUTH_COOKIE_SECURE_ENV = "SOCIAL_BRAIN_AUTH_COOKIE_SECURE"
TASK_TOKEN_ENV = "SOCIAL_BRAIN_TASK_TOKEN"
TASK_SECRET_ENV = "TASK_SECRET"
LLM_MODEL_SETTING = MODEL_SETTING_KEYS["default"]
DEFAULT_AUTH_USERNAME = DEFAULT_WEB_AUTH_USERNAME
DEFAULT_X_SYNC_USERNAME = "gavinzaentz"
DEFAULT_X_POST_SYNC_LIMIT = 3200
DEFAULT_DASHBOARD_POST_LIMIT = 250
DAILY_X_POST_SYNC_LIMIT = 25
X_POST_INCREMENTAL_LOOKBACK_MINUTES = 10
X_POSTS_SYNC_KEY = "x_posts_sync"
X_METRICS_SYNC_KEY = "x_metrics_sync"
X_DAILY_SYNC_KEY = "x_daily_sync"
TRENDS_SYNC_KEY = "trends_sync"
PATTERNS_SYNC_KEY = "patterns_sync"
TOPICS_SYNC_KEY = "topics_sync"
AUTOPILOT_SYNC_KEY = "autopilot_weekly"
LEAD_MAGNET_RESEARCH_SYNC_KEY = "lead_magnet_research_sync"
LEAD_MAGNET_AUTOPILOT_SYNC_KEY = "lead_magnet_autopilot"
VOICE_CONTEXT_NAME = "learned_from_high_performers"
FACT_CONTEXT_FIELDS = [
    {
        "kind": "writer",
        "name": "writer_profile",
        "title": "Writer / founder",
        "description": "Background, point of view, lived experience, and credible lanes.",
        "weight": 2.0,
    },
    {
        "kind": "product",
        "name": "company_product",
        "title": "Company / product",
        "description": "What the company/product does, who it helps, and what is true today.",
        "weight": 1.8,
    },
    {
        "kind": "audience",
        "name": "target_audience",
        "title": "Audience / ICP",
        "description": "Who posts should be written for and what they care about.",
        "weight": 1.6,
    },
    {
        "kind": "positioning",
        "name": "core_positioning",
        "title": "Positioning",
        "description": "Core beliefs, wedge, enemy, category, and differentiated point of view.",
        "weight": 1.7,
    },
    {
        "kind": "proof",
        "name": "proof_points",
        "title": "Proof points",
        "description": "Specific claims, examples, metrics, customers, launches, or evidence allowed in posts.",
        "weight": 1.4,
    },
    {
        "kind": "priority",
        "name": "current_priorities",
        "title": "Current priorities",
        "description": "What the account should emphasize right now.",
        "weight": 1.3,
    },
    {
        "kind": "guardrail",
        "name": "fact_guardrails",
        "title": "No-go facts / language",
        "description": "Claims, phrases, or positioning the writer should avoid.",
        "weight": 1.2,
    },
]
FACT_CONTEXT_KINDS = {str(field["kind"]) for field in FACT_CONTEXT_FIELDS} | {
    "bittensor",
    "sales_llm_vision",
}
DEFAULT_TOPIC_OPTIONS = [
    "company/product",
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
SUGGEST_NOTE_MISSING_PROVIDER_MESSAGE = "No LLM provider configured. Set a provider before suggesting notes."
VOICE_AUTOFILL_MISSING_PROVIDER_MESSAGE = "No LLM provider configured. Set a provider before auto-filling Voice details."


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
                status, payload = handle_api_request(
                    method,
                    self.path,
                    body,
                    db_path=db_path,
                    workspace_id=request_workspace_id(self.headers, db_path),
                )
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


def session_payload_from_cookie_value(value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    payload_text, signature = value.rsplit(".", 1)
    try:
        padded = payload_text + ("=" * (-len(payload_text) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["_payload_text"] = payload_text
    payload["_signature"] = signature
    return payload


def session_user_from_cookie_value(store: SQLiteStore, value: str | None) -> WebAuthUser | None:
    payload = session_payload_from_cookie_value(value)
    if not payload:
        return None
    user = store.get_web_auth_user(str(payload.get("u") or ""))
    if not user:
        return None
    payload_text = str(payload.get("_payload_text") or "")
    signature = str(payload.get("_signature") or "")
    expected = session_signature(payload_text, user.password_hash)
    if not hmac.compare_digest(signature, expected):
        return None
    if (
        payload.get("u") == user.username
        and isinstance(payload.get("e"), int)
        and payload["e"] >= int(time.time())
    ):
        return user
    return None


def validate_session_cookie_value(store: SQLiteStore, value: str | None) -> bool:
    return session_user_from_cookie_value(store, value) is not None


def request_is_authenticated(headers: Mapping[str, str], store: SQLiteStore) -> bool:
    return request_authenticated_user(headers, store) is not None


def request_authenticated_user(headers: Mapping[str, str], store: SQLiteStore) -> WebAuthUser | None:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except CookieError:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return session_user_from_cookie_value(store, morsel.value if morsel else None)


def request_workspace_id(headers: Mapping[str, str], db_path: str | Path | None) -> str | None:
    store = SQLiteStore(db_path)
    try:
        user = request_authenticated_user(headers, store)
        return user.workspace_id if user else None
    finally:
        store.close()


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
    workspace_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    payload = parse_json_body(body)
    store = SQLiteStore(db_path, workspace_id=workspace_id)
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
    if method == "POST" and parts == ["tasks", "publish-due-x"]:
        return 200, publish_due_payload(store, payload)
    if method == "POST" and parts == ["tasks", "refresh-trends"]:
        return 200, refresh_trends_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "refresh-patterns"]:
        return 200, refresh_patterns_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "refresh-patterns-weekly"]:
        return 200, refresh_pattern_windows_task_payload(store, payload, windows=["weekly", "monthly"])
    if method == "POST" and parts == ["tasks", "refresh-topics"]:
        return 200, refresh_topics_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "autopilot-weekly"]:
        return 200, autopilot_weekly_task_payload(store, payload)
    if method == "POST" and parts == ["tasks", "autopilot-weekly-status"]:
        return 200, autopilot_weekly_status_payload(store, payload)
    if method == "POST" and parts == ["tasks", "autopilot-lead-magnet"]:
        return 200, autopilot_lead_magnet_task_payload(store, payload)
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
    if method == "GET" and parts == ["api", "assistant", "starter-prompts"]:
        return 200, {"items": starter_prompts()}
    if method == "POST" and parts == ["api", "assistant", "chat"]:
        return assistant_chat_response(store, payload)
    if method == "GET" and parts == ["api", "voice", "status"]:
        return 200, voice_status_payload(store)
    if method == "GET" and parts == ["api", "voice", "current"]:
        return 200, voice_current_payload(store)
    if method == "GET" and parts == ["api", "voice", "examples"]:
        return 200, {"items": voice_examples_payload(store)}
    if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "voice", "examples"] and parts[4] == "suggest-note":
        return suggest_voice_note_response(store, parts[3])
    if method == "GET" and parts == ["api", "voice", "suggestions"]:
        return 200, {"items": voice_suggestions_payload(store)}
    if method == "GET" and parts == ["api", "voice", "coverage"]:
        return 200, voice_coverage_payload(store)
    if method == "GET" and parts == ["api", "voice", "facts"]:
        return 200, context_facts_payload(store)
    if method == "PATCH" and parts == ["api", "voice", "facts"]:
        return 200, save_context_facts_payload(store, payload)
    if method == "GET" and parts == ["api", "voice", "rules"]:
        return 200, voice_rules_payload(store)
    if method == "PATCH" and parts == ["api", "voice", "rules"]:
        return 200, save_voice_rules_payload(store, payload)
    if method == "GET" and parts == ["api", "voice", "avoid-phrases"]:
        return 200, {"items": load_avoid_phrases(store), "defaults": DEFAULT_AVOID_PHRASES}
    if method == "POST" and parts == ["api", "voice", "avoid-phrases"]:
        return 200, {"items": save_avoid_phrases(store, phrase_list_payload(payload))}
    if method == "GET" and parts == ["api", "taxonomy", "topics"]:
        return 200, {"items": taxonomy_options_payload(store, "topic", DEFAULT_TOPIC_OPTIONS)}
    if method == "GET" and parts == ["api", "taxonomy", "post-types"]:
        return 200, {"items": taxonomy_options_payload(store, "post_type", DEFAULT_POST_TYPE_OPTIONS)}
    if method == "POST" and parts == ["api", "voice", "update"]:
        limit = int(payload.get("limit") or 12)
        autofill = autofill_voice_example_details(store)
        section = ContextManager(store).rebuild_style_guide_from_examples(limit=limit)
        status = voice_status_payload(store)
        return 200, {
            "message": voice_update_message(autofill),
            "examples_used": status["examples_count"],
            "auto_filled": autofill,
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
    if method == "GET" and parts == ["api", "topics"]:
        status = one(query, "status") or "suggested"
        ideas = store.list_topic_ideas(status=None if status == "all" else status, limit=int(one(query, "limit") or 50))
        return 200, {"items": [topic_idea_payload(idea) for idea in ideas]}
    if method == "POST" and parts == ["api", "topics", "refresh"]:
        return 200, refresh_topics_task_payload(store, payload)
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "topics"] and parts[3] == "dismiss":
        idea = store.mark_topic_idea_status(parts[2], "dismissed")
        if not idea:
            return 404, {"error": "Topic idea not found."}
        return 200, {"item": topic_idea_payload(idea)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "patterns"] and parts[3] in {"archive", "ignore", "save", "unsave"}:
        status_by_action = {"archive": "archived", "ignore": "ignored", "save": "saved", "unsave": "active"}
        pattern = store.mark_viral_pattern_status(parts[2], status_by_action[parts[3]])
        if not pattern:
            return 404, {"error": "Pattern not found."}
        return 200, {"pattern": pattern_payload(pattern)}
    if method == "GET" and parts == ["api", "lead-magnets"]:
        items = store.list_lead_magnets(
            status=one(query, "status"),
            format=one(query, "format"),
            limit=int(one(query, "limit") or 50),
        )
        return 200, {"items": [lead_magnet_payload(store, item) for item in items]}
    if method == "GET" and parts == ["api", "lead-magnets", "suggestions"]:
        suggestions = lead_magnet_suggestions_for_store(
            store,
            limit=max(1, min(int(one(query, "limit") or 3), 6)),
        )
        return 200, {
            "items": [
                lead_magnet_suggestion_payload(suggestion, recommended=index == 0)
                for index, suggestion in enumerate(suggestions)
            ],
            "recommended_id": suggestions[0].id if suggestions else None,
            "message": (
                "Choose a research-backed direction or generate the recommended draft."
                if suggestions
                else "Research recent winners to unlock evidence-backed directions."
            ),
        }
    if method == "POST" and parts == ["api", "lead-magnets"]:
        provider = load_configured_provider(store)
        result = generate_lead_magnet(
            store,
            provider,
            audience=str(payload.get("audience") or ""),
            sales_outcome=str(payload.get("sales_outcome") or payload.get("sales_problem") or ""),
            format=str(payload.get("format") or "prompt_library"),
            depth=str(payload.get("depth") or "standard"),
            product_integration=str(payload.get("product_integration") or "embedded"),
            title=clean_optional(payload.get("title")),
            cta=clean_optional(payload.get("cta")),
            instruction=clean_optional(payload.get("instruction") or payload.get("instructions")),
            source_ids=string_list_payload(payload.get("source_ids")) if "source_ids" in payload else None,
            research_run_id=clean_optional(payload.get("research_run_id")),
            evidence_summary=clean_optional(payload.get("evidence_summary")),
        )
        return 200, {
            "item": lead_magnet_payload(store, result.item, include_detail=True),
            "version": lead_magnet_version_payload(result.version),
            "warnings": result.warnings,
        }
    if method == "POST" and parts == ["api", "lead-magnets", "research"]:
        provider_warnings: list[str] = []
        try:
            provider = load_configured_provider(store)
        except Exception as exc:  # noqa: BLE001 - deterministic research does not require AI classification.
            provider = None
            provider_warnings.append(f"AI classification was unavailable; deterministic research continued: {exc}")
        candidates = payload.get("candidates")
        if candidates is not None and not isinstance(candidates, list):
            raise ValueError("candidates must be an array when provided.")
        result = research_lead_magnets(
            store,
            provider,
            candidates=[row for row in candidates if isinstance(row, dict)] if isinstance(candidates, list) else None,
            window_days=int(payload.get("window_days") or 30),
            limit=int(payload.get("limit") or 20),
            query=clean_optional(payload.get("query")),
            initial_warnings=provider_warnings,
        )
        save_lead_magnet_research_sync(store, result)
        return 200, lead_magnet_research_result_payload(result)
    if method == "GET" and parts == ["api", "lead-magnets", "sources"]:
        sources = store.list_lead_magnet_sources(
            status=one(query, "status"),
            platform=one(query, "platform"),
            format=one(query, "format"),
            published_after=one(query, "published_after"),
            limit=int(one(query, "limit") or 100),
        )
        return 200, {"items": [lead_magnet_source_payload(source) for source in sources]}
    if method == "POST" and parts == ["api", "lead-magnets", "sources"]:
        candidate = dict(payload)
        if not candidate.get("published_at"):
            raise ValueError("published_at is required so the rolling research window can be verified.")
        provider_warnings: list[str] = []
        try:
            provider = load_configured_provider(store)
        except Exception as exc:  # noqa: BLE001 - manual evidence intake should remain available.
            provider = None
            provider_warnings.append(f"AI classification was unavailable; deterministic research continued: {exc}")
        result = research_lead_magnets(
            store,
            provider,
            candidates=[candidate],
            window_days=30,
            limit=1,
            initial_warnings=provider_warnings,
        )
        save_lead_magnet_research_sync(store, result)
        return 200, lead_magnet_research_result_payload(result)
    if method == "GET" and parts == ["api", "lead-magnets", "research-runs"]:
        runs = store.list_lead_magnet_research_runs(
            status=one(query, "status"),
            limit=int(one(query, "limit") or 50),
        )
        return 200, {"items": [lead_magnet_research_run_payload(run) for run in runs]}
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "lead-magnets"]:
        item = store.get_lead_magnet(parts[2])
        if not item:
            return 404, {"error": "Lead magnet not found."}
        return 200, {"item": lead_magnet_payload(store, item, include_detail=True)}
    if method == "PATCH" and len(parts) == 3 and parts[:2] == ["api", "lead-magnets"]:
        item = update_lead_magnet_from_payload(store, parts[2], payload)
        return 200, {"item": lead_magnet_payload(store, item, include_detail=True)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "lead-magnets"] and parts[3] == "revise":
        provider = load_configured_provider(store)
        result = revise_lead_magnet(
            store,
            provider,
            lead_magnet_id=parts[2],
            instruction=str(payload.get("instruction") or ""),
        )
        return 200, {
            "item": lead_magnet_payload(store, result.item, include_detail=True),
            "version": lead_magnet_version_payload(result.version),
            "warnings": result.warnings,
        }
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "lead-magnets"] and parts[3] == "approve":
        item = approve_lead_magnet(store, parts[2])
        return 200, {"item": lead_magnet_payload(store, item, include_detail=True)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "lead-magnets"] and parts[3] == "archive":
        item = archive_lead_magnet(store, parts[2])
        return 200, {"item": lead_magnet_payload(store, item, include_detail=True)}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "lead-magnets"] and parts[3] == "publish":
        item = store.get_lead_magnet(parts[2])
        if not item:
            return 404, {"error": "Lead magnet not found."}
        if item.status not in {"approved", "published"}:
            raise ValueError("Approve the lead magnet before publishing it to Notion.")
        try:
            published = publish_to_notion(
                item.content,
                title=item.title,
                existing_page_id=item.notion_page_id,
                icon="🧲",
            )
        except Exception as exc:  # noqa: BLE001 - retain the approved local artifact and retry receipt.
            partial_page_id = exc.page_id if isinstance(exc, NotionPublishError) else item.notion_page_id
            failed = store.save_lead_magnet(
                replace(
                    item,
                    status="approved",
                    notion_page_id=partial_page_id,
                    failure_reason=str(exc),
                    updated_at=utc_now(),
                )
            )
            return 502, {
                "error": str(exc),
                "item": lead_magnet_payload(store, failed, include_detail=True),
                "retryable": True,
            }
        saved = store.save_lead_magnet(
            replace(
                item,
                status="published",
                notion_page_id=published.page_id,
                notion_url=published.public_url or published.url,
                published_at=utc_now(),
                failure_reason=None,
                updated_at=utc_now(),
            )
        )
        return 200, {
            "item": lead_magnet_payload(store, saved, include_detail=True),
            "publish": {
                "page_id": published.page_id,
                "url": published.public_url or published.url,
                "created": published.created,
                "block_count": published.block_count,
                "batch_count": published.batch_count,
                "content_hash": published.content_hash,
            },
        }
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
        result = post_queue_item_x(store, client, item=item, author=str(payload.get("author") or "workspace"))
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
    if method == "POST" and parts == ["api", "content", "create-post"]:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(CREATE_POST_MISSING_PROVIDER_MESSAGE)
        result = create_post_x(
            store,
            SocialBrainGenerator(store, provider=provider),
            instruction=str(payload.get("instruction") or ""),
            topic=str(payload.get("topic") or "uncategorized"),
            post_type=str(payload.get("post_type") or "founder_insight"),
            use_trends=bool_from_payload(payload, "use_trends", False),
            scheduled_at=clean_optional(payload.get("scheduled_at")),
            extra_context=clean_optional(payload.get("extra_context")),
            use_patterns=bool_from_payload(payload, "use_patterns", False),
            pattern_ids=string_list_payload(payload.get("pattern_ids")),
            trend_ids=string_list_payload(payload.get("trend_ids")),
            length_mode=str(payload.get("length_mode") or "standard"),
            topic_idea_id=clean_optional(payload.get("topic_idea_id")),
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
        posts = int(payload.get("posts") or 5)
        autopilot = bool_from_payload(payload, "autopilot", False)
        topic_ideas: list[TopicIdea] | None = None
        topic_warnings: list[str] = []
        if autopilot:
            available = store.list_topic_ideas(status="suggested", limit=posts)
            if len(available) < posts or bool_from_payload(payload, "refresh_topics", True):
                topic_result = refresh_topic_ideas(
                    store,
                    provider,
                    limit=max(posts, int(payload.get("topic_limit") or DEFAULT_TOPIC_IDEA_LIMIT)),
                )
                available = topic_result.ideas
                topic_warnings.extend(topic_result.warnings)
            topic_ideas = available[:posts]
        result = plan_week_x(
            store,
            generator,
            week_start=str(payload.get("week_start") or next_monday()),
            posts=posts,
            use_trends=bool(payload.get("use_trends", False)),
            trend_window_days=int(payload.get("trend_window_days") or 14),
            topic_mix=clean_optional(payload.get("topic_mix")),
            cadence=str(payload.get("cadence") or "weekday_mornings"),
            default_post_type=str(payload.get("default_post_type") or "founder_insight"),
            variants_per_slot=int(payload.get("variants_per_slot") or 1),
            allow_evergreen=bool(payload.get("allow_evergreen", True)),
            use_patterns=bool(payload.get("use_patterns", False)),
            length_mode=str(payload.get("length_mode") or "standard"),
            topic_ideas=topic_ideas,
            extra_instruction=clean_optional(payload.get("instruction")),
            continue_on_generation_error=autopilot,
        )
        return 200, {
            "queued": [queue_item_payload(store, item) for item in result.queued],
            "warnings": [*topic_warnings, *result.warnings],
            "autopilot": autopilot,
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
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "trends"]:
        trend = store.get_trend_item(parts[2])
        if not trend:
            return 404, {"error": "Trend not found."}
        return 200, {"item": trend_payload(trend, include_raw=one(query, "include_raw") == "true")}
    if method == "POST" and parts == ["api", "trends", "refresh"]:
        try:
            return 200, refresh_trends_task_payload(store, payload)
        except Exception as exc:  # noqa: BLE001 - web trend sources can fail independently of the app.
            return 502, {"error": f"Could not refresh web trends: {exc}"}
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
        author=str(payload.get("author") or "workspace"),
    )
    errors = [result.error for result in results if result.error]
    return {"results": [publish_result_payload(result) for result in results], "errors": errors}


def assistant_chat_response(store: SQLiteStore, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    message = clean_optional(payload.get("message"))
    if not message:
        return 400, {"error": "message is required."}
    provider = load_configured_provider(store)
    if not provider:
        return 400, {"error": ASSISTANT_MISSING_PROVIDER_MESSAGE}
    context = build_assistant_context(store, message)
    prompt = build_assistant_prompt(message, context)
    try:
        answers = generate_with_provider(
            provider,
            prompt,
            variants=1,
            model=resolve_model_routing(store, provider).default,
        )
    except Exception as exc:  # noqa: BLE001 - provider failures should be visible in the drawer.
        return 400, {"error": str(exc)}
    answer = clean_optional(answers[0] if answers else None)
    if not answer:
        return 400, {"error": "Provider returned an empty assistant answer."}
    return 200, {
        "answer": answer,
        "suggested_actions": assistant_suggested_actions(message, context),
        "sources": assistant_sources(context),
        "conversation_id": clean_optional(payload.get("conversation_id")),
    }


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
        limit = int(payload.get("limit") or DEFAULT_WEB_TRENDS_PER_WINDOW)
        no_enrich = bool_from_payload(payload, "no_enrich", False)
        enrich_limit = int(payload.get("enrich_limit") or limit)
        scrapingdog_timeout_seconds = optional_int(payload.get("scrapingdog_timeout_seconds"))
        max_news_queries_per_window = optional_int(payload.get("max_news_queries_per_window"))
        deadline_seconds = optional_int(payload.get("deadline_seconds"))
        if scrapingdog_timeout_seconds is None:
            scrapingdog_timeout_seconds = 45
        if max_news_queries_per_window is None:
            max_news_queries_per_window = 6
        provider = None
        provider_warning = ""
        if not no_enrich:
            try:
                provider = load_configured_provider(store)
            except Exception as exc:  # noqa: BLE001 - provider-backed digest should not block trend storage.
                provider_warning = f"Trend enrichment provider unavailable: {exc}"
        groups = call_refresh_web_trends(
            store,
            limit=limit,
            provider=provider,
            enrich=not no_enrich,
            enrich_limit=enrich_limit,
            scrapingdog_timeout_seconds=scrapingdog_timeout_seconds,
            max_news_queries_per_window=max_news_queries_per_window,
            deadline_seconds=deadline_seconds,
        )
        completed_at = utc_now()
        errors = list(groups.errors)
        if provider_warning:
            errors.append(provider_warning)
        summary = {
            "daily_trends": len(groups.daily),
            "weekly_trends": len(groups.weekly),
            "candidates_found": groups.candidates_found,
            "trends_stored": groups.trends_stored,
            "enriched": groups.enriched,
            "partial": groups.partial,
            "failed": groups.failed,
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
            "candidates_found": 0,
            "trends_stored": 0,
            "enriched": 0,
            "partial": 0,
            "failed": 0,
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


def call_refresh_web_trends(
    store: SQLiteStore,
    *,
    limit: int,
    provider: Any | None,
    enrich: bool,
    enrich_limit: int,
    scrapingdog_timeout_seconds: int,
    max_news_queries_per_window: int,
    deadline_seconds: int | None,
):
    try:
        return refresh_web_trends(
            store,
            limit=limit,
            provider=provider,
            enrich=enrich,
            enrich_limit=enrich_limit,
            scrapingdog_timeout_seconds=scrapingdog_timeout_seconds,
            max_news_queries_per_window=max_news_queries_per_window,
            deadline_seconds=deadline_seconds,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return refresh_web_trends(store)


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
            window=str(payload.get("window") or "weekly"),
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


def refresh_pattern_windows_task_payload(store: SQLiteStore, payload: dict[str, Any], *, windows: list[str]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(PATTERN_REFRESH_MISSING_PROVIDER)
        limit = int(payload.get("limit") or 5)
        window_summaries: dict[str, Any] = {}
        all_patterns: list[dict[str, Any]] = []
        errors: list[str] = []
        fetched = 0
        stored = 0
        skipped = 0
        for window in windows:
            result = refresh_patterns(store, provider, limit=limit, window=window)
            fetched += result.fetched
            stored += result.stored
            skipped += result.skipped
            errors.extend(result.errors)
            all_patterns.extend(pattern_payload(pattern) for pattern in result.patterns)
            window_summaries[window] = {
                "fetched": result.fetched,
                "stored": result.stored,
                "skipped": result.skipped,
                "errors": result.errors,
            }
        completed_at = utc_now()
        summary = {
            "fetched": fetched,
            "stored": stored,
            "skipped": skipped,
            "errors": errors,
            "completed_at": completed_at,
            "windows": window_summaries,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=PATTERNS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if errors else "success",
                summary=summary,
                error_message="\n".join(errors[:5]) if errors else None,
            )
        )
        return {
            **summary,
            "patterns": all_patterns,
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


def refresh_topics_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError("No LLM provider configured. Set a provider before refreshing topics.")
        result = refresh_topic_ideas(
            store,
            provider,
            limit=int(payload.get("limit") or DEFAULT_TOPIC_IDEA_LIMIT),
        )
        completed_at = utc_now()
        summary = {
            "stored": len(result.ideas),
            "warnings": result.warnings,
            "diagnostics": result.diagnostics,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=TOPICS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if result.warnings else "success",
                summary=summary,
                error_message="\n".join(result.warnings[:5]) if result.warnings else None,
            )
        )
        return {
            **summary,
            "items": [topic_idea_payload(idea) for idea in result.ideas],
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - task persists provider failures for account status.
        completed_at = utc_now()
        store.save_sync_metadata(
            SyncMetadata(
                key=TOPICS_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary={"stored": 0, "warnings": [str(exc)], "completed_at": completed_at},
                error_message=str(exc),
            )
        )
        raise


def autopilot_weekly_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    started_at = utc_now()
    try:
        week_start = str(payload.get("week_start") or next_monday())
        start_date = date.fromisoformat(week_start)
        posts = max(1, int(payload.get("posts") or 5))
        cadence = str(payload.get("cadence") or "weekday_mornings")
        existing = [
            item
            for item in store.list_content_queue(platform="x", limit=500)
            if item.scheduled_at
            and start_date <= parse_dt(item.scheduled_at).astimezone(timezone.utc).date() < start_date + timedelta(days=7)
            and item.status not in {"skipped", "failed"}
        ]
        if len(existing) >= posts:
            completed_at = utc_now()
            summary = {
                "skipped": True,
                "reason": f"The target is already met with {len(existing)} queue item(s) for this week.",
                "queued": 0,
                "existing": len(existing),
                "target": posts,
                "completed_at": completed_at,
            }
            metadata = store.save_sync_metadata(
                SyncMetadata(
                    key=AUTOPILOT_SYNC_KEY,
                    last_started_at=started_at,
                    last_completed_at=completed_at,
                    status="success",
                    summary=summary,
                )
            )
            return {**summary, "items": [], "sync": sync_metadata_payload(metadata)}

        remaining_posts = posts - len(existing)
        occupied_schedule_dates = {
            parse_dt(item.scheduled_at).date()
            for item in existing
            if item.scheduled_at
        }
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError(MISSING_PROVIDER_MESSAGE)
        topic_result = refresh_topic_ideas(
            store,
            provider,
            limit=max(remaining_posts, int(payload.get("topic_limit") or DEFAULT_TOPIC_IDEA_LIMIT)),
        )
        result = plan_week_x(
            store,
            SocialBrainGenerator(store, provider=provider),
            week_start=week_start,
            posts=remaining_posts,
            use_trends=bool_from_payload(payload, "use_trends", True),
            trend_window_days=int(payload.get("trend_window_days") or 14),
            topic_mix=clean_optional(payload.get("topic_mix")),
            cadence=cadence,
            default_post_type=str(payload.get("default_post_type") or "founder_insight"),
            variants_per_slot=1,
            allow_evergreen=True,
            use_patterns=bool_from_payload(payload, "use_patterns", True),
            length_mode=str(payload.get("length_mode") or "standard"),
            topic_ideas=topic_result.ideas,
            extra_instruction=clean_optional(payload.get("instruction")),
            continue_on_generation_error=True,
            occupied_schedule_dates=occupied_schedule_dates,
            generation_attempt_limit=remaining_posts + 2,
        )
        completed_at = utc_now()
        warnings = [*topic_result.warnings, *result.warnings]
        summary = {
            "skipped": False,
            "queued": len(result.queued),
            "existing": len(existing),
            "target": posts,
            "topics_generated": len(topic_result.ideas),
            "warnings": warnings,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=AUTOPILOT_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if warnings else "success",
                summary=summary,
                error_message="\n".join(warnings[:5]) if warnings else None,
            )
        )
        return {
            **summary,
            "items": [queue_item_payload(store, item) for item in result.queued],
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - task persists failures for the next cron run.
        completed_at = utc_now()
        store.save_sync_metadata(
            SyncMetadata(
                key=AUTOPILOT_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary={"queued": 0, "warnings": [str(exc)], "completed_at": completed_at},
                error_message=str(exc),
            )
        )
        raise


def autopilot_weekly_status_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    week_start = str(payload.get("week_start") or next_monday())
    start_date = date.fromisoformat(week_start)
    items = [
        item
        for item in store.list_content_queue(platform="x", limit=500)
        if item.scheduled_at
        and start_date <= parse_dt(item.scheduled_at).astimezone(timezone.utc).date() < start_date + timedelta(days=7)
        and item.status not in {"skipped", "failed"}
    ]
    return {
        "week_start": week_start,
        "queued": len(items),
        "items": [
            {
                "id": item.id,
                "status": item.status,
                "scheduled_at": item.scheduled_at,
            }
            for item in items
        ],
        "sync": sync_metadata_payload(store.get_sync_metadata(AUTOPILOT_SYNC_KEY)),
    }


def autopilot_lead_magnet_task_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Research and create one review-ready artifact at most once per 14-day period."""

    started_at = utc_now()
    now = parse_dt(clean_optional(payload.get("now")) or started_at).astimezone(timezone.utc)
    base_period_key = lead_magnet_period_key(now)
    force = bool_from_payload(payload, "force", False)
    existing = store.get_lead_magnet_research_run_by_period_key(base_period_key)
    if existing and existing.status in {"running", "completed", "success", "partial"} and not force:
        items = store.list_lead_magnets(research_run_id=existing.id, limit=5)
        completed_at = utc_now()
        summary = {
            "skipped": True,
            "reason": "A lead-magnet run already exists for this 14-day period.",
            "period_key": base_period_key,
            "research_run_id": existing.id,
            "created": len(items),
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=LEAD_MAGNET_AUTOPILOT_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="success",
                summary=summary,
            )
        )
        return {
            **summary,
            "items": [lead_magnet_payload(store, item) for item in items],
            "sync": sync_metadata_payload(metadata),
        }

    period_key = base_period_key if not force else f"{base_period_key}:force:{new_id()[:8]}"
    try:
        provider = load_configured_provider(store)
        if not provider:
            raise ValueError("Lead-magnet autopilot requires a configured LLM provider.")
        candidates = payload.get("candidates")
        if candidates is not None and not isinstance(candidates, list):
            raise ValueError("candidates must be an array when provided.")
        research = research_lead_magnets(
            store,
            provider,
            candidates=[row for row in candidates if isinstance(row, dict)] if isinstance(candidates, list) else None,
            window_days=int(payload.get("window_days") or 30),
            limit=int(payload.get("research_limit") or 20),
            query=clean_optional(payload.get("query")),
            now=now,
            period_key=period_key,
        )
        save_lead_magnet_research_sync(store, research)
        suggestions = build_lead_magnet_suggestions(
            research.sources,
            trends=active_lead_magnet_trends(store),
            topic_ideas=store.list_topic_ideas(status="suggested", limit=20),
            research_run_id=research.run.id,
            limit=3,
        )
        direction = suggestions[0] if suggestions else None
        format = (
            clean_optional(payload.get("format"))
            or (direction.format if direction else None)
            or best_lead_magnet_format(research.sources)
        )
        audience = (
            clean_optional(payload.get("audience"))
            or (direction.audience if direction else None)
            or first_source_value(research.sources, "audience")
            or "sales and GTM operators"
        )
        sales_outcome = (
            clean_optional(payload.get("sales_outcome") or payload.get("sales_problem"))
            or (direction.sales_outcome if direction else None)
            or first_source_value(research.sources, "sales_topic")
            or "turn buyer research into a repeatable, signal-aware sales workflow"
        )
        selected_source_ids = (
            string_list_payload(payload.get("source_ids"))
            if "source_ids" in payload
            else (direction.source_ids if direction else [source.id for source in research.sources])
        )
        generated = generate_lead_magnet(
            store,
            provider,
            audience=audience,
            sales_outcome=sales_outcome,
            format=format,
            depth=str(payload.get("depth") or (direction.depth if direction else "standard")),
            product_integration=str(
                payload.get("product_integration") or (direction.product_integration if direction else "embedded")
            ),
            title=clean_optional(payload.get("title")) or (direction.title if direction else None),
            cta=clean_optional(payload.get("cta")) or (direction.cta if direction else None),
            instruction=clean_optional(payload.get("instruction")) or (direction.instruction if direction else None),
            source_ids=selected_source_ids,
            research_run_id=research.run.id,
            evidence_summary=(direction.evidence_summary if direction else None)
            or (
                f"Rolling {int(payload.get('window_days') or 30)}-day research run; "
                f"{len(research.sources)} qualified source(s)."
            ),
        )
        completed_at = utc_now()
        warnings = [*research.warnings, *generated.warnings]
        summary = {
            "skipped": False,
            "created": 1,
            "period_key": period_key,
            "research_run_id": research.run.id,
            "sources_qualified": len(research.sources),
            "metric_backed_sources": sum(
                1 for source in research.sources if source.evidence_strength == "metric_backed"
            ),
            "lead_magnet_id": generated.item.id,
            "suggestion_id": direction.id if direction else None,
            "suggestion_title": direction.title if direction else None,
            "status": generated.item.status,
            "warnings": warnings,
            "completed_at": completed_at,
        }
        metadata = store.save_sync_metadata(
            SyncMetadata(
                key=LEAD_MAGNET_AUTOPILOT_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="partial" if warnings else "success",
                summary=summary,
                error_message="\n".join(warnings[:5]) if warnings else None,
            )
        )
        return {
            **summary,
            "item": lead_magnet_payload(store, generated.item, include_detail=True),
            "sync": sync_metadata_payload(metadata),
        }
    except Exception as exc:  # noqa: BLE001 - preserve an actionable task receipt.
        completed_at = utc_now()
        store.save_sync_metadata(
            SyncMetadata(
                key=LEAD_MAGNET_AUTOPILOT_SYNC_KEY,
                last_started_at=started_at,
                last_completed_at=completed_at,
                status="failed",
                summary={
                    "skipped": False,
                    "created": 0,
                    "period_key": period_key,
                    "errors": [str(exc)],
                    "completed_at": completed_at,
                },
                error_message=str(exc),
            )
        )
        raise


def lead_magnet_period_key(value: datetime) -> str:
    return f"lead-magnet:{value.date().toordinal() // 14}"


def best_lead_magnet_format(sources: list[LeadMagnetSource]) -> str:
    if not sources:
        return "prompt_library"
    counts = Counter(source.format for source in sources if source.format)
    supported = {"prompt_library", "playbook", "template_pack", "scorecard"}
    ranked = [name for name, _ in counts.most_common() if name in supported]
    return ranked[0] if ranked else "prompt_library"


def first_source_value(sources: list[LeadMagnetSource], field_name: str) -> str | None:
    for source in sources:
        value = clean_optional(getattr(source, field_name, None))
        if value:
            return value
    return None


def active_lead_magnet_trends(store: SQLiteStore, *, limit: int = 20) -> list[TrendItem]:
    trends = store.list_trends(status="unused", limit=limit)
    return trends or store.list_trends(limit=limit)


def lead_magnet_suggestions_for_store(
    store: SQLiteStore,
    *,
    limit: int = 3,
) -> list[LeadMagnetSuggestion]:
    research_run = next(
        (
            run
            for run in store.list_lead_magnet_research_runs(limit=20)
            if run.source_ids and run.status in {"completed", "success", "partial"}
        ),
        None,
    )
    sources = (
        [source for source_id in research_run.source_ids if (source := store.get_lead_magnet_source(source_id))]
        if research_run
        else []
    )
    if not sources:
        sources = store.list_lead_magnet_sources(status="active", limit=20)
    return build_lead_magnet_suggestions(
        sources,
        trends=active_lead_magnet_trends(store),
        topic_ideas=store.list_topic_ideas(status="suggested", limit=20),
        research_run_id=research_run.id if research_run else None,
        limit=limit,
    )


def lead_magnet_suggestion_payload(
    suggestion: LeadMagnetSuggestion,
    *,
    recommended: bool,
) -> dict[str, Any]:
    return {**asdict(suggestion), "recommended": recommended}


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
        username = clean_optional(payload.get("username")) or default_x_sync_username_for_store(store)
        if not username:
            raise ValueError("X username is required for this workspace.")
        result = sync_x_user_posts(
            store,
            XApiClient.from_env(),
            username=username,
            limit=limit,
            default_topic=clean_optional(payload.get("default_topic")) or (
                UNCATEGORIZED_TOPIC if auto_categorize else default_import_topic_for_store(store)
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
        UNCATEGORIZED_TOPIC if auto_categorize else default_import_topic_for_store(store)
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


def default_import_topic_for_store(store: SQLiteStore) -> str:
    return DEFAULT_IMPORT_TOPIC if store.workspace_id == DEFAULT_WORKSPACE_ID else UNCATEGORIZED_TOPIC


def default_x_sync_username_for_store(store: SQLiteStore) -> str:
    return DEFAULT_X_SYNC_USERNAME if store.workspace_id == DEFAULT_WORKSPACE_ID else ""


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
    new_topic = value_or_current(payload, "topic", item.topic)
    new_post_type = value_or_current(payload, "post_type", item.post_type)
    new_angle = value_or_current(payload, "angle", item.angle)
    requested_status = value_or_current(payload, "status", item.status)
    status, approved_at, _ = resolve_queue_edit_state(
        item,
        text=new_text,
        topic=new_topic,
        post_type=new_post_type,
        angle=new_angle,
        requested_status=requested_status,
    )
    if new_text != item.text:
        store.save_queued_post_version(
            QueuedPostVersion(
                queue_id=item.id,
                text=item.text,
                edit_type="manual",
                edit_instruction="previous text before manual edit",
                source_draft_id=item.draft_id,
            )
        )
    updated = ContentQueueItem(
        **{
            **item.__dict__,
            "text": new_text,
            "topic": new_topic,
            "post_type": new_post_type,
            "angle": new_angle,
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
        description = "No Voice Examples yet. Mark a few strong posts to build the Voice profile."
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
        description = "Voice is current."
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
            "title": "Voice Profile",
            "content": "",
            "sections": [],
            "updated_at": None,
            "empty_message": "No Voice yet. Mark a few strong posts as Voice Examples, then update Voice.",
        }
    return {
        "exists": True,
        "title": "Voice Profile",
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
        if post.source_draft_id is None
        and not (notes.get(post.id) and notes[post.id].imitate)
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


def suggest_voice_note_response(store: SQLiteStore, post_id: str) -> tuple[int, dict[str, Any]]:
    post = store.get_post(post_id)
    if not post:
        return 404, {"error": "Post not found."}
    provider = load_configured_provider(store)
    if not provider:
        return 400, {"error": SUGGEST_NOTE_MISSING_PROVIDER_MESSAGE}
    metrics = store.latest_metrics_by_post_id([post.id]).get(post.id)
    current_voice = current_voice_section(store)
    prompt = build_voice_note_prompt(
        post,
        metrics=metrics,
        current_voice=current_voice.content if current_voice else "",
        fact_context=fact_context_prompt_text(store),
    )
    try:
        suggestions = generate_with_provider(
            provider,
            prompt,
            variants=1,
            model=resolve_model_routing(store, provider).default,
        )
    except Exception as exc:  # noqa: BLE001 - show provider errors in the UI.
        return 400, {"error": str(exc)}
    suggested_note = clean_suggested_voice_note(suggestions[0] if suggestions else "")
    if not suggested_note:
        return 400, {"error": "Provider returned an empty suggested note."}
    return 200, {"suggested_note": suggested_note}


def build_voice_note_prompt(
    post: SocialPost,
    *,
    metrics: Any | None,
    current_voice: str = "",
    fact_context: str = "",
) -> str:
    metric = metrics_payload(metrics)
    metrics_text = "\n".join(
        f"- {key}: {value}"
        for key, value in metric.items()
        if value is not None and key != "measured_at"
    ) or "- No metrics available."
    return f"""Suggest a concise "Why this worked" note for this Voice Example.

Post text:
{post.text}

Topic: {post.topic}
Post type: {post.post_type}

Metrics:
{metrics_text}

Current Voice:
{current_voice or "- No current Voice summary yet."}

Facts & Context:
{fact_context or "- No saved facts/context yet."}

Focus on voice, structure, specificity, and why the audience likely responded.
Only reference writer, company, or product identity if it is explicitly present in Facts & Context or the post text.
Avoid generic notes like "This post performed well because it got engagement."
Do not mention Koinoniko; that is only the platform name. Do not use ellipses or unfinished phrases.

Return one concise paragraph, maximum 35 words. Do not include bullets, labels, or quotation marks."""


def clean_suggested_voice_note(value: str) -> str:
    return clean_voice_detail_text(str(value or "").strip().strip('"').strip("'"), 600) or ""


VOICE_AUTOFILL_FIELDS = {
    "why_this_worked": ("worked_reason", 700),
    "hook": ("hook", 260),
    "structure": ("structure", 320),
    "tone": ("tone", 260),
    "cta": ("cta", 180),
    "formatting": ("formatting", 260),
    "pattern": ("pattern_name", 260),
}


def autofill_voice_example_details(store: SQLiteStore) -> dict[str, Any]:
    examples = voice_example_posts(store)
    notes = store.notes_by_post_id([post.id for post in examples])
    candidates = [
        (post, note, missing_voice_detail_fields(note))
        for post in examples
        if (note := notes.get(post.id)) and note.imitate
    ]
    candidates = [(post, note, missing) for post, note, missing in candidates if missing]
    summary: dict[str, Any] = {
        "checked": len(examples),
        "needed": len(candidates),
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "provider_required": bool(candidates),
    }
    if not candidates:
        return summary
    try:
        provider = load_configured_provider(store)
    except Exception as exc:  # noqa: BLE001 - keep Voice rebuild available even if provider setup is bad.
        summary["skipped"] = len(candidates)
        summary["errors"].append(str(exc))
        return summary
    if not provider:
        summary["skipped"] = len(candidates)
        summary["errors"].append(VOICE_AUTOFILL_MISSING_PROVIDER_MESSAGE)
        return summary
    metrics_by_post_id = store.latest_metrics_by_post_id([post.id for post, _, _ in candidates])
    current_voice = current_voice_section(store)
    fact_context = fact_context_prompt_text(store)
    model = resolve_model_routing(store, provider).default
    for post, note, missing in candidates:
        prompt = build_voice_detail_prompt(
            post,
            note=note,
            metrics=metrics_by_post_id.get(post.id),
            current_voice=current_voice.content if current_voice else "",
            fact_context=fact_context,
            missing_fields=missing,
        )
        try:
            raw = generate_with_provider(provider, prompt, variants=1, model=model)[0]
        except Exception as exc:  # noqa: BLE001 - keep filling the rest of the examples.
            summary["skipped"] += 1
            summary["errors"].append(f"{post_summary(post.text, max_length=64)}: {exc}")
            continue
        values = clean_voice_detail_suggestion(raw, missing)
        if not values:
            summary["skipped"] += 1
            summary["errors"].append(f"{post_summary(post.text, max_length=64)}: provider returned no usable Voice details")
            continue
        store.save_note(merged_voice_note(note, values))
        summary["updated"] += 1
    return summary


def voice_update_message(autofill: dict[str, Any]) -> str:
    updated = int(autofill.get("updated") or 0)
    skipped = int(autofill.get("skipped") or 0)
    needed = int(autofill.get("needed") or 0)
    errors = [str(error) for error in autofill.get("errors", []) if str(error)]
    if updated and not skipped and not errors:
        return f"Voice updated and filled details for {updated} Voice Example(s)."
    if updated and (skipped or errors):
        message = f"Voice updated and filled details for {updated} Voice Example(s), but skipped {skipped}."
        return f"{message} {errors[0]}" if errors else message
    if needed and errors:
        return f"Voice updated, but Voice Example details were not filled: {errors[0]}"
    if needed and skipped:
        return f"Voice updated, but {skipped} Voice Example(s) still need details."
    return "Voice updated from selected examples."


def missing_voice_detail_fields(note: PostNote | None) -> list[str]:
    if not note:
        return list(VOICE_AUTOFILL_FIELDS)
    missing: list[str] = []
    for payload_key, (note_attr, _) in VOICE_AUTOFILL_FIELDS.items():
        if clean_optional(getattr(note, note_attr, None)) is None:
            missing.append(payload_key)
    return missing


def build_voice_detail_prompt(
    post: SocialPost,
    *,
    note: PostNote,
    metrics: Any | None,
    current_voice: str = "",
    fact_context: str = "",
    missing_fields: list[str],
) -> str:
    metric = metrics_payload(metrics)
    metrics_text = "\n".join(
        f"- {key}: {value}"
        for key, value in metric.items()
        if value is not None and key != "measured_at"
    ) or "- No metrics available."
    existing = {
        "why_this_worked": note.worked_reason,
        "hook": note.hook,
        "structure": note.structure,
        "tone": note.tone,
        "cta": note.cta,
        "formatting": note.formatting,
        "pattern": note.pattern_name,
    }
    missing_json = json.dumps(missing_fields)
    existing_json = json.dumps({key: value for key, value in existing.items() if clean_optional(value)}, indent=2)
    return f"""Fill missing Voice Example details.

Post text:
{post.text}

Topic: {post.topic}
Post type: {post.post_type}

Metrics:
{metrics_text}

Existing details:
{existing_json or "{}"}

Current Voice:
{current_voice or "- No current Voice summary yet."}

Facts & Context:
{fact_context or "- No saved facts/context yet."}

Return only valid JSON with exactly these missing keys:
{missing_json}

Field guidance:
- why_this_worked: one concise sentence on why this is a strong Voice Example.
- hook: short human-readable hook mechanic, not a slogan.
- structure: short description of the post structure.
- tone: short description of the tone.
- cta: CTA style, or "none" if there is no CTA.
- formatting: short description of line breaks, length, and layout.
- pattern: reusable writing pattern name in plain language.

Keep every value specific to this post. Avoid generic labels, hype, and internal database language.
Do not mention Koinoniko; that is only the platform name.
Only reference writer, company, or product identity if it is explicitly present in Facts & Context or the post text.
Every value must be a complete phrase or sentence. Never end with ellipses (...)."""


def clean_voice_detail_suggestion(raw: str, allowed_fields: list[str]) -> dict[str, str]:
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict):
        return {}
    values: dict[str, str] = {}
    for key in allowed_fields:
        if key not in VOICE_AUTOFILL_FIELDS:
            continue
        cleaned = clean_optional(parsed.get(key))
        if not cleaned:
            continue
        limit = VOICE_AUTOFILL_FIELDS[key][1]
        cleaned_detail = clean_voice_detail_text(cleaned, limit)
        if cleaned_detail:
            values[key] = cleaned_detail
    return values


def clean_voice_detail_text(value: object, limit: int) -> str | None:
    cleaned = clean_optional(value)
    if not cleaned:
        return None
    cleaned = " ".join(cleaned.replace("\u2026", "...").split())
    cleaned = re.sub(r"\bKoinoniko(?:'s)?\s+voice\b", "this voice", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bKoinoniko(?:'s)?\b", "the platform", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.{3,}", ".", cleaned).strip()
    cleaned = re.sub(r"\s+[.,;:]", lambda match: match.group(0).strip(), cleaned)
    cleaned = cleaned.rstrip(" ,-;:")
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    clipped = cleaned[:limit].rstrip()
    boundary = max(clipped.rfind(". "), clipped.rfind("; "), clipped.rfind(", "), clipped.rfind(" "))
    if boundary > int(limit * 0.65):
        clipped = clipped[:boundary].rstrip()
    return clipped.rstrip(" ,-;:") or None


def merged_voice_note(note: PostNote, values: Mapping[str, str]) -> PostNote:
    return PostNote(
        post_id=note.post_id,
        worked_reason=note.worked_reason or values.get("why_this_worked"),
        pattern_name=note.pattern_name or values.get("pattern"),
        hook=note.hook or values.get("hook"),
        structure=note.structure or values.get("structure"),
        tone=note.tone or values.get("tone"),
        cta=note.cta or values.get("cta"),
        formatting=note.formatting or values.get("formatting"),
        imitate=note.imitate,
    )


def voice_coverage_payload(store: SQLiteStore) -> dict[str, Any]:
    examples = voice_example_posts(store)
    if not examples:
        return {
            "topics": [],
            "post_types": [],
            "recommendation": "Add a few Voice Examples to see coverage.",
        }
    topic_counts = Counter(post.topic or "Uncategorized" for post in examples)
    type_counts = Counter(post.post_type or "unknown" for post in examples)
    topics = coverage_rows(topic_counts, ["sales LLMs", "Bittensor", "product"])
    post_types = coverage_rows(type_counts, ["founder_insight", "quote_tweet", "product_update"])
    return {
        "topics": topics,
        "post_types": post_types,
        "recommendation": voice_coverage_recommendation(topic_counts, type_counts, len(examples)),
    }


def coverage_rows(counts: Counter[str], defaults: list[str]) -> list[dict[str, Any]]:
    names: list[str] = []
    for name in defaults:
        if name not in names:
            names.append(name)
    for name, _ in counts.most_common():
        if name not in names:
            names.append(name)
    return [{"name": name, "count": int(counts.get(name, 0))} for name in names[:8]]


def context_facts_payload(store: SQLiteStore) -> dict[str, Any]:
    sections = store.list_context_sections(kinds=FACT_CONTEXT_KINDS, active_only=False)
    by_key = {(section.kind, section.name): section for section in sections}
    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field in FACT_CONTEXT_FIELDS:
        key = (str(field["kind"]), str(field["name"]))
        seen.add(key)
        fields.append(context_fact_field_payload(field, by_key.get(key)))
    for section in sections:
        key = (section.kind, section.name)
        if key in seen:
            continue
        fields.append(
            {
                "kind": section.kind,
                "name": section.name,
                "title": label_from_identifier(section.name),
                "description": label_from_identifier(section.kind),
                "content": section.content,
                "active": section.active,
                "weight": section.weight,
                "updated_at": section.updated_at,
                "legacy": True,
            }
        )
    filled = sum(1 for field in fields if clean_optional(field.get("content")) and field.get("active", True))
    return {
        "items": fields,
        "summary": f"{filled} fact section{'s' if filled != 1 else ''} saved" if filled else "No facts saved yet",
    }


def context_fact_field_payload(field: Mapping[str, Any], section: ContextSection | None) -> dict[str, Any]:
    return {
        "kind": str(field["kind"]),
        "name": str(field["name"]),
        "title": str(field["title"]),
        "description": str(field["description"]),
        "content": section.content if section else "",
        "active": section.active if section else False,
        "weight": float(field.get("weight") or (section.weight if section else 1.0)),
        "updated_at": section.updated_at if section else None,
        "legacy": False,
    }


def save_context_facts_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    existing = {
        (section.kind, section.name): section
        for section in store.list_context_sections(kinds=FACT_CONTEXT_KINDS, active_only=False)
    }
    known = {
        (str(field["kind"]), str(field["name"])): field
        for field in FACT_CONTEXT_FIELDS
    }
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Facts payload must include items.")
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        kind = slug_value(item.get("kind"))
        name = slug_value(item.get("name"))
        if not kind or not name:
            continue
        if kind not in FACT_CONTEXT_KINDS and (kind, name) not in existing:
            continue
        field = known.get((kind, name), {})
        current = existing.get((kind, name))
        content = normalize_multiline_text(str(item.get("content") or ""))
        active = bool(content)
        store.save_context_section(
            ContextSection(
                kind=kind,
                name=name,
                content=content,
                weight=float(field.get("weight") or (current.weight if current else item.get("weight") or 1.0)),
                active=active,
                id=current.id if current else new_id(),
                created_at=current.created_at if current else utc_now(),
            )
        )
    response = context_facts_payload(store)
    response["message"] = "Facts & Context saved."
    return response


def label_from_identifier(value: object) -> str:
    words = re.split(r"[_\-\s]+", str(value or "").strip())
    return " ".join(word[:1].upper() + word[1:] for word in words if word) or "Context"


def slug_value(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    return re.sub(r"[^a-z0-9_]", "", text)


def normalize_multiline_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").splitlines()]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if cleaned and not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    return "\n".join(cleaned).strip()


def fact_context_prompt_text(store: SQLiteStore) -> str:
    sections = [
        section
        for section in store.list_context_sections(kinds=FACT_CONTEXT_KINDS)
        if clean_optional(section.content)
    ]
    return "\n".join(
        f"- {label_from_identifier(section.kind)} / {label_from_identifier(section.name)}: {section.content}"
        for section in sections
    )


def voice_rules_payload(store: SQLiteStore) -> dict[str, Any]:
    avoid_phrases = load_avoid_phrases(store)
    review_rules = review_filter_rules(store)
    groups = [
        {
            "key": "positioning_rules",
            "title": "Positioning Rules",
            "items": load_positioning_rules(store),
            "editable": True,
        },
        {
            "key": "voice_rules",
            "title": "Voice Rules",
            "items": current_voice_rules(store),
            "editable": True,
        },
        {
            "key": "review_rules",
            "title": "Review Filters",
            "items": review_rules,
            "editable": True,
        },
    ]
    rules_count = sum(len(group["items"]) for group in groups) + len(avoid_phrases)
    return {
        "rules_count": rules_count,
        "summary": "Review filters and writing guardrails",
        "groups": groups,
        "avoid_phrases": avoid_phrases,
    }


def save_voice_rules_payload(store: SQLiteStore, payload: dict[str, Any]) -> dict[str, Any]:
    if "positioning_rules" in payload:
        save_positioning_rules(store, string_list_payload(payload.get("positioning_rules")))
    if "voice_rules" in payload:
        save_voice_rules_override(store, string_list_payload(payload.get("voice_rules")))
    if "review_rules" in payload:
        save_review_rules(store, string_list_payload(payload.get("review_rules")))
    if "avoid_phrases" in payload:
        save_avoid_phrases(store, string_list_payload(payload.get("avoid_phrases")))
    response = voice_rules_payload(store)
    response["message"] = "Active Writing Rules saved."
    return response


def current_voice_rules(store: SQLiteStore) -> list[str]:
    override = load_voice_rules_override(store)
    if override is not None:
        return override
    current = current_voice_section(store)
    if not current:
        return list(DEFAULT_VOICE_RULES)
    rules: list[str] = []
    for section in voice_content_sections(current.content):
        if section["title"] in {"Recurring voice traits", "Voice traits", "Reusable writing principles"}:
            rules.extend(str(item).strip() for item in section["items"] if str(item).strip())
    return rules[:8] or list(DEFAULT_VOICE_RULES)


def review_filter_rules(store: SQLiteStore | None = None) -> list[str]:
    try:
        content = load_skill("review_filter.md").content
    except FileNotFoundError:
        default_rules: list[str] = []
    else:
        default_rules = [line[2:].strip() for line in content.splitlines() if line.startswith("- ")]
        default_rules = [rule for rule in default_rules if rule]
    if store:
        return load_review_rules(store, default_rules)
    return default_rules


def voice_coverage_recommendation(topic_counts: Counter[str], type_counts: Counter[str], examples_count: int) -> str:
    if examples_count < 3:
        return "Add 2-3 more Voice Examples across sales, product, and Bittensor before relying on Voice too heavily."
    if type_counts.get("product_update", 0) == 0:
        return "Add 1-2 product update examples so launch/product posts have stronger reference material."
    active_topics = [topic for topic, count in topic_counts.items() if count > 0]
    if len(active_topics) == 1:
        return f"Add 1-2 Voice Examples outside {active_topics[0]} so Voice does not overfit one lane."
    if type_counts.get("quote_tweet", 0) == 0 and examples_count >= 4:
        return "Add one quote tweet example if commentary posts should be part of the Voice profile."
    return "Voice coverage looks balanced enough for now."


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
            worked_reason=voice_payload_value(payload, "why_this_worked", existing.worked_reason if existing else None),
            pattern_name=voice_payload_value(payload, "pattern", existing.pattern_name if existing else None),
            hook=voice_payload_value(payload, "hook", existing.hook if existing else None),
            structure=voice_payload_value(payload, "structure", existing.structure if existing else None),
            tone=voice_payload_value(payload, "tone", existing.tone if existing else None),
            cta=voice_payload_value(payload, "cta", existing.cta if existing else None),
            formatting=voice_payload_value(payload, "formatting", existing.formatting if existing else None),
            imitate=bool_from_payload(payload, "voice_example", existing.imitate if existing else False),
        )
    )
    metrics = store.latest_metrics_by_post_id([post.id]).get(post.id)
    return post_detail_payload(store, post, note=note, metrics=metrics)


def voice_payload_value(payload: dict[str, Any], key: str, current: str | None) -> str | None:
    if key not in payload:
        return current
    limit = VOICE_AUTOFILL_FIELDS.get(key, ("", 260))[1]
    return clean_voice_detail_text(payload.get(key), limit)


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
        if line in {"Observed Gavin/Leadpoet Writing Guide", "Koinoniko Voice", "Voice Profile"}:
            continue
        if line.endswith(":"):
            if current_title or current_lines:
                sections.append({"title": current_title or "Voice Profile", "items": current_lines})
            current_title = line[:-1]
            current_lines = []
            continue
        current_lines.append(line[2:] if line.startswith("- ") else line)
    if current_title or current_lines:
        sections.append({"title": current_title or "Voice Profile", "items": current_lines})
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
    payload["why_this_worked"] = clean_voice_detail_text(note.worked_reason, VOICE_AUTOFILL_FIELDS["why_this_worked"][1]) if note else None
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
        if isinstance(payload.get("windows"), list):
            windows = [str(window) for window in payload.get("windows", []) if str(window) in {"weekly", "monthly"}]
            if windows:
                return 200, refresh_pattern_windows_task_payload(store, payload, windows=windows)
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
    recent_posts = store.list_posts(platform="x", limit=DEFAULT_DASHBOARD_POST_LIMIT)
    latest_metrics = store.latest_metrics_by_post_id([post.id for post in recent_posts])
    notes = store.notes_by_post_id([post.id for post in recent_posts])
    trend_groups = web_trend_groups(store)
    trends = [*trend_groups.daily, *trend_groups.weekly]
    patterns = pattern_groups(store)
    saved_patterns = store.list_viral_patterns(status="saved", limit=100)
    topic_ideas = store.list_topic_ideas(status="suggested", limit=DEFAULT_TOPIC_IDEA_LIMIT)
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
        "saved_patterns": [pattern_payload(pattern) for pattern in saved_patterns],
        "topic_ideas": [topic_idea_payload(idea) for idea in topic_ideas],
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
        "workspace_id": store.workspace_id if store else "",
        "provider_configured": bool(os.getenv("SOCIAL_BRAIN_PROVIDER")),
        "provider_path": os.getenv("SOCIAL_BRAIN_PROVIDER") or "",
        "openrouter_api_key_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "llm_model": model or "",
        "llm_model_source": model_source,
        "model_routing": resolve_model_routing(store).as_dict() if store else {},
        "x_read_configured": bool(os.getenv("X_BEARER_TOKEN")),
        "x_write_configured": has_x_write_credentials(),
        "scrapingdog_configured": scrapingdog_configured(),
        "notion_configured": bool(os.getenv("NOTION_API_TOKEN") and os.getenv("NOTION_PARENT_PAGE_ID")),
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
            "x_sync_username": default_x_sync_username_for_store(store),
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
            "Routes model calls for strategy, writing, and review.",
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
        "notion": connection_payload(
            "connected" if status.get("notion_configured") else "not_connected",
            "Notion",
            "Publishes approved lead magnets as reusable pages.",
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
        LEAD_MAGNET_RESEARCH_SYNC_KEY: sync_metadata_payload(metadata.get(LEAD_MAGNET_RESEARCH_SYNC_KEY)),
        LEAD_MAGNET_AUTOPILOT_SYNC_KEY: sync_metadata_payload(metadata.get(LEAD_MAGNET_AUTOPILOT_SYNC_KEY)),
    }


def account_sync_status_payload(sync: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_x_post_sync": sync[X_POSTS_SYNC_KEY]["last_completed_at"],
        "last_x_metrics_sync": sync[X_METRICS_SYNC_KEY]["last_completed_at"],
        "last_trend_refresh": sync[TRENDS_SYNC_KEY]["last_completed_at"],
        "last_pattern_refresh": sync[PATTERNS_SYNC_KEY]["last_completed_at"],
        "last_lead_magnet_research": sync[LEAD_MAGNET_RESEARCH_SYNC_KEY]["last_completed_at"],
        "last_lead_magnet_autopilot": sync[LEAD_MAGNET_AUTOPILOT_SYNC_KEY]["last_completed_at"],
        "latest_error": latest_sync_error(sync),
    }


def latest_sync_error(sync: dict[str, Any]) -> str | None:
    for key in [
        LEAD_MAGNET_AUTOPILOT_SYNC_KEY,
        LEAD_MAGNET_RESEARCH_SYNC_KEY,
        PATTERNS_SYNC_KEY,
        TRENDS_SYNC_KEY,
        X_DAILY_SYNC_KEY,
        X_POSTS_SYNC_KEY,
        X_METRICS_SYNC_KEY,
    ]:
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
        """
        SELECT username
        FROM web_auth_users
        WHERE workspace_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (store.workspace_id,),
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
        "topic_idea_id": item.topic_idea_id,
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
        "source_draft_id": version.source_draft_id,
        "accepted_at": version.accepted_at,
        "created_at": version.created_at,
    }


def lead_magnet_payload(store: SQLiteStore, item: LeadMagnet, *, include_detail: bool = False) -> dict[str, Any]:
    sources = [source for source_id in item.source_ids if (source := store.get_lead_magnet_source(source_id))]
    payload: dict[str, Any] = {
        "id": item.id,
        "title": item.title,
        "promise": item.promise,
        "audience": item.audience,
        "sales_outcome": item.sales_outcome,
        "format": item.format,
        "depth": item.depth,
        "product_integration": item.product_integration,
        "cta": item.cta,
        "status": item.status,
        "source_ids": item.source_ids,
        "research_run_id": item.research_run_id,
        "evidence_summary": item.evidence_summary,
        "research_strength": lead_magnet_research_strength(sources),
        "source_count": len(sources),
        "notion_page_id": item.notion_page_id,
        "notion_url": item.notion_url,
        "published_at": item.published_at,
        "failure_reason": item.failure_reason,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if include_detail:
        payload.update(
            {
                "content": item.content,
                "markdown": item.markdown,
                "generation_brief": item.generation_brief,
                "sources": [lead_magnet_source_payload(source) for source in sources],
                "versions": [
                    lead_magnet_version_payload(version)
                    for version in store.list_lead_magnet_versions(item.id, limit=50)
                ],
                "research_run": lead_magnet_research_run_payload(run)
                if item.research_run_id and (run := store.get_lead_magnet_research_run(item.research_run_id))
                else None,
            }
        )
    return payload


def lead_magnet_source_payload(source: LeadMagnetSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title,
        "source_url": source.source_url,
        "canonical_url": source.canonical_url,
        "platform": source.platform,
        "author": source.author,
        "published_at": source.published_at,
        "collected_at": source.collected_at,
        "format": source.format,
        "sales_topic": source.sales_topic,
        "hook": source.hook,
        "promise": source.promise,
        "audience": source.audience,
        "delivery_mechanism": source.delivery_mechanism,
        "cta": source.cta,
        "leadpoet_integration": source.leadpoet_integration,
        "why_it_worked": source.why_it_worked,
        "observed_metrics": {
            "likes": source.likes,
            "replies": source.replies,
            "reposts": source.reposts,
            "bookmarks": source.bookmarks,
            "impressions": source.impressions,
            "follower_count": source.follower_count,
            "observed_at": source.metric_observed_at,
        },
        "performance_score": source.performance_score,
        "evidence_strength": source.evidence_strength,
        "status": source.status,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def lead_magnet_research_run_payload(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "period_key": run.period_key,
        "query": run.query,
        "provider": run.provider,
        "status": run.status,
        "candidates_found": run.candidates_found,
        "sources_qualified": run.sources_qualified,
        "source_ids": run.source_ids,
        "warnings": run.warnings,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def lead_magnet_version_payload(version: LeadMagnetVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "lead_magnet_id": version.lead_magnet_id,
        "content": version.content,
        "markdown": version.markdown,
        "edit_type": version.edit_type,
        "instruction": version.instruction,
        "source_ids": version.source_ids,
        "created_at": version.created_at,
        "updated_at": version.updated_at,
    }


def lead_magnet_research_result_payload(result: Any) -> dict[str, Any]:
    return {
        "run": lead_magnet_research_run_payload(result.run),
        "sources": [lead_magnet_source_payload(source) for source in result.sources],
        "warnings": list(result.warnings),
        "candidates_found": result.candidates_found,
    }


def lead_magnet_research_strength(sources: list[LeadMagnetSource]) -> str:
    observed = sum(1 for source in sources if source.evidence_strength == "metric_backed")
    if observed >= 3:
        return "strong"
    if observed:
        return "directional"
    return "qualitative" if sources else "none"


def update_lead_magnet_from_payload(store: SQLiteStore, lead_magnet_id: str, payload: dict[str, Any]) -> LeadMagnet:
    item = store.get_lead_magnet(lead_magnet_id)
    if not item:
        raise ValueError("Lead magnet not found.")
    content_value = payload.get("content")
    if content_value is not None and not isinstance(content_value, dict):
        raise ValueError("content must be an object.")
    content = dict(content_value) if isinstance(content_value, dict) else item.content
    markdown = str(payload.get("markdown") or "") if "markdown" in payload else item.markdown
    if isinstance(content_value, dict) and "markdown" not in payload:
        markdown = render_lead_magnet_markdown(content)
    source_ids = string_list_payload(payload.get("source_ids")) if "source_ids" in payload else item.source_ids
    missing_sources = [source_id for source_id in source_ids if not store.get_lead_magnet_source(source_id)]
    if missing_sources:
        raise ValueError("Unknown lead-magnet source id(s): " + ", ".join(missing_sources))
    content_changed = content != item.content or markdown != item.markdown
    updated = store.save_lead_magnet(
        replace(
            item,
            title=str(payload.get("title") or item.title).strip(),
            promise=str(payload.get("promise") or item.promise).strip(),
            audience=str(payload.get("audience") or item.audience).strip(),
            sales_outcome=str(payload.get("sales_outcome") or payload.get("sales_problem") or item.sales_outcome).strip(),
            format=str(payload.get("format") or item.format).strip(),
            depth=str(payload.get("depth") or item.depth).strip(),
            product_integration=str(payload.get("product_integration") or item.product_integration).strip(),
            cta=str(payload.get("cta") or item.cta).strip(),
            source_ids=source_ids,
            evidence_summary=clean_optional(payload.get("evidence_summary")) if "evidence_summary" in payload else item.evidence_summary,
            content=content,
            markdown=markdown,
            status="needs_review" if content_changed else item.status,
            failure_reason=None if content_changed else item.failure_reason,
            updated_at=utc_now(),
        )
    )
    if content_changed:
        store.save_lead_magnet_version(
            LeadMagnetVersion(
                lead_magnet_id=updated.id,
                content=content,
                markdown=markdown,
                edit_type="manual_edit",
                instruction=clean_optional(payload.get("edit_note")),
                source_ids=source_ids,
            )
        )
    return updated


def save_lead_magnet_research_sync(store: SQLiteStore, result: Any) -> SyncMetadata:
    summary = {
        "research_run_id": result.run.id,
        "window_start": result.run.window_start,
        "window_end": result.run.window_end,
        "candidates_found": result.candidates_found,
        "sources_qualified": len(result.sources),
        "metric_backed_sources": sum(
            1 for source in result.sources if source.evidence_strength == "metric_backed"
        ),
        "warnings": list(result.warnings),
        "completed_at": result.run.completed_at,
    }
    return store.save_sync_metadata(
        SyncMetadata(
            key=LEAD_MAGNET_RESEARCH_SYNC_KEY,
            last_started_at=result.run.started_at,
            last_completed_at=result.run.completed_at,
            status="partial" if result.warnings else "success",
            summary=summary,
            error_message="\n".join(result.warnings[:5]) if result.warnings else None,
        )
    )


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


def trend_payload(trend: TrendItem, *, include_raw: bool = False) -> dict[str, Any]:
    payload = {
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
        "source_title": trend.source_title,
        "source_published_at": trend.source_published_at,
        "source_excerpt": trend.source_excerpt,
        "source_text_excerpt": truncate_payload_text(trend.source_text_excerpt, 1200),
        "key_points": json_list_payload(trend.key_points_json),
        "important_claims": json_list_payload(trend.important_claims_json),
        "post_angles": json_list_payload(trend.post_angles_json),
        "source_confidence": trend.source_confidence,
        "enriched_at": trend.enriched_at,
        "enrichment_status": trend.enrichment_status,
        "enrichment_error": trend.enrichment_error,
        "created_at": trend.created_at,
        "updated_at": trend.updated_at,
    }
    if include_raw:
        payload["raw_text"] = truncate_payload_text(trend.raw_text, 4000)
    return payload


def topic_idea_payload(idea: TopicIdea) -> dict[str, Any]:
    return {
        "id": idea.id,
        "title": idea.title,
        "topic": idea.topic,
        "angle": idea.angle,
        "why_now": idea.why_now,
        "source_type": idea.source_type,
        "source_refs": idea.source_refs,
        "post_type": idea.post_type,
        "priority": idea.priority,
        "status": idea.status,
        "created_at": idea.created_at,
        "updated_at": idea.updated_at,
    }


def pattern_groups_payload(groups: dict[str, list[ViralPattern]]) -> dict[str, Any]:
    return {
        "weekly": [pattern_payload(pattern) for pattern in groups.get("weekly", [])],
        "monthly": [pattern_payload(pattern) for pattern in groups.get("monthly", [])],
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
        "why_this_worked": clean_voice_detail_text(note.worked_reason, VOICE_AUTOFILL_FIELDS["why_this_worked"][1]) if note else None,
        "hook": clean_voice_detail_text(note.hook, VOICE_AUTOFILL_FIELDS["hook"][1]) if note else None,
        "structure": clean_voice_detail_text(note.structure, VOICE_AUTOFILL_FIELDS["structure"][1]) if note else None,
        "tone": clean_voice_detail_text(note.tone, VOICE_AUTOFILL_FIELDS["tone"][1]) if note else None,
        "cta": clean_voice_detail_text(note.cta, VOICE_AUTOFILL_FIELDS["cta"][1]) if note else None,
        "formatting": clean_voice_detail_text(note.formatting, VOICE_AUTOFILL_FIELDS["formatting"][1]) if note else None,
        "pattern": clean_voice_detail_text(note.pattern_name, VOICE_AUTOFILL_FIELDS["pattern"][1]) if note else None,
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
        topic=value_or_existing(payload.get("topic"), existing.topic if existing else "uncategorized"),
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
        source_title=value_or_existing(payload.get("source_title"), existing.source_title if existing else None),
        source_published_at=value_or_existing(payload.get("source_published_at"), existing.source_published_at if existing else None),
        source_excerpt=value_or_existing(payload.get("source_excerpt"), existing.source_excerpt if existing else None),
        source_text_excerpt=value_or_existing(payload.get("source_text_excerpt"), existing.source_text_excerpt if existing else None),
        key_points_json=value_or_existing(payload.get("key_points_json"), existing.key_points_json if existing else None),
        important_claims_json=value_or_existing(payload.get("important_claims_json"), existing.important_claims_json if existing else None),
        post_angles_json=value_or_existing(payload.get("post_angles_json"), existing.post_angles_json if existing else None),
        source_confidence=value_or_existing(payload.get("source_confidence"), existing.source_confidence if existing else None),
        enriched_at=value_or_existing(payload.get("enriched_at"), existing.enriched_at if existing else None),
        enrichment_status=value_or_existing(payload.get("enrichment_status"), existing.enrichment_status if existing else "not_attempted"),
        enrichment_error=value_or_existing(payload.get("enrichment_error"), existing.enrichment_error if existing else None),
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
        "candidates_found": int(getattr(groups, "candidates_found", 0) or 0),
        "trends_stored": int(getattr(groups, "trends_stored", 0) or 0),
        "enriched": int(getattr(groups, "enriched", 0) or 0),
        "partial": int(getattr(groups, "partial", 0) or 0),
        "failed": int(getattr(groups, "failed", 0) or 0),
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


def json_list_payload(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if not isinstance(parsed, list):
        return []
    return [cleaned for item in parsed if (cleaned := clean_optional(item))]


def truncate_payload_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


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


def phrase_list_payload(payload: dict[str, Any]) -> list[object]:
    phrases = payload.get("phrases")
    if isinstance(phrases, list):
        return phrases
    if isinstance(payload.get("items"), list):
        return payload["items"]
    text = clean_optional(payload.get("text"))
    if text:
        return text.splitlines()
    return []


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
