from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


TASK_TIMEOUT_ENV = "SOCIAL_BRAIN_TASK_TIMEOUT"
DEFAULT_TASK_TIMEOUT_SECONDS = 180

TASK_DEFAULTS: dict[str, dict[str, Any]] = {
    "sync-x-posts": {"limit": 25},
    "sync-x-metrics": {"since_days": 30, "limit": 250},
    "sync-x-all": {"post_limit": 25, "metrics_limit": 250, "since_days": 30},
    "sync-x-daily": {"post_limit": 25, "metrics_limit": 250, "since_days": 30},
    "publish-due-x": {"limit": 20},
    "refresh-trends": {
        "limit": 5,
        "no_enrich": True,
        "scrapingdog_timeout_seconds": 12,
        "max_news_queries_per_window": 2,
        "deadline_seconds": 85,
    },
    "refresh-patterns": {"limit": 5, "window": "weekly"},
    "refresh-patterns-weekly": {"limit": 5, "windows": ["weekly", "monthly"]},
    "refresh-topics": {"limit": 10},
    "autopilot-weekly": {
        "posts": 5,
        "topic_limit": 10,
        "use_trends": True,
        "use_patterns": True,
        "length_mode": "standard",
    },
    "autopilot-lead-magnet": {
        "window_days": 30,
        "research_limit": 20,
        "depth": "standard",
        "product_integration": "embedded",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call protected Koinoniko task endpoints.")
    parser.add_argument("task", choices=sorted(TASK_DEFAULTS))
    parser.add_argument("--url", default=os.getenv("SOCIAL_BRAIN_URL", ""))
    parser.add_argument("--secret", default=os.getenv("TASK_SECRET", ""))
    parser.add_argument(
        "--timeout",
        type=int,
        default=task_timeout_from_env(),
        help=f"HTTP timeout in seconds. Defaults to {TASK_TIMEOUT_ENV} or {DEFAULT_TASK_TIMEOUT_SECONDS}.",
    )
    parser.add_argument("--payload", default="", help="Optional JSON object to merge into the request body.")
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    if not base_url:
        raise SystemExit("SOCIAL_BRAIN_URL or --url is required.")
    if not args.secret:
        raise SystemExit("TASK_SECRET or --secret is required.")

    payload = dict(TASK_DEFAULTS[args.task])
    if args.payload:
        override = json.loads(args.payload)
        if not isinstance(override, dict):
            raise SystemExit("--payload must be a JSON object.")
        payload.update(override)

    try:
        result = post_task(base_url, args.task, args.secret, payload, timeout_seconds=args.timeout)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if task_result_has_errors(args.task, result) else 0


def task_timeout_from_env() -> int:
    raw = os.getenv(TASK_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TASK_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TASK_TIMEOUT_SECONDS
    return max(1, value)


def task_result_has_errors(task: str, result: dict[str, Any]) -> bool:
    sync = result.get("sync")
    stored_trends = int(result.get("trends_stored") or 0)
    if task == "refresh-trends" and stored_trends > 0 and isinstance(sync, dict) and sync.get("status") in {"partial", "success"}:
        return False
    if result.get("errors"):
        return True
    for item in result.get("results", []):
        if isinstance(item, dict) and item.get("error"):
            return True
    return False


def post_task(
    base_url: str,
    task: str,
    secret: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/tasks/{task}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": "koinoniko-task-client/0.2",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(task_request_failure_message(task, body_text, status_code=exc.code)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(task_request_failure_message(task, str(exc.reason))) from exc


def task_request_failure_message(task: str, detail: str, *, status_code: int | None = None) -> str:
    prefix = f"Task {task} request failed"
    if status_code is not None:
        prefix = f"{prefix} ({status_code})"
    message = f"{prefix}: {detail}"
    if status_code == 524:
        message = (
            f"{message}. The hosted task request timed out before the app returned a response; "
            "reduce the task payload/batch size or split the cron job into smaller tasks."
        )
    return message


if __name__ == "__main__":
    raise SystemExit(main())
