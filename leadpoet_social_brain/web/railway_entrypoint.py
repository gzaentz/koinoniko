from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from leadpoet_social_brain.web import app as web_app
from leadpoet_social_brain.web.task_client import TASK_DEFAULTS, main as task_client_main


TASK_BY_SERVICE_NAME = {
    "publish-due-x": "publish-due-x",
    "sync-x-posts": "sync-x-posts",
    "sync-x-metrics": "sync-x-metrics",
    "sync-x-all": "sync-x-all",
    "sync-x-daily": "sync-x-daily",
    "refresh-trends": "refresh-trends",
    "refresh-patterns": "refresh-patterns-weekly",
    "refresh-patterns-weekly": "refresh-patterns-weekly",
    "refresh-topics": "refresh-topics",
    "autopilot-weekly": "autopilot-weekly",
}


def main(argv: Sequence[str] | None = None) -> int:
    task = task_from_environment(os.environ)
    if task:
        return task_client_main([task, *(argv or [])])
    return web_app.main(["--host", os.getenv("HOST", "0.0.0.0"), *(argv or [])])


def task_from_environment(environ: Mapping[str, str]) -> str | None:
    explicit_task = clean(environ.get("SOCIAL_BRAIN_TASK"))
    if explicit_task:
        if explicit_task not in TASK_DEFAULTS:
            raise SystemExit(f"Unknown SOCIAL_BRAIN_TASK: {explicit_task}")
        return explicit_task

    service_name = clean(environ.get("RAILWAY_SERVICE_NAME"))
    if not service_name:
        return None
    return TASK_BY_SERVICE_NAME.get(service_name)


def clean(value: str | None) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
