from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


TASK_DEFAULTS: dict[str, dict[str, Any]] = {
    "sync-x-posts": {"limit": 25},
    "sync-x-metrics": {"since_days": 30, "limit": 250},
    "sync-x-all": {"post_limit": 25, "metrics_limit": 250, "since_days": 30},
    "sync-x-daily": {"post_limit": 25, "metrics_limit": 250, "since_days": 30},
    "refresh-trends": {"limit": 5},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call protected Koinoniko task endpoints.")
    parser.add_argument("task", choices=sorted(TASK_DEFAULTS))
    parser.add_argument("--url", default=os.getenv("SOCIAL_BRAIN_URL", ""))
    parser.add_argument("--secret", default=os.getenv("TASK_SECRET", ""))
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
        result = post_task(base_url, args.task, args.secret, payload)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("errors") else 0


def post_task(base_url: str, task: str, secret: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Task request failed ({exc.code}): {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Task request failed: {exc.reason}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
