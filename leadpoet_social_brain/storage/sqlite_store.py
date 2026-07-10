from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
import re

from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    ContextSection,
    DEFAULT_WORKSPACE_ID,
    DraftRequest,
    EngagementMetrics,
    GeneratedDraft,
    LeadMagnet,
    LeadMagnetResearchRun,
    LeadMagnetSource,
    LeadMagnetVersion,
    Pattern,
    PostInfluence,
    PostNote,
    QueuedPostVersion,
    SocialPost,
    SyncMetadata,
    TopicIdea,
    TrendItem,
    ViralPattern,
    VoiceExampleCandidate,
    WebAuthUser,
    new_id,
    utc_now,
)


DEFAULT_DB_PATH = Path("data/social_brain.sqlite3")
DB_ENV_VAR = "SOCIAL_BRAIN_DB"
RAILWAY_VOLUME_MOUNT_ENV_VAR = "RAILWAY_VOLUME_MOUNT_PATH"
DEFAULT_WEB_AUTH_USERNAME = "gavinz444"
PASSWORD_HASH_ITERATIONS = 310_000
WORKSPACE_ID_MAX_LENGTH = 80
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_BUSY_RETRY_SECONDS = 10.0


def resolve_db_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.getenv(DB_ENV_VAR)
    if env_path:
        return Path(env_path)
    railway_volume_path = os.getenv(RAILWAY_VOLUME_MOUNT_ENV_VAR)
    if railway_volume_path:
        return Path(railway_volume_path) / DEFAULT_DB_PATH.name
    return DEFAULT_DB_PATH


class SQLiteStore:
    def __init__(self, path: str | Path | None = None, *, workspace_id: str | None = None):
        self.path = resolve_db_path(path)
        self.workspace_id = normalize_workspace_id(workspace_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=SQLITE_BUSY_RETRY_SECONDS)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._run_with_busy_retry(self.initialize)

    def close(self) -> None:
        self.conn.close()

    def _configure_connection(self) -> None:
        self.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        self._run_with_busy_retry(lambda: self.conn.execute("PRAGMA journal_mode=WAL").fetchone())
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _run_with_busy_retry(self, operation):
        deadline = time.monotonic() + SQLITE_BUSY_RETRY_SECONDS
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not self._is_busy_error(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    def initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL,
                text TEXT NOT NULL,
                url TEXT,
                published_at TEXT,
                author TEXT NOT NULL,
                topic TEXT NOT NULL,
                post_type TEXT NOT NULL,
                status TEXT NOT NULL,
                source_draft_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_metrics (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                measured_at TEXT NOT NULL,
                impressions INTEGER,
                likes INTEGER,
                replies INTEGER,
                reposts INTEGER,
                quotes INTEGER,
                bookmarks INTEGER,
                engagement_rate REAL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_post_metrics_post_id
            ON post_metrics(post_id, measured_at DESC);

            CREATE TABLE IF NOT EXISTS post_notes (
                post_id TEXT PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                worked_reason TEXT,
                pattern_name TEXT,
                hook TEXT,
                structure TEXT,
                tone TEXT,
                cta TEXT,
                formatting TEXT,
                imitate INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS patterns (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                example_post_ids TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, name)
            );

            CREATE TABLE IF NOT EXISTS pattern_examples (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL DEFAULT 'x',
                source_url TEXT,
                source_author TEXT,
                source_text TEXT,
                source_summary TEXT,
                topic TEXT,
                category TEXT,
                pattern_name TEXT,
                hook_style TEXT,
                structure TEXT,
                pacing TEXT,
                tone TEXT,
                formatting TEXT,
                why_it_worked TEXT,
                adaptation_rule TEXT,
                engagement_notes TEXT,
                likes INTEGER,
                replies INTEGER,
                reposts INTEGER,
                quotes INTEGER,
                bookmarks INTEGER,
                impressions INTEGER,
                collected_at TEXT,
                window TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'active',
                raw_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_examples_lookup
            ON pattern_examples(workspace_id, status, window, category, topic, collected_at);

            CREATE TABLE IF NOT EXISTS context_sections (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, kind, name)
            );

            CREATE TABLE IF NOT EXISTS generated_drafts (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL,
                topic TEXT NOT NULL,
                post_type TEXT NOT NULL,
                request_json TEXT NOT NULL,
                text TEXT NOT NULL,
                explanation TEXT NOT NULL,
                influences_json TEXT NOT NULL,
                context_snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_influences (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                source_draft_id TEXT,
                influence_type TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                ref_id TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(post_id, influence_type, category, name, ref_id)
            );

            CREATE INDEX IF NOT EXISTS idx_post_influences_post_id
            ON post_influences(post_id);

            CREATE INDEX IF NOT EXISTS idx_post_influences_category
            ON post_influences(influence_type, category, name);

            CREATE TABLE IF NOT EXISTS trend_items (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                source_url TEXT,
                source_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                why_it_matters TEXT NOT NULL,
                leadpoet_angle TEXT NOT NULL,
                suggested_hooks TEXT,
                urgency TEXT NOT NULL DEFAULT 'medium',
                date TEXT,
                status TEXT NOT NULL DEFAULT 'unused',
                engagement_notes TEXT,
                source_author TEXT,
                raw_text TEXT,
                source_title TEXT,
                source_published_at TEXT,
                source_excerpt TEXT,
                source_text_excerpt TEXT,
                key_points_json TEXT,
                important_claims_json TEXT,
                post_angles_json TEXT,
                source_confidence TEXT,
                enriched_at TEXT,
                enrichment_status TEXT NOT NULL DEFAULT 'not_attempted',
                enrichment_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trend_items_lookup
            ON trend_items(workspace_id, status, urgency, topic, date);

            CREATE TABLE IF NOT EXISTS lead_magnet_sources (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                source_url TEXT,
                canonical_url TEXT,
                platform TEXT NOT NULL DEFAULT 'web',
                author TEXT,
                published_at TEXT,
                collected_at TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'other',
                sales_topic TEXT,
                hook TEXT,
                promise TEXT,
                audience TEXT,
                delivery_mechanism TEXT,
                cta TEXT,
                leadpoet_integration TEXT,
                why_it_worked TEXT,
                likes INTEGER,
                replies INTEGER,
                reposts INTEGER,
                bookmarks INTEGER,
                impressions INTEGER,
                follower_count INTEGER,
                metric_observed_at TEXT,
                performance_score REAL,
                evidence_strength TEXT NOT NULL DEFAULT 'unknown',
                raw_text TEXT,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_magnet_research_runs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                period_key TEXT NOT NULL,
                query TEXT NOT NULL,
                provider TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                candidates_found INTEGER NOT NULL DEFAULT 0,
                sources_qualified INTEGER NOT NULL DEFAULT 0,
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_magnets (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                promise TEXT NOT NULL DEFAULT '',
                audience TEXT NOT NULL DEFAULT '',
                sales_outcome TEXT NOT NULL DEFAULT '',
                format TEXT NOT NULL DEFAULT 'notion_prompt_repo',
                depth TEXT NOT NULL DEFAULT 'standard',
                product_integration TEXT NOT NULL DEFAULT '',
                cta TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                research_run_id TEXT REFERENCES lead_magnet_research_runs(id) ON DELETE SET NULL,
                evidence_summary TEXT,
                content_json TEXT NOT NULL DEFAULT '{}',
                markdown TEXT NOT NULL DEFAULT '',
                generation_brief_json TEXT NOT NULL DEFAULT '{}',
                notion_page_id TEXT,
                notion_url TEXT,
                published_at TEXT,
                failure_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_magnet_versions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                lead_magnet_id TEXT NOT NULL REFERENCES lead_magnets(id) ON DELETE CASCADE,
                content_json TEXT NOT NULL DEFAULT '{}',
                markdown TEXT NOT NULL DEFAULT '',
                edit_type TEXT NOT NULL DEFAULT 'generated',
                instruction TEXT,
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topic_ideas (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                angle TEXT NOT NULL,
                why_now TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'evergreen',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_basis_json TEXT NOT NULL DEFAULT '[]',
                reader_value_mode TEXT,
                post_type TEXT NOT NULL DEFAULT 'founder_insight',
                priority INTEGER NOT NULL DEFAULT 50,
                status TEXT NOT NULL DEFAULT 'suggested',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_topic_ideas_lookup
            ON topic_ideas(workspace_id, status, priority DESC, created_at DESC);

            CREATE TABLE IF NOT EXISTS content_queue (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL DEFAULT 'x',
                draft_id TEXT,
                topic_idea_id TEXT,
                text TEXT NOT NULL,
                topic TEXT NOT NULL,
                post_type TEXT NOT NULL,
                angle TEXT,
                trend_ids_json TEXT NOT NULL DEFAULT '[]',
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewer_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT,
                posted_at TEXT,
                x_post_id TEXT,
                x_url TEXT,
                failure_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_content_queue_status_scheduled
            ON content_queue(workspace_id, status, scheduled_at);

            CREATE TABLE IF NOT EXISTS queued_post_versions (
                id TEXT PRIMARY KEY,
                queue_id TEXT NOT NULL REFERENCES content_queue(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                edit_type TEXT NOT NULL,
                edit_instruction TEXT,
                source_draft_id TEXT,
                accepted_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_queued_post_versions_queue_id
            ON queued_post_versions(queue_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS web_auth_users (
                username TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                iterations INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                workspace_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, key)
            );

            CREATE TABLE IF NOT EXISTS sync_metadata (
                workspace_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                last_started_at TEXT,
                last_completed_at TEXT,
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                PRIMARY KEY (workspace_id, key)
            );
            """
        )
        self._migrate_existing_schema()
        self._ensure_required_context_sections()
        self.conn.commit()

    def _migrate_existing_schema(self) -> None:
        self._ensure_workspace_columns()
        self._migrate_workspace_unique_tables()
        self._ensure_lead_magnet_columns()
        self._ensure_unique_lead_magnet_period_keys()
        self._recreate_workspace_indexes()
        self._ensure_column("posts", "source_draft_id", "TEXT")
        for column in ["hook", "structure", "tone", "cta", "formatting"]:
            self._ensure_column("post_notes", column, "TEXT")
        self._ensure_column("generated_drafts", "context_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("content_queue", "topic_idea_id", "TEXT")
        self._ensure_column("queued_post_versions", "source_draft_id", "TEXT")
        self._ensure_column("queued_post_versions", "accepted_at", "TEXT")
        self._ensure_column("topic_ideas", "evidence_basis_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("topic_ideas", "reader_value_mode", "TEXT")
        for column in [
            "source_title",
            "source_published_at",
            "source_excerpt",
            "source_text_excerpt",
            "key_points_json",
            "important_claims_json",
            "post_angles_json",
            "source_confidence",
            "enriched_at",
            "enrichment_error",
        ]:
            self._ensure_column("trend_items", column, "TEXT")
        self._ensure_column("trend_items", "enrichment_status", "TEXT NOT NULL DEFAULT 'not_attempted'")

    def _ensure_workspace_columns(self) -> None:
        for table in [
            "posts",
            "patterns",
            "pattern_examples",
            "context_sections",
            "generated_drafts",
            "trend_items",
            "lead_magnet_sources",
            "lead_magnet_research_runs",
            "lead_magnets",
            "lead_magnet_versions",
            "topic_ideas",
            "content_queue",
            "web_auth_users",
            "app_settings",
            "sync_metadata",
        ]:
            self._ensure_column(table, "workspace_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE_ID}'")

    def _ensure_lead_magnet_columns(self) -> None:
        columns_by_table = {
            "lead_magnet_sources": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "source_url": "TEXT",
                "canonical_url": "TEXT",
                "platform": "TEXT NOT NULL DEFAULT 'web'",
                "author": "TEXT",
                "published_at": "TEXT",
                "collected_at": "TEXT NOT NULL DEFAULT ''",
                "format": "TEXT NOT NULL DEFAULT 'other'",
                "sales_topic": "TEXT",
                "hook": "TEXT",
                "promise": "TEXT",
                "audience": "TEXT",
                "delivery_mechanism": "TEXT",
                "cta": "TEXT",
                "leadpoet_integration": "TEXT",
                "why_it_worked": "TEXT",
                "likes": "INTEGER",
                "replies": "INTEGER",
                "reposts": "INTEGER",
                "bookmarks": "INTEGER",
                "impressions": "INTEGER",
                "follower_count": "INTEGER",
                "metric_observed_at": "TEXT",
                "performance_score": "REAL",
                "evidence_strength": "TEXT NOT NULL DEFAULT 'unknown'",
                "raw_text": "TEXT",
                "status": "TEXT NOT NULL DEFAULT 'candidate'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "lead_magnet_research_runs": {
                "window_start": "TEXT NOT NULL DEFAULT ''",
                "window_end": "TEXT NOT NULL DEFAULT ''",
                "period_key": "TEXT NOT NULL DEFAULT ''",
                "query": "TEXT NOT NULL DEFAULT ''",
                "provider": "TEXT",
                "status": "TEXT NOT NULL DEFAULT 'running'",
                "candidates_found": "INTEGER NOT NULL DEFAULT 0",
                "sources_qualified": "INTEGER NOT NULL DEFAULT 0",
                "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
                "error_message": "TEXT",
                "started_at": "TEXT NOT NULL DEFAULT ''",
                "completed_at": "TEXT",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "lead_magnets": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "promise": "TEXT NOT NULL DEFAULT ''",
                "audience": "TEXT NOT NULL DEFAULT ''",
                "sales_outcome": "TEXT NOT NULL DEFAULT ''",
                "format": "TEXT NOT NULL DEFAULT 'notion_prompt_repo'",
                "depth": "TEXT NOT NULL DEFAULT 'standard'",
                "product_integration": "TEXT NOT NULL DEFAULT ''",
                "cta": "TEXT NOT NULL DEFAULT ''",
                "status": "TEXT NOT NULL DEFAULT 'draft'",
                "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "research_run_id": "TEXT",
                "evidence_summary": "TEXT",
                "content_json": "TEXT NOT NULL DEFAULT '{}'",
                "markdown": "TEXT NOT NULL DEFAULT ''",
                "generation_brief_json": "TEXT NOT NULL DEFAULT '{}'",
                "notion_page_id": "TEXT",
                "notion_url": "TEXT",
                "published_at": "TEXT",
                "failure_reason": "TEXT",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "lead_magnet_versions": {
                "lead_magnet_id": "TEXT NOT NULL DEFAULT ''",
                "content_json": "TEXT NOT NULL DEFAULT '{}'",
                "markdown": "TEXT NOT NULL DEFAULT ''",
                "edit_type": "TEXT NOT NULL DEFAULT 'generated'",
                "instruction": "TEXT",
                "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in columns_by_table.items():
            for column, ddl in columns.items():
                self._ensure_column(table, column, ddl)

    def _ensure_unique_lead_magnet_period_keys(self) -> None:
        rows = self.conn.execute(
            """
            SELECT rowid, id, workspace_id, period_key
            FROM lead_magnet_research_runs
            ORDER BY rowid
            """
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        for row in rows:
            workspace_id = str(row["workspace_id"] or DEFAULT_WORKSPACE_ID)
            original = str(row["period_key"] or "").strip()
            base = original or f"legacy:{row['id'] or row['rowid']}"
            candidate = base
            duplicate_number = 1
            while (workspace_id, candidate) in seen:
                duplicate_number += 1
                candidate = f"{base}:legacy-{duplicate_number}"
            seen.add((workspace_id, candidate))
            if candidate != original:
                self.conn.execute(
                    "UPDATE lead_magnet_research_runs SET period_key = ? WHERE rowid = ?",
                    (candidate, row["rowid"]),
                )

    def _migrate_workspace_unique_tables(self) -> None:
        if not self._has_unique_index("patterns", ["workspace_id", "name"]):
            self._rebuild_patterns_table()
        if not self._has_unique_index("context_sections", ["workspace_id", "kind", "name"]):
            self._rebuild_context_sections_table()
        if not self._has_unique_index("app_settings", ["workspace_id", "key"]):
            self._rebuild_app_settings_table()
        if not self._has_unique_index("sync_metadata", ["workspace_id", "key"]):
            self._rebuild_sync_metadata_table()

    def _recreate_workspace_indexes(self) -> None:
        self._ensure_index(
            "idx_pattern_examples_lookup",
            ["workspace_id", "status", "window", "category", "topic", "collected_at"],
            """
            CREATE INDEX IF NOT EXISTS idx_pattern_examples_lookup
            ON pattern_examples(workspace_id, status, window, category, topic, collected_at)
            """,
        )
        self._ensure_index(
            "idx_trend_items_lookup",
            ["workspace_id", "status", "urgency", "topic", "date"],
            """
            CREATE INDEX IF NOT EXISTS idx_trend_items_lookup
            ON trend_items(workspace_id, status, urgency, topic, date)
            """,
        )
        self._ensure_index(
            "idx_content_queue_status_scheduled",
            ["workspace_id", "status", "scheduled_at"],
            """
            CREATE INDEX IF NOT EXISTS idx_content_queue_status_scheduled
            ON content_queue(workspace_id, status, scheduled_at)
            """,
        )
        self._ensure_index(
            "idx_topic_ideas_lookup",
            ["workspace_id", "status", "priority", "created_at"],
            """
            CREATE INDEX IF NOT EXISTS idx_topic_ideas_lookup
            ON topic_ideas(workspace_id, status, priority DESC, created_at DESC)
            """,
        )
        self._ensure_index(
            "idx_lead_magnet_sources_lookup",
            ["workspace_id", "status", "published_at", "performance_score"],
            """
            CREATE INDEX IF NOT EXISTS idx_lead_magnet_sources_lookup
            ON lead_magnet_sources(workspace_id, status, published_at DESC, performance_score DESC)
            """,
        )
        self._ensure_index(
            "idx_lead_magnet_sources_canonical_url",
            ["workspace_id", "canonical_url"],
            """
            CREATE INDEX IF NOT EXISTS idx_lead_magnet_sources_canonical_url
            ON lead_magnet_sources(workspace_id, canonical_url)
            """,
        )
        self._ensure_index(
            "idx_lead_magnet_research_runs_lookup",
            ["workspace_id", "status", "period_key", "window_end"],
            """
            CREATE INDEX IF NOT EXISTS idx_lead_magnet_research_runs_lookup
            ON lead_magnet_research_runs(workspace_id, status, period_key, window_end DESC)
            """,
        )
        self._ensure_index(
            "idx_lead_magnet_research_runs_period_key",
            ["workspace_id", "period_key"],
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lead_magnet_research_runs_period_key
            ON lead_magnet_research_runs(workspace_id, period_key)
            """,
            unique=True,
        )
        self._ensure_index(
            "idx_lead_magnets_lookup",
            ["workspace_id", "status", "format", "created_at"],
            """
            CREATE INDEX IF NOT EXISTS idx_lead_magnets_lookup
            ON lead_magnets(workspace_id, status, format, created_at DESC)
            """,
        )
        self._ensure_index(
            "idx_lead_magnet_versions_lookup",
            ["workspace_id", "lead_magnet_id", "created_at"],
            """
            CREATE INDEX IF NOT EXISTS idx_lead_magnet_versions_lookup
            ON lead_magnet_versions(workspace_id, lead_magnet_id, created_at DESC)
            """,
        )

    def _ensure_index(
        self,
        index_name: str,
        columns: list[str],
        create_sql: str,
        *,
        unique: bool | None = None,
    ) -> None:
        existing_columns = self._index_columns(index_name)
        uniqueness_matches = unique is None or self._index_is_unique(index_name) == unique
        if existing_columns == columns and uniqueness_matches:
            return
        if existing_columns is not None:
            self.conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        self.conn.execute(create_sql)

    def _index_columns(self, index_name: str) -> list[str] | None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        if not row:
            return None
        rows = self.conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        return [row["name"] for row in rows]

    def _index_is_unique(self, index_name: str) -> bool:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        return bool(row and row["sql"] and "CREATE UNIQUE INDEX" in str(row["sql"]).upper())

    def _ensure_required_context_sections(self) -> None:
        for section in required_context_sections():
            exists = self.conn.execute(
                "SELECT 1 FROM context_sections WHERE workspace_id = ? AND kind = ? AND name = ?",
                (self.workspace_id, section.kind, section.name),
            ).fetchone()
            if exists:
                continue
            now = utc_now()
            self.conn.execute(
                """
                INSERT INTO context_sections (
                    id, workspace_id, kind, name, content, weight, active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    section.id,
                    self.workspace_id,
                    section.kind,
                    section.name,
                    section.content,
                    section.weight,
                    int(section.active),
                    now,
                    now,
                ),
            )

    def save_content_queue_item(self, item: ContentQueueItem) -> ContentQueueItem:
        now = utc_now()
        saved = ContentQueueItem(
            id=item.id,
            workspace_id=self.workspace_id,
            platform=item.platform,
            draft_id=item.draft_id,
            topic_idea_id=item.topic_idea_id,
            text=item.text,
            topic=item.topic,
            post_type=item.post_type,
            angle=item.angle,
            trend_ids=item.trend_ids,
            scheduled_at=item.scheduled_at,
            status=item.status,
            reviewer_notes=item.reviewer_notes,
            created_at=item.created_at,
            updated_at=now,
            approved_at=item.approved_at,
            posted_at=item.posted_at,
            x_post_id=item.x_post_id,
            x_url=item.x_url,
            failure_reason=item.failure_reason,
        )
        self.conn.execute(
            """
            INSERT INTO content_queue (
                id, workspace_id, platform, draft_id, topic_idea_id, text, topic, post_type, angle,
                trend_ids_json, scheduled_at, status, reviewer_notes,
                created_at, updated_at, approved_at, posted_at, x_post_id,
                x_url, failure_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                platform = excluded.platform,
                draft_id = excluded.draft_id,
                topic_idea_id = excluded.topic_idea_id,
                text = excluded.text,
                topic = excluded.topic,
                post_type = excluded.post_type,
                angle = excluded.angle,
                trend_ids_json = excluded.trend_ids_json,
                scheduled_at = excluded.scheduled_at,
                status = excluded.status,
                reviewer_notes = excluded.reviewer_notes,
                updated_at = excluded.updated_at,
                approved_at = excluded.approved_at,
                posted_at = excluded.posted_at,
                x_post_id = excluded.x_post_id,
                x_url = excluded.x_url,
                failure_reason = excluded.failure_reason
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.platform,
                saved.draft_id,
                saved.topic_idea_id,
                saved.text,
                saved.topic,
                saved.post_type,
                saved.angle,
                json.dumps(saved.trend_ids),
                saved.scheduled_at,
                saved.status,
                saved.reviewer_notes,
                saved.created_at,
                saved.updated_at,
                saved.approved_at,
                saved.posted_at,
                saved.x_post_id,
                saved.x_url,
                saved.failure_reason,
            ),
        )
        self.conn.commit()
        return saved

    def save_topic_idea(self, idea: TopicIdea) -> TopicIdea:
        now = utc_now()
        saved = TopicIdea(
            id=idea.id,
            workspace_id=self.workspace_id,
            title=idea.title,
            topic=idea.topic,
            angle=idea.angle,
            why_now=idea.why_now,
            source_type=idea.source_type,
            source_refs=list(idea.source_refs),
            evidence_basis=list(idea.evidence_basis),
            reader_value_mode=idea.reader_value_mode,
            post_type=idea.post_type,
            priority=max(0, min(100, int(idea.priority))),
            status=idea.status,
            created_at=idea.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO topic_ideas (
                id, workspace_id, title, topic, angle, why_now, source_type,
                source_refs_json, evidence_basis_json, reader_value_mode,
                post_type, priority, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                title = excluded.title,
                topic = excluded.topic,
                angle = excluded.angle,
                why_now = excluded.why_now,
                source_type = excluded.source_type,
                source_refs_json = excluded.source_refs_json,
                evidence_basis_json = excluded.evidence_basis_json,
                reader_value_mode = excluded.reader_value_mode,
                post_type = excluded.post_type,
                priority = excluded.priority,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.title,
                saved.topic,
                saved.angle,
                saved.why_now,
                saved.source_type,
                json.dumps(saved.source_refs),
                json.dumps(saved.evidence_basis),
                saved.reader_value_mode,
                saved.post_type,
                saved.priority,
                saved.status,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def get_topic_idea(self, idea_id: str) -> TopicIdea | None:
        row = self.conn.execute(
            "SELECT * FROM topic_ideas WHERE id = ? AND workspace_id = ?",
            (idea_id, self.workspace_id),
        ).fetchone()
        return self._row_to_topic_idea(row) if row else None

    def list_topic_ideas(self, *, status: str | None = "suggested", limit: int = 50) -> list[TopicIdea]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM topic_ideas
            WHERE {' AND '.join(clauses)}
            ORDER BY priority DESC, created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [self._row_to_topic_idea(row) for row in rows]

    def mark_topic_idea_status(self, idea_id: str, status: str) -> TopicIdea | None:
        self.conn.execute(
            "UPDATE topic_ideas SET status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (status, utc_now(), idea_id, self.workspace_id),
        )
        self.conn.commit()
        return self.get_topic_idea(idea_id)

    def get_content_queue_item(self, queue_id: str) -> ContentQueueItem | None:
        row = self.conn.execute(
            "SELECT * FROM content_queue WHERE id = ? AND workspace_id = ?",
            (queue_id, self.workspace_id),
        ).fetchone()
        return self._row_to_queue_item(row) if row else None

    def claim_content_queue_item_for_posting(self, queue_id: str) -> ContentQueueItem | None:
        """Atomically move one approved queue item into an in-flight state."""

        cursor = self.conn.execute(
            """
            UPDATE content_queue
            SET status = 'posting', updated_at = ?, failure_reason = NULL
            WHERE id = ?
              AND workspace_id = ?
              AND status = 'approved'
              AND x_post_id IS NULL
            """,
            (utc_now(), queue_id, self.workspace_id),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_content_queue_item(queue_id)

    def list_content_queue(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        scheduled_from: str | None = None,
        scheduled_to: str | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ContentQueueItem]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if scheduled_from:
            clauses.append("scheduled_at >= ?")
            params.append(scheduled_from)
        if scheduled_to:
            clauses.append("scheduled_at < ?")
            params.append(scheduled_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = (
            "updated_at DESC, created_at DESC, scheduled_at DESC, id DESC"
            if newest_first
            else "scheduled_at ASC, created_at ASC"
        )
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM content_queue
            {where}
            ORDER BY {order_by}
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [self._row_to_queue_item(row) for row in rows]

    def due_content_queue_items(
        self,
        *,
        platform: str = "x",
        now: str | None = None,
        limit: int = 20,
    ) -> list[ContentQueueItem]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM content_queue
            WHERE platform = ? AND status = 'approved' AND scheduled_at <= ?
              AND workspace_id = ?
            ORDER BY scheduled_at ASC, created_at ASC
            LIMIT ?
            """,
            (platform, now or utc_now(), self.workspace_id, limit),
        ).fetchall()
        return [self._row_to_queue_item(row) for row in rows]

    def save_queued_post_version(self, version: QueuedPostVersion) -> QueuedPostVersion:
        if not self.get_content_queue_item(version.queue_id):
            raise ValueError(f"No queued post found for id {version.queue_id}")
        saved = QueuedPostVersion(
            id=version.id,
            queue_id=version.queue_id,
            text=version.text,
            edit_type=version.edit_type,
            edit_instruction=version.edit_instruction,
            source_draft_id=version.source_draft_id,
            accepted_at=version.accepted_at,
            created_at=version.created_at,
        )
        self.conn.execute(
            """
            INSERT INTO queued_post_versions (
                id, queue_id, text, edit_type, edit_instruction,
                source_draft_id, accepted_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved.id,
                saved.queue_id,
                saved.text,
                saved.edit_type,
                saved.edit_instruction,
                saved.source_draft_id,
                saved.accepted_at,
                saved.created_at,
            ),
        )
        self.conn.commit()
        return saved

    def mark_queued_post_version_accepted(self, version_id: str) -> QueuedPostVersion | None:
        """Record which exact generated version became the queue's current copy."""

        accepted_at = utc_now()
        cursor = self.conn.execute(
            """
            UPDATE queued_post_versions
            SET accepted_at = ?
            WHERE id = ?
              AND EXISTS (
                  SELECT 1 FROM content_queue
                  WHERE content_queue.id = queued_post_versions.queue_id
                    AND content_queue.workspace_id = ?
              )
            """,
            (accepted_at, version_id, self.workspace_id),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_queued_post_version(version_id)

    def get_queued_post_version(self, version_id: str) -> QueuedPostVersion | None:
        row = self.conn.execute(
            """
            SELECT qv.*
            FROM queued_post_versions qv
            JOIN content_queue cq ON cq.id = qv.queue_id
            WHERE qv.id = ? AND cq.workspace_id = ?
            """,
            (version_id, self.workspace_id),
        ).fetchone()
        return self._row_to_queue_version(row) if row else None

    def list_queued_post_versions(self, queue_id: str, *, limit: int = 20) -> list[QueuedPostVersion]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM queued_post_versions
            WHERE queue_id = ?
              AND EXISTS (
                  SELECT 1 FROM content_queue
                  WHERE content_queue.id = queued_post_versions.queue_id
                    AND content_queue.workspace_id = ?
              )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (queue_id, self.workspace_id, limit),
        ).fetchall()
        return [self._row_to_queue_version(row) for row in rows]

    def save_web_auth_user(self, username: str, password: str, *, workspace_id: str | None = None) -> WebAuthUser:
        clean_username = username.strip()
        if not clean_username:
            raise ValueError("Username is required.")
        if not password:
            raise ValueError("Password is required.")
        existing = self.get_web_auth_user(clean_username)
        salt, password_hash, iterations = hash_password(password)
        now = utc_now()
        resolved_workspace_id = normalize_workspace_id(
            workspace_id
            or (existing.workspace_id if existing else None)
            or workspace_id_from_username(clean_username)
        )
        user = WebAuthUser(
            username=clean_username,
            workspace_id=resolved_workspace_id,
            password_hash=password_hash,
            salt=salt,
            iterations=iterations,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO web_auth_users (
                username, workspace_id, password_hash, salt, iterations, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                password_hash = excluded.password_hash,
                salt = excluded.salt,
                iterations = excluded.iterations,
                updated_at = excluded.updated_at
            """,
            (
                user.username,
                user.workspace_id,
                user.password_hash,
                user.salt,
                user.iterations,
                user.created_at,
                user.updated_at,
            ),
        )
        self.conn.commit()
        return user

    def get_web_auth_user(self, username: str) -> WebAuthUser | None:
        row = self.conn.execute(
            "SELECT * FROM web_auth_users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
        return self._row_to_web_auth_user(row) if row else None

    def has_web_auth_user(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM web_auth_users LIMIT 1").fetchone()
        return bool(row)

    def verify_web_auth_user(self, username: str, password: str) -> bool:
        user = self.get_web_auth_user(username)
        if not user or not password:
            return False
        _, candidate_hash, _ = hash_password(
            password,
            salt=user.salt,
            iterations=user.iterations,
        )
        return hmac.compare_digest(candidate_hash, user.password_hash)

    def get_app_setting(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM app_settings WHERE workspace_id = ? AND key = ?",
            (self.workspace_id, key.strip()),
        ).fetchone()
        return str(row["value"]) if row else None

    def save_app_setting(self, key: str, value: str | None) -> None:
        clean_key = key.strip()
        if not clean_key:
            raise ValueError("Setting key is required.")
        clean_value = str(value or "").strip()
        if not clean_value:
            self.conn.execute(
                "DELETE FROM app_settings WHERE workspace_id = ? AND key = ?",
                (self.workspace_id, clean_key),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO app_settings (workspace_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (self.workspace_id, clean_key, clean_value, utc_now()),
            )
        self.conn.commit()

    def save_sync_metadata(self, metadata: SyncMetadata) -> SyncMetadata:
        clean_key = metadata.key.strip()
        if not clean_key:
            raise ValueError("Sync metadata key is required.")
        saved = SyncMetadata(
            key=clean_key,
            workspace_id=self.workspace_id,
            last_started_at=metadata.last_started_at,
            last_completed_at=metadata.last_completed_at,
            status=metadata.status,
            summary=metadata.summary,
            error_message=metadata.error_message,
        )
        self.conn.execute(
            """
            INSERT INTO sync_metadata (
                workspace_id, key, last_started_at, last_completed_at, status,
                summary_json, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, key) DO UPDATE SET
                last_started_at = excluded.last_started_at,
                last_completed_at = excluded.last_completed_at,
                status = excluded.status,
                summary_json = excluded.summary_json,
                error_message = excluded.error_message
            """,
            (
                saved.workspace_id,
                saved.key,
                saved.last_started_at,
                saved.last_completed_at,
                saved.status,
                json.dumps(saved.summary),
                saved.error_message,
            ),
        )
        self.conn.commit()
        return saved

    def get_sync_metadata(self, key: str) -> SyncMetadata | None:
        row = self.conn.execute(
            "SELECT * FROM sync_metadata WHERE workspace_id = ? AND key = ?",
            (self.workspace_id, key.strip()),
        ).fetchone()
        return self._row_to_sync_metadata(row) if row else None

    def sync_metadata_by_key(self) -> dict[str, SyncMetadata]:
        rows = self.conn.execute(
            "SELECT * FROM sync_metadata WHERE workspace_id = ?",
            (self.workspace_id,),
        ).fetchall()
        return {row["key"]: self._row_to_sync_metadata(row) for row in rows}

    def count_posts(self, *, platform: str | None = None, status: str | None = None) -> int:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.conn.execute(f"SELECT COUNT(*) AS count FROM posts {where}", params).fetchone()
        return int(row["count"] or 0)

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _has_unique_index(self, table: str, columns: list[str]) -> bool:
        for index in self.conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not bool(index["unique"]):
                continue
            index_name = index["name"]
            rows = self.conn.execute(f"PRAGMA index_info({index_name})").fetchall()
            if [row["name"] for row in rows] == columns:
                return True
        return False

    def _rebuild_patterns_table(self) -> None:
        self.conn.execute("ALTER TABLE patterns RENAME TO patterns_old")
        self.conn.execute(
            """
            CREATE TABLE patterns (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                example_post_ids TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, name)
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO patterns (
                id, workspace_id, name, category, description, example_post_ids,
                active, created_at, updated_at
            )
            SELECT id, COALESCE(workspace_id, ?), name, category, description,
                   example_post_ids, active, created_at, updated_at
            FROM patterns_old
            """,
            (DEFAULT_WORKSPACE_ID,),
        )
        self.conn.execute("DROP TABLE patterns_old")

    def _rebuild_context_sections_table(self) -> None:
        self.conn.execute("ALTER TABLE context_sections RENAME TO context_sections_old")
        self.conn.execute(
            """
            CREATE TABLE context_sections (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, kind, name)
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO context_sections (
                id, workspace_id, kind, name, content, weight, active,
                created_at, updated_at
            )
            SELECT id, COALESCE(workspace_id, ?), kind, name, content, weight,
                   active, created_at, updated_at
            FROM context_sections_old
            """,
            (DEFAULT_WORKSPACE_ID,),
        )
        self.conn.execute("DROP TABLE context_sections_old")

    def _rebuild_app_settings_table(self) -> None:
        self.conn.execute("ALTER TABLE app_settings RENAME TO app_settings_old")
        self.conn.execute(
            """
            CREATE TABLE app_settings (
                workspace_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, key)
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO app_settings (workspace_id, key, value, updated_at)
            SELECT COALESCE(workspace_id, ?), key, value, updated_at
            FROM app_settings_old
            """,
            (DEFAULT_WORKSPACE_ID,),
        )
        self.conn.execute("DROP TABLE app_settings_old")

    def _rebuild_sync_metadata_table(self) -> None:
        self.conn.execute("ALTER TABLE sync_metadata RENAME TO sync_metadata_old")
        self.conn.execute(
            """
            CREATE TABLE sync_metadata (
                workspace_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                last_started_at TEXT,
                last_completed_at TEXT,
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                PRIMARY KEY (workspace_id, key)
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO sync_metadata (
                workspace_id, key, last_started_at, last_completed_at, status,
                summary_json, error_message
            )
            SELECT COALESCE(workspace_id, ?), key, last_started_at,
                   last_completed_at, status, summary_json, error_message
            FROM sync_metadata_old
            """,
            (DEFAULT_WORKSPACE_ID,),
        )
        self.conn.execute("DROP TABLE sync_metadata_old")

    def save_post(self, post: SocialPost) -> SocialPost:
        now = utc_now()
        saved = SocialPost(
            id=post.id,
            workspace_id=self.workspace_id,
            platform=post.platform,
            text=post.text,
            url=post.url,
            published_at=post.published_at,
            author=post.author,
            topic=post.topic,
            post_type=post.post_type,
            status=post.status,
            source_draft_id=post.source_draft_id,
            created_at=post.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO posts (
                id, workspace_id, platform, text, url, published_at, author, topic, post_type,
                status, source_draft_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                platform = excluded.platform,
                text = excluded.text,
                url = excluded.url,
                published_at = excluded.published_at,
                author = excluded.author,
                topic = excluded.topic,
                post_type = excluded.post_type,
                status = excluded.status,
                source_draft_id = excluded.source_draft_id,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.platform,
                saved.text,
                saved.url,
                saved.published_at,
                saved.author,
                saved.topic,
                saved.post_type,
                saved.status,
                saved.source_draft_id,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def add_metrics(self, post_id: str, metrics: EngagementMetrics) -> EngagementMetrics:
        if not self.get_post(post_id):
            raise ValueError(f"No post found for id {post_id}")
        normalized = metrics.normalized()
        self.conn.execute(
            """
            INSERT INTO post_metrics (
                id, post_id, measured_at, impressions, likes, replies, reposts,
                quotes, bookmarks, engagement_rate, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                post_id,
                normalized.measured_at,
                normalized.impressions,
                normalized.likes,
                normalized.replies,
                normalized.reposts,
                normalized.quotes,
                normalized.bookmarks,
                normalized.engagement_rate,
                utc_now(),
            ),
        )
        self.conn.commit()
        return normalized

    def save_note(self, note: PostNote) -> PostNote:
        if not self.get_post(note.post_id):
            raise ValueError(f"No post found for id {note.post_id}")
        saved = PostNote(
            post_id=note.post_id,
            worked_reason=note.worked_reason,
            pattern_name=note.pattern_name,
            hook=note.hook,
            structure=note.structure,
            tone=note.tone,
            cta=note.cta,
            formatting=note.formatting,
            imitate=note.imitate,
            updated_at=utc_now(),
        )
        self.conn.execute(
            """
            INSERT INTO post_notes (
                post_id, worked_reason, pattern_name, hook, structure, tone,
                cta, formatting, imitate, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                worked_reason = excluded.worked_reason,
                pattern_name = excluded.pattern_name,
                hook = excluded.hook,
                structure = excluded.structure,
                tone = excluded.tone,
                cta = excluded.cta,
                formatting = excluded.formatting,
                imitate = excluded.imitate,
                updated_at = excluded.updated_at
            """,
            (
                saved.post_id,
                saved.worked_reason,
                saved.pattern_name,
                saved.hook,
                saved.structure,
                saved.tone,
                saved.cta,
                saved.formatting,
                int(saved.imitate),
                saved.updated_at,
            ),
        )
        self._sync_note_influences(saved)
        self.conn.commit()
        return saved

    def save_pattern(self, pattern: Pattern) -> Pattern:
        now = utc_now()
        saved = Pattern(
            id=pattern.id,
            workspace_id=self.workspace_id,
            name=pattern.name,
            category=pattern.category,
            description=pattern.description,
            example_post_ids=pattern.example_post_ids,
            active=pattern.active,
            created_at=pattern.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO patterns (
                id, workspace_id, name, category, description, example_post_ids, active,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, name) DO UPDATE SET
                category = excluded.category,
                description = excluded.description,
                example_post_ids = excluded.example_post_ids,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.name,
                saved.category,
                saved.description,
                json.dumps(saved.example_post_ids),
                int(saved.active),
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def save_context_section(self, section: ContextSection) -> ContextSection:
        now = utc_now()
        saved = ContextSection(
            id=section.id,
            workspace_id=self.workspace_id,
            kind=section.kind,
            name=section.name,
            content=section.content,
            weight=section.weight,
            active=section.active,
            created_at=section.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO context_sections (
                id, workspace_id, kind, name, content, weight, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, kind, name) DO UPDATE SET
                content = excluded.content,
                weight = excluded.weight,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.kind,
                saved.name,
                saved.content,
                saved.weight,
                int(saved.active),
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def save_generated_draft(self, draft: GeneratedDraft) -> GeneratedDraft:
        saved = GeneratedDraft(
            id=draft.id,
            workspace_id=self.workspace_id,
            text=draft.text,
            request=draft.request,
            explanation=draft.explanation,
            influences=draft.influences,
            context_snapshot=draft.context_snapshot,
            created_at=draft.created_at,
        )
        self.conn.execute(
            """
            INSERT INTO generated_drafts (
                id, workspace_id, platform, topic, post_type, request_json, text, explanation,
                influences_json, context_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.request.platform,
                saved.request.topic,
                saved.request.post_type,
                json.dumps(saved.request.__dict__),
                saved.text,
                saved.explanation,
                json.dumps(saved.influences),
                json.dumps(saved.context_snapshot),
                saved.created_at,
            ),
        )
        self.conn.commit()
        return saved

    def save_trend_item(self, trend: TrendItem) -> TrendItem:
        now = utc_now()
        saved = TrendItem(
            id=trend.id,
            workspace_id=self.workspace_id,
            title=trend.title,
            source_url=trend.source_url,
            source_type=trend.source_type,
            topic=trend.topic,
            summary=trend.summary,
            why_it_matters=trend.why_it_matters,
            leadpoet_angle=trend.leadpoet_angle,
            suggested_hooks=trend.suggested_hooks,
            urgency=trend.urgency,
            date=trend.date,
            status=trend.status,
            engagement_notes=trend.engagement_notes,
            source_author=trend.source_author,
            raw_text=trend.raw_text,
            source_title=trend.source_title,
            source_published_at=trend.source_published_at,
            source_excerpt=trend.source_excerpt,
            source_text_excerpt=trend.source_text_excerpt,
            key_points_json=trend.key_points_json,
            important_claims_json=trend.important_claims_json,
            post_angles_json=trend.post_angles_json,
            source_confidence=trend.source_confidence,
            enriched_at=trend.enriched_at,
            enrichment_status=trend.enrichment_status or "not_attempted",
            enrichment_error=trend.enrichment_error,
            created_at=trend.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO trend_items (
                id, workspace_id, title, source_url, source_type, topic, summary, why_it_matters,
                leadpoet_angle, suggested_hooks, urgency, date, status,
                engagement_notes, source_author, raw_text, source_title,
                source_published_at, source_excerpt, source_text_excerpt,
                key_points_json, important_claims_json, post_angles_json,
                source_confidence, enriched_at, enrichment_status,
                enrichment_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                title = excluded.title,
                source_url = excluded.source_url,
                source_type = excluded.source_type,
                topic = excluded.topic,
                summary = excluded.summary,
                why_it_matters = excluded.why_it_matters,
                leadpoet_angle = excluded.leadpoet_angle,
                suggested_hooks = excluded.suggested_hooks,
                urgency = excluded.urgency,
                date = excluded.date,
                status = excluded.status,
                engagement_notes = excluded.engagement_notes,
                source_author = excluded.source_author,
                raw_text = excluded.raw_text,
                source_title = excluded.source_title,
                source_published_at = excluded.source_published_at,
                source_excerpt = excluded.source_excerpt,
                source_text_excerpt = excluded.source_text_excerpt,
                key_points_json = excluded.key_points_json,
                important_claims_json = excluded.important_claims_json,
                post_angles_json = excluded.post_angles_json,
                source_confidence = excluded.source_confidence,
                enriched_at = excluded.enriched_at,
                enrichment_status = excluded.enrichment_status,
                enrichment_error = excluded.enrichment_error,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.title,
                saved.source_url,
                saved.source_type,
                saved.topic,
                saved.summary,
                saved.why_it_matters,
                saved.leadpoet_angle,
                saved.suggested_hooks,
                saved.urgency,
                saved.date,
                saved.status,
                saved.engagement_notes,
                saved.source_author,
                saved.raw_text,
                saved.source_title,
                saved.source_published_at,
                saved.source_excerpt,
                saved.source_text_excerpt,
                saved.key_points_json,
                saved.important_claims_json,
                saved.post_angles_json,
                saved.source_confidence,
                saved.enriched_at,
                saved.enrichment_status,
                saved.enrichment_error,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def save_lead_magnet_source(self, source: LeadMagnetSource) -> LeadMagnetSource:
        now = utc_now()
        saved = LeadMagnetSource(
            id=source.id,
            workspace_id=self.workspace_id,
            title=source.title,
            source_url=source.source_url,
            canonical_url=source.canonical_url,
            platform=source.platform or "web",
            author=source.author,
            published_at=source.published_at,
            collected_at=source.collected_at or now,
            format=source.format or "other",
            sales_topic=source.sales_topic,
            hook=source.hook,
            promise=source.promise,
            audience=source.audience,
            delivery_mechanism=source.delivery_mechanism,
            cta=source.cta,
            leadpoet_integration=source.leadpoet_integration,
            why_it_worked=source.why_it_worked,
            likes=source.likes,
            replies=source.replies,
            reposts=source.reposts,
            bookmarks=source.bookmarks,
            impressions=source.impressions,
            follower_count=source.follower_count,
            metric_observed_at=source.metric_observed_at,
            performance_score=source.performance_score,
            evidence_strength=source.evidence_strength or "unknown",
            raw_text=source.raw_text,
            status=source.status or "candidate",
            created_at=source.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO lead_magnet_sources (
                id, workspace_id, title, source_url, canonical_url, platform,
                author, published_at, collected_at, format, sales_topic, hook,
                promise, audience, delivery_mechanism, cta,
                leadpoet_integration, why_it_worked, likes, replies, reposts,
                bookmarks, impressions, follower_count, metric_observed_at,
                performance_score, evidence_strength, raw_text, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                title = excluded.title,
                source_url = excluded.source_url,
                canonical_url = excluded.canonical_url,
                platform = excluded.platform,
                author = excluded.author,
                published_at = excluded.published_at,
                collected_at = excluded.collected_at,
                format = excluded.format,
                sales_topic = excluded.sales_topic,
                hook = excluded.hook,
                promise = excluded.promise,
                audience = excluded.audience,
                delivery_mechanism = excluded.delivery_mechanism,
                cta = excluded.cta,
                leadpoet_integration = excluded.leadpoet_integration,
                why_it_worked = excluded.why_it_worked,
                likes = excluded.likes,
                replies = excluded.replies,
                reposts = excluded.reposts,
                bookmarks = excluded.bookmarks,
                impressions = excluded.impressions,
                follower_count = excluded.follower_count,
                metric_observed_at = excluded.metric_observed_at,
                performance_score = excluded.performance_score,
                evidence_strength = excluded.evidence_strength,
                raw_text = excluded.raw_text,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.title,
                saved.source_url,
                saved.canonical_url,
                saved.platform,
                saved.author,
                saved.published_at,
                saved.collected_at,
                saved.format,
                saved.sales_topic,
                saved.hook,
                saved.promise,
                saved.audience,
                saved.delivery_mechanism,
                saved.cta,
                saved.leadpoet_integration,
                saved.why_it_worked,
                saved.likes,
                saved.replies,
                saved.reposts,
                saved.bookmarks,
                saved.impressions,
                saved.follower_count,
                saved.metric_observed_at,
                saved.performance_score,
                saved.evidence_strength,
                saved.raw_text,
                saved.status,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def get_lead_magnet_source(self, source_id: str) -> LeadMagnetSource | None:
        row = self.conn.execute(
            "SELECT * FROM lead_magnet_sources WHERE id = ? AND workspace_id = ?",
            (source_id, self.workspace_id),
        ).fetchone()
        return self._row_to_lead_magnet_source(row) if row else None

    def get_lead_magnet_source_by_canonical_url(self, canonical_url: str) -> LeadMagnetSource | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM lead_magnet_sources
            WHERE workspace_id = ? AND canonical_url = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (self.workspace_id, canonical_url),
        ).fetchone()
        return self._row_to_lead_magnet_source(row) if row else None

    def list_lead_magnet_sources(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        format: str | None = None,
        sales_topic: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        min_performance_score: float | None = None,
        limit: int | None = 100,
    ) -> list[LeadMagnetSource]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if format:
            clauses.append("format = ?")
            params.append(format)
        if sales_topic:
            clauses.append("(sales_topic = ? OR sales_topic LIKE ?)")
            params.extend([sales_topic, f"%{sales_topic}%"])
        if published_after:
            clauses.append("published_at >= ?")
            params.append(published_after)
        if published_before:
            clauses.append("published_at <= ?")
            params.append(published_before)
        if min_performance_score is not None:
            clauses.append("performance_score >= ?")
            params.append(min_performance_score)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM lead_magnet_sources
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE WHEN performance_score IS NULL THEN 1 ELSE 0 END,
                performance_score DESC,
                COALESCE(published_at, collected_at, created_at) DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_lead_magnet_source(row) for row in rows]

    def mark_lead_magnet_source_status(self, source_id: str, status: str) -> LeadMagnetSource | None:
        self.conn.execute(
            """
            UPDATE lead_magnet_sources
            SET status = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ?
            """,
            (status, utc_now(), source_id, self.workspace_id),
        )
        self.conn.commit()
        return self.get_lead_magnet_source(source_id)

    def delete_lead_magnet_source(self, source_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM lead_magnet_sources WHERE id = ? AND workspace_id = ?",
            (source_id, self.workspace_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def save_lead_magnet_research_run(self, run: LeadMagnetResearchRun) -> LeadMagnetResearchRun:
        period_key = run.period_key.strip()
        if not period_key:
            raise ValueError("Lead magnet research period_key is required.")
        now = utc_now()
        saved = LeadMagnetResearchRun(
            id=run.id,
            workspace_id=self.workspace_id,
            window_start=run.window_start,
            window_end=run.window_end,
            period_key=period_key,
            query=run.query,
            provider=run.provider,
            status=run.status or "running",
            candidates_found=max(0, int(run.candidates_found)),
            sources_qualified=max(0, int(run.sources_qualified)),
            source_ids=list(run.source_ids or []),
            warnings=list(run.warnings or []),
            error_message=run.error_message,
            started_at=run.started_at or now,
            completed_at=run.completed_at,
            created_at=run.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO lead_magnet_research_runs (
                id, workspace_id, window_start, window_end, period_key, query,
                provider, status, candidates_found, sources_qualified,
                source_ids_json, warnings_json, error_message, started_at,
                completed_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, period_key) DO UPDATE SET
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                query = excluded.query,
                provider = excluded.provider,
                status = excluded.status,
                candidates_found = excluded.candidates_found,
                sources_qualified = excluded.sources_qualified,
                source_ids_json = excluded.source_ids_json,
                warnings_json = excluded.warnings_json,
                error_message = excluded.error_message,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.window_start,
                saved.window_end,
                saved.period_key,
                saved.query,
                saved.provider,
                saved.status,
                saved.candidates_found,
                saved.sources_qualified,
                json.dumps(saved.source_ids),
                json.dumps(saved.warnings),
                saved.error_message,
                saved.started_at,
                saved.completed_at,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        persisted = self.get_lead_magnet_research_run_by_period_key(saved.period_key)
        if persisted is None:
            raise RuntimeError(f"Lead magnet research run was not saved for period {saved.period_key}")
        return persisted

    def get_lead_magnet_research_run(self, run_id: str) -> LeadMagnetResearchRun | None:
        row = self.conn.execute(
            "SELECT * FROM lead_magnet_research_runs WHERE id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
        ).fetchone()
        return self._row_to_lead_magnet_research_run(row) if row else None

    def get_lead_magnet_research_run_by_period_key(self, period_key: str) -> LeadMagnetResearchRun | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM lead_magnet_research_runs
            WHERE workspace_id = ? AND period_key = ?
            ORDER BY started_at DESC, created_at DESC
            LIMIT 1
            """,
            (self.workspace_id, period_key),
        ).fetchone()
        return self._row_to_lead_magnet_research_run(row) if row else None

    def list_lead_magnet_research_runs(
        self,
        *,
        status: str | None = None,
        period_key: str | None = None,
        limit: int | None = 50,
    ) -> list[LeadMagnetResearchRun]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if period_key:
            clauses.append("period_key = ?")
            params.append(period_key)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM lead_magnet_research_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY started_at DESC, created_at DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_lead_magnet_research_run(row) for row in rows]

    def mark_lead_magnet_research_run_status(
        self,
        run_id: str,
        status: str,
        *,
        completed_at: str | None = None,
        error_message: str | None = None,
    ) -> LeadMagnetResearchRun | None:
        now = utc_now()
        completed = completed_at
        if completed is None and status in {"success", "completed", "partial", "failed"}:
            completed = now
        self.conn.execute(
            """
            UPDATE lead_magnet_research_runs
            SET status = ?, completed_at = COALESCE(?, completed_at),
                error_message = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ?
            """,
            (status, completed, error_message, now, run_id, self.workspace_id),
        )
        self.conn.commit()
        return self.get_lead_magnet_research_run(run_id)

    def delete_lead_magnet_research_run(self, run_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM lead_magnet_research_runs WHERE id = ? AND workspace_id = ?",
            (run_id, self.workspace_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def save_lead_magnet(self, magnet: LeadMagnet) -> LeadMagnet:
        if magnet.research_run_id and not self.get_lead_magnet_research_run(magnet.research_run_id):
            raise ValueError(f"No lead magnet research run found for id {magnet.research_run_id}")
        now = utc_now()
        saved = LeadMagnet(
            id=magnet.id,
            workspace_id=self.workspace_id,
            title=magnet.title,
            promise=magnet.promise or "",
            audience=magnet.audience or "",
            sales_outcome=magnet.sales_outcome or "",
            format=magnet.format or "notion_prompt_repo",
            depth=magnet.depth or "standard",
            product_integration=magnet.product_integration or "",
            cta=magnet.cta or "",
            status=magnet.status or "draft",
            source_ids=list(magnet.source_ids or []),
            research_run_id=magnet.research_run_id,
            evidence_summary=magnet.evidence_summary,
            content=dict(magnet.content or {}),
            markdown=magnet.markdown or "",
            generation_brief=dict(magnet.generation_brief or {}),
            notion_page_id=magnet.notion_page_id,
            notion_url=magnet.notion_url,
            published_at=magnet.published_at,
            failure_reason=magnet.failure_reason,
            created_at=magnet.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO lead_magnets (
                id, workspace_id, title, promise, audience, sales_outcome,
                format, depth, product_integration, cta, status,
                source_ids_json, research_run_id, evidence_summary,
                content_json, markdown, generation_brief_json, notion_page_id,
                notion_url, published_at, failure_reason, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                title = excluded.title,
                promise = excluded.promise,
                audience = excluded.audience,
                sales_outcome = excluded.sales_outcome,
                format = excluded.format,
                depth = excluded.depth,
                product_integration = excluded.product_integration,
                cta = excluded.cta,
                status = excluded.status,
                source_ids_json = excluded.source_ids_json,
                research_run_id = excluded.research_run_id,
                evidence_summary = excluded.evidence_summary,
                content_json = excluded.content_json,
                markdown = excluded.markdown,
                generation_brief_json = excluded.generation_brief_json,
                notion_page_id = excluded.notion_page_id,
                notion_url = excluded.notion_url,
                published_at = excluded.published_at,
                failure_reason = excluded.failure_reason,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.title,
                saved.promise,
                saved.audience,
                saved.sales_outcome,
                saved.format,
                saved.depth,
                saved.product_integration,
                saved.cta,
                saved.status,
                json.dumps(saved.source_ids),
                saved.research_run_id,
                saved.evidence_summary,
                json.dumps(saved.content),
                saved.markdown,
                json.dumps(saved.generation_brief),
                saved.notion_page_id,
                saved.notion_url,
                saved.published_at,
                saved.failure_reason,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def get_lead_magnet(self, lead_magnet_id: str) -> LeadMagnet | None:
        row = self.conn.execute(
            "SELECT * FROM lead_magnets WHERE id = ? AND workspace_id = ?",
            (lead_magnet_id, self.workspace_id),
        ).fetchone()
        return self._row_to_lead_magnet(row) if row else None

    def list_lead_magnets(
        self,
        *,
        status: str | None = None,
        format: str | None = None,
        research_run_id: str | None = None,
        limit: int | None = 50,
    ) -> list[LeadMagnet]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if format:
            clauses.append("format = ?")
            params.append(format)
        if research_run_id:
            clauses.append("research_run_id = ?")
            params.append(research_run_id)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM lead_magnets
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(published_at, updated_at, created_at) DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_lead_magnet(row) for row in rows]

    def mark_lead_magnet_status(
        self,
        lead_magnet_id: str,
        status: str,
        *,
        published_at: str | None = None,
        failure_reason: str | None = None,
    ) -> LeadMagnet | None:
        now = utc_now()
        published = published_at
        if published is None and status == "published":
            published = now
        self.conn.execute(
            """
            UPDATE lead_magnets
            SET status = ?, published_at = COALESCE(?, published_at),
                failure_reason = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ?
            """,
            (status, published, failure_reason, now, lead_magnet_id, self.workspace_id),
        )
        self.conn.commit()
        return self.get_lead_magnet(lead_magnet_id)

    def delete_lead_magnet(self, lead_magnet_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM lead_magnets WHERE id = ? AND workspace_id = ?",
            (lead_magnet_id, self.workspace_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def save_lead_magnet_version(self, version: LeadMagnetVersion) -> LeadMagnetVersion:
        if not self.get_lead_magnet(version.lead_magnet_id):
            raise ValueError(f"No lead magnet found for id {version.lead_magnet_id}")
        now = utc_now()
        saved = LeadMagnetVersion(
            id=version.id,
            workspace_id=self.workspace_id,
            lead_magnet_id=version.lead_magnet_id,
            content=dict(version.content or {}),
            markdown=version.markdown or "",
            edit_type=version.edit_type or "generated",
            instruction=version.instruction,
            source_ids=list(version.source_ids or []),
            created_at=version.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO lead_magnet_versions (
                id, workspace_id, lead_magnet_id, content_json, markdown,
                edit_type, instruction, source_ids_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                lead_magnet_id = excluded.lead_magnet_id,
                content_json = excluded.content_json,
                markdown = excluded.markdown,
                edit_type = excluded.edit_type,
                instruction = excluded.instruction,
                source_ids_json = excluded.source_ids_json,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.lead_magnet_id,
                json.dumps(saved.content),
                saved.markdown,
                saved.edit_type,
                saved.instruction,
                json.dumps(saved.source_ids),
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def get_lead_magnet_version(self, version_id: str) -> LeadMagnetVersion | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM lead_magnet_versions
            WHERE id = ? AND workspace_id = ?
            """,
            (version_id, self.workspace_id),
        ).fetchone()
        return self._row_to_lead_magnet_version(row) if row else None

    def list_lead_magnet_versions(
        self,
        lead_magnet_id: str | None = None,
        *,
        limit: int | None = 50,
    ) -> list[LeadMagnetVersion]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if lead_magnet_id:
            clauses.append("lead_magnet_id = ?")
            params.append(lead_magnet_id)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM lead_magnet_versions
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_lead_magnet_version(row) for row in rows]

    def latest_lead_magnet_version(self, lead_magnet_id: str) -> LeadMagnetVersion | None:
        versions = self.list_lead_magnet_versions(lead_magnet_id, limit=1)
        return versions[0] if versions else None

    def delete_lead_magnet_version(self, version_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM lead_magnet_versions WHERE id = ? AND workspace_id = ?",
            (version_id, self.workspace_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def save_viral_pattern(self, pattern: ViralPattern) -> ViralPattern:
        now = utc_now()
        saved = ViralPattern(
            id=pattern.id,
            workspace_id=self.workspace_id,
            platform=pattern.platform or "x",
            source_url=pattern.source_url,
            source_author=pattern.source_author,
            source_text=pattern.source_text,
            source_summary=pattern.source_summary,
            topic=pattern.topic,
            category=pattern.category,
            pattern_name=pattern.pattern_name,
            hook_style=pattern.hook_style,
            structure=pattern.structure,
            pacing=pattern.pacing,
            tone=pattern.tone,
            formatting=pattern.formatting,
            why_it_worked=pattern.why_it_worked,
            adaptation_rule=pattern.adaptation_rule,
            engagement_notes=pattern.engagement_notes,
            likes=pattern.likes,
            replies=pattern.replies,
            reposts=pattern.reposts,
            quotes=pattern.quotes,
            bookmarks=pattern.bookmarks,
            impressions=pattern.impressions,
            collected_at=pattern.collected_at or now,
            window=pattern.window or "manual",
            status=pattern.status or "active",
            raw_text=pattern.raw_text,
            created_at=pattern.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO pattern_examples (
                id, workspace_id, platform, source_url, source_author, source_text,
                source_summary, topic, category, pattern_name, hook_style,
                structure, pacing, tone, formatting, why_it_worked,
                adaptation_rule, engagement_notes, likes, replies, reposts,
                quotes, bookmarks, impressions, collected_at, window, status,
                raw_text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                workspace_id = excluded.workspace_id,
                platform = excluded.platform,
                source_url = excluded.source_url,
                source_author = excluded.source_author,
                source_text = excluded.source_text,
                source_summary = excluded.source_summary,
                topic = excluded.topic,
                category = excluded.category,
                pattern_name = excluded.pattern_name,
                hook_style = excluded.hook_style,
                structure = excluded.structure,
                pacing = excluded.pacing,
                tone = excluded.tone,
                formatting = excluded.formatting,
                why_it_worked = excluded.why_it_worked,
                adaptation_rule = excluded.adaptation_rule,
                engagement_notes = excluded.engagement_notes,
                likes = excluded.likes,
                replies = excluded.replies,
                reposts = excluded.reposts,
                quotes = excluded.quotes,
                bookmarks = excluded.bookmarks,
                impressions = excluded.impressions,
                collected_at = excluded.collected_at,
                window = excluded.window,
                status = excluded.status,
                raw_text = excluded.raw_text,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
                saved.workspace_id,
                saved.platform,
                saved.source_url,
                saved.source_author,
                saved.source_text,
                saved.source_summary,
                saved.topic,
                saved.category,
                saved.pattern_name,
                saved.hook_style,
                saved.structure,
                saved.pacing,
                saved.tone,
                saved.formatting,
                saved.why_it_worked,
                saved.adaptation_rule,
                saved.engagement_notes,
                saved.likes,
                saved.replies,
                saved.reposts,
                saved.quotes,
                saved.bookmarks,
                saved.impressions,
                saved.collected_at,
                saved.window,
                saved.status,
                saved.raw_text,
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def get_viral_pattern(self, pattern_id: str) -> ViralPattern | None:
        row = self.conn.execute(
            "SELECT * FROM pattern_examples WHERE id = ? AND workspace_id = ?",
            (pattern_id, self.workspace_id),
        ).fetchone()
        return self._row_to_viral_pattern(row) if row else None

    def list_viral_patterns(
        self,
        *,
        status: str | None = "active",
        window: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        limit: int | None = 20,
    ) -> list[ViralPattern]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if window:
            clauses.append("window = ?")
            params.append(window)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if topic:
            clauses.append("(topic = ? OR topic LIKE ? OR category LIKE ?)")
            params.extend([topic, f"%{topic}%", f"%{topic}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM pattern_examples
            {where}
            ORDER BY collected_at DESC, updated_at DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_viral_pattern(row) for row in rows]

    def mark_viral_pattern_status(self, pattern_id: str, status: str) -> ViralPattern | None:
        self.conn.execute(
            "UPDATE pattern_examples SET status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (status, utc_now(), pattern_id, self.workspace_id),
        )
        self.conn.commit()
        return self.get_viral_pattern(pattern_id)

    def list_trends(
        self,
        *,
        topic: str | None = None,
        status: str | None = None,
        urgency: str | None = None,
        limit: int = 20,
    ) -> list[TrendItem]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if topic:
            clauses.append("(topic = ? OR topic LIKE ? OR title LIKE ? OR summary LIKE ?)")
            params.extend([topic, f"%{topic}%", f"%{topic}%", f"%{topic}%"])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if urgency:
            clauses.append("urgency = ?")
            params.append(urgency)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM trend_items
            {where}
            ORDER BY
                CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                COALESCE(date, created_at) DESC,
                created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [self._row_to_trend(row) for row in rows]

    def get_trend_item(self, trend_id: str) -> TrendItem | None:
        row = self.conn.execute(
            "SELECT * FROM trend_items WHERE id = ? AND workspace_id = ?",
            (trend_id, self.workspace_id),
        ).fetchone()
        return self._row_to_trend(row) if row else None

    def relevant_trends(
        self,
        *,
        topic: str,
        angle: str | None = None,
        limit: int = 3,
    ) -> list[TrendItem]:
        candidates = self.list_trends(topic=topic, status="unused", limit=20)
        if not candidates:
            candidates = self.list_trends(status="unused", limit=20)
        terms = _keywords(f"{topic} {angle or ''}")
        scored: list[tuple[int, TrendItem]] = []
        for trend in candidates:
            haystack = " ".join(
                [
                    trend.title,
                    trend.topic,
                    trend.summary,
                    trend.why_it_matters,
                    trend.leadpoet_angle,
                    trend.suggested_hooks or "",
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if trend.urgency == "high":
                score += 2
            elif trend.urgency == "medium":
                score += 1
            scored.append((score, trend))
        return [trend for _, trend in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]

    def mark_trend_status(self, trend_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE trend_items SET status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (status, utc_now(), trend_id, self.workspace_id),
        )
        self.conn.commit()

    def list_generated_drafts(
        self,
        *,
        platform: str | None = None,
        limit: int | None = 100,
    ) -> list[GeneratedDraft]:
        clauses = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(max(0, int(limit)))
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM generated_drafts
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        drafts: list[GeneratedDraft] = []
        for row in rows:
            drafts.append(
                GeneratedDraft(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    text=row["text"],
                    request=DraftRequest(**json.loads(row["request_json"])),
                    explanation=row["explanation"],
                    influences=json.loads(row["influences_json"]),
                    context_snapshot=json.loads(row["context_snapshot_json"] or "{}"),
                    created_at=row["created_at"],
                )
            )
        return drafts

    def get_generated_draft(self, draft_id: str) -> GeneratedDraft | None:
        row = self.conn.execute(
            "SELECT * FROM generated_drafts WHERE id = ? AND workspace_id = ?",
            (draft_id, self.workspace_id),
        ).fetchone()
        if not row:
            return None
        request = DraftRequest(**json.loads(row["request_json"]))
        return GeneratedDraft(
            id=row["id"],
            workspace_id=row["workspace_id"],
            text=row["text"],
            request=request,
            explanation=row["explanation"],
            influences=json.loads(row["influences_json"]),
            context_snapshot=json.loads(row["context_snapshot_json"] or "{}"),
            created_at=row["created_at"],
        )

    def save_post_influences(self, influences: Iterable[PostInfluence]) -> None:
        rows = [
            influence
            for influence in influences
            if self.get_post(influence.post_id)
        ]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO post_influences (
                id, post_id, source_draft_id, influence_type, category, name,
                ref_id, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    influence.id,
                    influence.post_id,
                    influence.source_draft_id,
                    influence.influence_type,
                    influence.category or "",
                    influence.name,
                    influence.ref_id or "",
                    json.dumps(influence.metadata),
                    influence.created_at,
                )
                for influence in rows
            ],
        )
        self.conn.commit()

    def link_post_to_draft(self, post_id: str, draft: GeneratedDraft) -> None:
        self.save_post_influences(draft_influences_for_post(post_id, draft))

    def list_post_influences(
        self,
        post_ids: Iterable[str] | None = None,
        *,
        influence_type: str | None = None,
        category: str | None = None,
    ) -> list[PostInfluence]:
        clauses: list[str] = []
        params: list[object] = []
        ids = list(post_ids or [])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            clauses.append(f"post_id IN ({placeholders})")
            params.extend(ids)
        if influence_type:
            clauses.append("influence_type = ?")
            params.append(influence_type)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT pi.*
            FROM post_influences pi
            JOIN posts p ON p.id = pi.post_id
            {where}
            {"AND" if where else "WHERE"} p.workspace_id = ?
            ORDER BY pi.created_at DESC
            """,
            [*params, self.workspace_id],
        ).fetchall()
        return [self._row_to_influence(row) for row in rows]

    def get_post(self, post_id: str) -> SocialPost | None:
        row = self.conn.execute(
            "SELECT * FROM posts WHERE id = ? AND workspace_id = ?",
            (post_id, self.workspace_id),
        ).fetchone()
        return self._row_to_post(row) if row else None

    def get_post_by_url(self, url: str) -> SocialPost | None:
        row = self.conn.execute(
            "SELECT * FROM posts WHERE url = ? AND workspace_id = ? LIMIT 1",
            (url, self.workspace_id),
        ).fetchone()
        return self._row_to_post(row) if row else None

    def get_post_by_x_post_id(self, x_post_id: str) -> SocialPost | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM posts
            WHERE platform = 'x'
              AND workspace_id = ?
              AND (
                url LIKE ?
                OR url LIKE ?
                OR url LIKE ?
                OR url LIKE ?
              )
            LIMIT 1
            """,
            (
                self.workspace_id,
                f"%/status/{x_post_id}",
                f"%/status/{x_post_id}?%",
                f"%/status/{x_post_id}/%",
                f"%/status/{x_post_id}#%",
            ),
        ).fetchone()
        return self._row_to_post(row) if row else None

    def list_posts(
        self,
        *,
        platform: str | None = None,
        status: str | None = None,
        topic: str | None = None,
        post_type: str | None = None,
        limit: int | None = 50,
    ) -> list[SocialPost]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if post_type:
            clauses.append("post_type = ?")
            params.append(post_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM posts {where} ORDER BY published_at DESC, created_at DESC{limit_sql}",
            params,
        ).fetchall()
        return [self._row_to_post(row) for row in rows]

    def latest_metrics_by_post_id(self, post_ids: Iterable[str] | None = None) -> dict[str, EngagementMetrics]:
        params: list[object] = [self.workspace_id]
        filter_sql = ""
        ids = list(post_ids or [])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            filter_sql = f"AND pm.post_id IN ({placeholders})"
            params.extend(ids)
        rows = self.conn.execute(
            f"""
            SELECT pm.*
            FROM post_metrics pm
            JOIN posts p ON p.id = pm.post_id
            WHERE pm.id = (
                SELECT latest.id
                FROM post_metrics AS latest
                WHERE latest.post_id = pm.post_id
                ORDER BY latest.measured_at DESC, latest.created_at DESC, latest.id DESC
                LIMIT 1
            )
            AND p.workspace_id = ?
            {filter_sql}
            """,
            params,
        ).fetchall()
        return {row["post_id"]: self._row_to_metrics(row) for row in rows}

    def metrics_for_post(self, post_id: str, *, limit: int | None = None) -> list[EngagementMetrics]:
        params: list[object] = [post_id, self.workspace_id]
        limit_sql = ""
        if limit:
            limit_sql = "LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM post_metrics
            WHERE post_id = ?
              AND EXISTS (
                  SELECT 1 FROM posts
                  WHERE posts.id = post_metrics.post_id
                    AND posts.workspace_id = ?
              )
            ORDER BY measured_at DESC, created_at DESC, id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [self._row_to_metrics(row) for row in rows]

    def notes_by_post_id(self, post_ids: Iterable[str] | None = None) -> dict[str, PostNote]:
        params: list[object] = [self.workspace_id]
        where = ""
        ids = list(post_ids or [])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            where = f"AND n.post_id IN ({placeholders})"
            params.extend(ids)
        rows = self.conn.execute(
            f"""
            SELECT n.*
            FROM post_notes n
            JOIN posts p ON p.id = n.post_id
            WHERE p.workspace_id = ?
            {where}
            """,
            params,
        ).fetchall()
        return {row["post_id"]: self._row_to_note(row) for row in rows}

    def list_context_sections(
        self,
        *,
        kinds: Iterable[str] | None = None,
        active_only: bool = True,
    ) -> list[ContextSection]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if kinds:
            kinds_list = list(kinds)
            placeholders = ", ".join("?" for _ in kinds_list)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds_list)
        if active_only:
            clauses.append("active = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM context_sections {where} ORDER BY weight DESC, updated_at DESC",
            params,
        ).fetchall()
        return [self._row_to_context(row) for row in rows]

    def list_patterns(
        self,
        *,
        categories: Iterable[str] | None = None,
        active_only: bool = True,
        limit: int | None = None,
    ) -> list[Pattern]:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[object] = [self.workspace_id]
        if categories:
            category_list = list(categories)
            placeholders = ", ".join("?" for _ in category_list)
            clauses.append(f"category IN ({placeholders})")
            params.extend(category_list)
        if active_only:
            clauses.append("active = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = "LIMIT ?" if limit else ""
        if limit:
            params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM patterns {where} ORDER BY updated_at DESC {limit_sql}",
            params,
        ).fetchall()
        return [self._row_to_pattern(row) for row in rows]

    def top_examples(
        self,
        *,
        platform: str,
        topic: str | None = None,
        post_type: str | None = None,
        limit: int = 5,
        imitate_only: bool = True,
        authored_only: bool = False,
    ) -> list[SocialPost]:
        clauses = ["p.workspace_id = ?", "p.platform = ?", "p.status IN ('example', 'published')"]
        params: list[object] = [self.workspace_id, platform]
        if topic:
            clauses.append("(p.topic = ? OR p.topic LIKE ?)")
            params.extend([topic, f"%{topic}%"])
        if post_type:
            clauses.append("p.post_type = ?")
            params.append(post_type)
        if imitate_only:
            clauses.append("COALESCE(n.imitate, 0) = 1")
        if authored_only:
            clauses.append("p.source_draft_id IS NULL")
        where = " AND ".join(clauses)
        rows = self.conn.execute(
            f"""
            SELECT p.*
            FROM posts p
            LEFT JOIN post_notes n ON n.post_id = p.id
            LEFT JOIN (
                SELECT post_id, engagement_rate,
                       COALESCE(likes, 0) + COALESCE(replies, 0) + COALESCE(reposts, 0)
                       + COALESCE(quotes, 0) + COALESCE(bookmarks, 0) AS total_engagements,
                       ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY measured_at DESC) AS rn
                FROM post_metrics
            ) m ON m.post_id = p.id AND m.rn = 1
            WHERE {where}
            ORDER BY COALESCE(m.engagement_rate, 0) DESC,
                     COALESCE(m.total_engagements, 0) DESC,
                     p.created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [self._row_to_post(row) for row in rows]

    def list_authored_voice_candidates(self, *, platform: str) -> list[VoiceExampleCandidate]:
        """Return the authored corpus used only by automatic Voice fallback.

        Explicit Voice Examples continue to use ``top_examples``. Keeping this as
        a separate read model prevents confidence-aware fallback ranking from
        changing analytics, Topic Scout, or any legacy example ordering.
        """

        rows = self.conn.execute(
            """
            SELECT p.*
            FROM posts p
            WHERE p.workspace_id = ?
              AND p.platform = ?
              AND p.status IN ('example', 'published')
              AND p.source_draft_id IS NULL
            ORDER BY COALESCE(p.published_at, p.created_at) DESC,
                     p.created_at DESC,
                     p.id DESC
            """,
            (self.workspace_id, platform),
        ).fetchall()
        posts = [self._row_to_post(row) for row in rows]
        post_ids = [post.id for post in posts]
        notes = self.notes_by_post_id(post_ids) if post_ids else {}
        metrics = self.latest_metrics_by_post_id(post_ids) if post_ids else {}
        return [
            VoiceExampleCandidate(
                post=post,
                note=notes.get(post.id),
                metrics=metrics.get(post.id),
            )
            for post in posts
        ]

    def seed_defaults(self) -> None:
        for section in default_context_sections():
            self.save_context_section(section)
        for pattern in default_patterns():
            self.save_pattern(pattern)

    def _row_to_post(self, row: sqlite3.Row) -> SocialPost:
        return SocialPost(
            id=row["id"],
            workspace_id=row["workspace_id"],
            platform=row["platform"],
            text=row["text"],
            url=row["url"],
            published_at=row["published_at"],
            author=row["author"],
            topic=row["topic"],
            post_type=row["post_type"],
            status=row["status"],
            source_draft_id=row["source_draft_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_metrics(self, row: sqlite3.Row) -> EngagementMetrics:
        return EngagementMetrics(
            impressions=row["impressions"],
            likes=row["likes"],
            replies=row["replies"],
            reposts=row["reposts"],
            quotes=row["quotes"],
            bookmarks=row["bookmarks"],
            engagement_rate=row["engagement_rate"],
            measured_at=row["measured_at"],
        )

    def _row_to_note(self, row: sqlite3.Row) -> PostNote:
        return PostNote(
            post_id=row["post_id"],
            worked_reason=row["worked_reason"],
            pattern_name=row["pattern_name"],
            hook=row["hook"],
            structure=row["structure"],
            tone=row["tone"],
            cta=row["cta"],
            formatting=row["formatting"],
            imitate=bool(row["imitate"]),
            updated_at=row["updated_at"],
        )

    def _row_to_influence(self, row: sqlite3.Row) -> PostInfluence:
        return PostInfluence(
            id=row["id"],
            post_id=row["post_id"],
            source_draft_id=row["source_draft_id"],
            influence_type=row["influence_type"],
            category=row["category"] or None,
            name=row["name"],
            ref_id=row["ref_id"] or None,
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
        )

    def _row_to_pattern(self, row: sqlite3.Row) -> Pattern:
        return Pattern(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            category=row["category"],
            description=row["description"],
            example_post_ids=json.loads(row["example_post_ids"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_viral_pattern(self, row: sqlite3.Row) -> ViralPattern:
        return ViralPattern(
            id=row["id"],
            workspace_id=row["workspace_id"],
            platform=row["platform"],
            source_url=row["source_url"],
            source_author=row["source_author"],
            source_text=row["source_text"],
            source_summary=row["source_summary"],
            topic=row["topic"],
            category=row["category"],
            pattern_name=row["pattern_name"],
            hook_style=row["hook_style"],
            structure=row["structure"],
            pacing=row["pacing"],
            tone=row["tone"],
            formatting=row["formatting"],
            why_it_worked=row["why_it_worked"],
            adaptation_rule=row["adaptation_rule"],
            engagement_notes=row["engagement_notes"],
            likes=row["likes"],
            replies=row["replies"],
            reposts=row["reposts"],
            quotes=row["quotes"],
            bookmarks=row["bookmarks"],
            impressions=row["impressions"],
            collected_at=row["collected_at"],
            window=row["window"],
            status=row["status"],
            raw_text=row["raw_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_context(self, row: sqlite3.Row) -> ContextSection:
        return ContextSection(
            id=row["id"],
            workspace_id=row["workspace_id"],
            kind=row["kind"],
            name=row["name"],
            content=row["content"],
            weight=row["weight"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_trend(self, row: sqlite3.Row) -> TrendItem:
        return TrendItem(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            source_url=row["source_url"],
            source_type=row["source_type"],
            topic=row["topic"],
            summary=row["summary"],
            why_it_matters=row["why_it_matters"],
            leadpoet_angle=row["leadpoet_angle"],
            suggested_hooks=row["suggested_hooks"],
            urgency=row["urgency"],
            date=row["date"],
            status=row["status"],
            engagement_notes=row["engagement_notes"],
            source_author=row["source_author"],
            raw_text=row["raw_text"],
            source_title=row["source_title"],
            source_published_at=row["source_published_at"],
            source_excerpt=row["source_excerpt"],
            source_text_excerpt=row["source_text_excerpt"],
            key_points_json=row["key_points_json"],
            important_claims_json=row["important_claims_json"],
            post_angles_json=row["post_angles_json"],
            source_confidence=row["source_confidence"],
            enriched_at=row["enriched_at"],
            enrichment_status=row["enrichment_status"] or "not_attempted",
            enrichment_error=row["enrichment_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_lead_magnet_source(self, row: sqlite3.Row) -> LeadMagnetSource:
        return LeadMagnetSource(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            source_url=row["source_url"],
            canonical_url=row["canonical_url"],
            platform=row["platform"],
            author=row["author"],
            published_at=row["published_at"],
            collected_at=row["collected_at"],
            format=row["format"],
            sales_topic=row["sales_topic"],
            hook=row["hook"],
            promise=row["promise"],
            audience=row["audience"],
            delivery_mechanism=row["delivery_mechanism"],
            cta=row["cta"],
            leadpoet_integration=row["leadpoet_integration"],
            why_it_worked=row["why_it_worked"],
            likes=row["likes"],
            replies=row["replies"],
            reposts=row["reposts"],
            bookmarks=row["bookmarks"],
            impressions=row["impressions"],
            follower_count=row["follower_count"],
            metric_observed_at=row["metric_observed_at"],
            performance_score=row["performance_score"],
            evidence_strength=row["evidence_strength"] or "unknown",
            raw_text=row["raw_text"],
            status=row["status"] or "candidate",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_lead_magnet_research_run(self, row: sqlite3.Row) -> LeadMagnetResearchRun:
        return LeadMagnetResearchRun(
            id=row["id"],
            workspace_id=row["workspace_id"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            period_key=row["period_key"],
            query=row["query"],
            provider=row["provider"],
            status=row["status"] or "running",
            candidates_found=int(row["candidates_found"] or 0),
            sources_qualified=int(row["sources_qualified"] or 0),
            source_ids=json.loads(row["source_ids_json"] or "[]"),
            warnings=json.loads(row["warnings_json"] or "[]"),
            error_message=row["error_message"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_lead_magnet(self, row: sqlite3.Row) -> LeadMagnet:
        return LeadMagnet(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            promise=row["promise"],
            audience=row["audience"],
            sales_outcome=row["sales_outcome"],
            format=row["format"],
            depth=row["depth"],
            product_integration=row["product_integration"],
            cta=row["cta"],
            status=row["status"] or "draft",
            source_ids=json.loads(row["source_ids_json"] or "[]"),
            research_run_id=row["research_run_id"],
            evidence_summary=row["evidence_summary"],
            content=json.loads(row["content_json"] or "{}"),
            markdown=row["markdown"] or "",
            generation_brief=json.loads(row["generation_brief_json"] or "{}"),
            notion_page_id=row["notion_page_id"],
            notion_url=row["notion_url"],
            published_at=row["published_at"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_lead_magnet_version(self, row: sqlite3.Row) -> LeadMagnetVersion:
        return LeadMagnetVersion(
            id=row["id"],
            workspace_id=row["workspace_id"],
            lead_magnet_id=row["lead_magnet_id"],
            content=json.loads(row["content_json"] or "{}"),
            markdown=row["markdown"] or "",
            edit_type=row["edit_type"] or "generated",
            instruction=row["instruction"],
            source_ids=json.loads(row["source_ids_json"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_topic_idea(self, row: sqlite3.Row) -> TopicIdea:
        return TopicIdea(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            topic=row["topic"],
            angle=row["angle"],
            why_now=row["why_now"],
            source_type=row["source_type"],
            source_refs=json.loads(row["source_refs_json"] or "[]"),
            evidence_basis=json.loads(row["evidence_basis_json"] or "[]"),
            reader_value_mode=row["reader_value_mode"],
            post_type=row["post_type"],
            priority=int(row["priority"] or 0),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_queue_item(self, row: sqlite3.Row) -> ContentQueueItem:
        return ContentQueueItem(
            id=row["id"],
            workspace_id=row["workspace_id"],
            platform=row["platform"],
            draft_id=row["draft_id"],
            topic_idea_id=row["topic_idea_id"],
            text=row["text"],
            topic=row["topic"],
            post_type=row["post_type"],
            angle=row["angle"],
            trend_ids=json.loads(row["trend_ids_json"] or "[]"),
            scheduled_at=row["scheduled_at"],
            status=row["status"],
            reviewer_notes=row["reviewer_notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_at=row["approved_at"],
            posted_at=row["posted_at"],
            x_post_id=row["x_post_id"],
            x_url=row["x_url"],
            failure_reason=row["failure_reason"],
        )

    def _row_to_queue_version(self, row: sqlite3.Row) -> QueuedPostVersion:
        return QueuedPostVersion(
            id=row["id"],
            queue_id=row["queue_id"],
            text=row["text"],
            edit_type=row["edit_type"],
            edit_instruction=row["edit_instruction"],
            source_draft_id=row["source_draft_id"],
            accepted_at=row["accepted_at"],
            created_at=row["created_at"],
        )

    def _row_to_web_auth_user(self, row: sqlite3.Row) -> WebAuthUser:
        return WebAuthUser(
            username=row["username"],
            workspace_id=row["workspace_id"],
            password_hash=row["password_hash"],
            salt=row["salt"],
            iterations=row["iterations"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_sync_metadata(self, row: sqlite3.Row) -> SyncMetadata:
        return SyncMetadata(
            key=row["key"],
            workspace_id=row["workspace_id"],
            last_started_at=row["last_started_at"],
            last_completed_at=row["last_completed_at"],
            status=row["status"],
            summary=json.loads(row["summary_json"] or "{}"),
            error_message=row["error_message"],
        )

    def _sync_note_influences(self, note: PostNote) -> None:
        self.conn.execute(
            """
            DELETE FROM post_influences
            WHERE post_id = ? AND source_draft_id IS NULL AND influence_type = 'pattern'
            """,
            (note.post_id,),
        )
        influences: list[PostInfluence] = []
        if _clean(note.pattern_name):
            pattern_name = _clean(note.pattern_name)
            influences.append(
                PostInfluence(
                    post_id=note.post_id,
                    influence_type="pattern",
                    category=self._pattern_category(pattern_name) or "pattern",
                    name=pattern_name,
                    metadata={"source": "post_notes.pattern_name"},
                )
            )
        for category in ["hook", "structure", "tone", "cta", "formatting"]:
            value = _clean(getattr(note, category))
            if value:
                influences.append(
                    PostInfluence(
                        post_id=note.post_id,
                        influence_type="pattern",
                        category=category,
                        name=value,
                        metadata={"source": f"post_notes.{category}"},
                    )
                )
        if influences:
            self.conn.executemany(
                """
                INSERT OR IGNORE INTO post_influences (
                    id, post_id, source_draft_id, influence_type, category, name,
                    ref_id, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        influence.id,
                        influence.post_id,
                        influence.source_draft_id,
                        influence.influence_type,
                        influence.category or "",
                        influence.name,
                        influence.ref_id or "",
                        json.dumps(influence.metadata),
                        influence.created_at,
                    )
                    for influence in influences
                ],
            )

    def _pattern_category(self, pattern_name: str) -> str | None:
        row = self.conn.execute(
            "SELECT category FROM patterns WHERE name = ? AND workspace_id = ?",
            (pattern_name, self.workspace_id),
        ).fetchone()
        return row["category"] if row else None


def default_context_sections() -> list[ContextSection]:
    return [
        ContextSection(
            kind="writer",
            name="writer_profile",
            content="",
            weight=2.0,
            active=False,
        ),
        ContextSection(
            kind="product",
            name="company_product",
            content="",
            weight=1.8,
            active=False,
        ),
        ContextSection(
            kind="audience",
            name="target_audience",
            content="",
            weight=1.6,
            active=False,
        ),
        ContextSection(
            kind="positioning",
            name="core_positioning",
            content="",
            weight=1.7,
            active=False,
        ),
        ContextSection(
            kind="proof",
            name="proof_points",
            content="",
            weight=1.4,
            active=False,
        ),
        ContextSection(
            kind="priority",
            name="current_priorities",
            content="",
            weight=1.3,
            active=False,
        ),
        ContextSection(
            kind="guardrail",
            name="fact_guardrails",
            content="",
            weight=1.2,
            active=False,
        ),
    ]


def required_context_sections() -> list[ContextSection]:
    return []


def draft_influences_for_post(post_id: str, draft: GeneratedDraft) -> list[PostInfluence]:
    snapshot = draft.context_snapshot or {}
    request = snapshot.get("request") or draft.request.__dict__
    influences: list[PostInfluence] = []
    angle = _clean(request.get("angle"))
    if angle:
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="request",
                category="angle",
                name=angle,
            )
        )

    for pattern in snapshot.get("selected_patterns", []):
        name = _clean(pattern.get("name"))
        if not name:
            continue
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="pattern",
                category=_clean(pattern.get("category")) or "pattern",
                name=name,
                ref_id=_clean(pattern.get("id")) or None,
                metadata={
                    "source": "draft.selected_patterns",
                    "source_field": pattern.get("source_field"),
                    "description": pattern.get("description"),
                    "source_example_id": pattern.get("source_example_id"),
                },
            )
        )

    for pattern in snapshot.get("selected_viral_patterns", []):
        name = _clean(pattern.get("pattern_name") or pattern.get("name"))
        if not name:
            continue
        metadata = {
            "source": "draft.selected_viral_patterns",
            "source_field": "pattern",
            "source_url": pattern.get("source_url"),
            "hook_style": pattern.get("hook_style"),
            "structure": pattern.get("structure"),
            "why_it_worked": pattern.get("why_it_worked"),
            "adaptation_rule": pattern.get("adaptation_rule"),
        }
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="pattern",
                category=_clean(pattern.get("category")) or "viral_pattern",
                name=name,
                ref_id=_clean(pattern.get("id")) or None,
                metadata=metadata,
            )
        )
        for category, value in [
            ("hook", pattern.get("hook_style")),
            ("structure", pattern.get("structure")),
        ]:
            label = _clean(value)
            if not label:
                continue
            influences.append(
                PostInfluence(
                    post_id=post_id,
                    source_draft_id=draft.id,
                    influence_type="pattern",
                    category=category,
                    name=label,
                    ref_id=_clean(pattern.get("id")) or None,
                    metadata={**metadata, "source_field": category},
                )
            )

    for section in snapshot.get("selected_context_sections", []):
        name = _clean(section.get("name"))
        if not name:
            continue
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="context",
                category=_clean(section.get("kind")) or "context",
                name=name,
                ref_id=_clean(section.get("id")) or None,
                metadata={"weight": section.get("weight")},
            )
        )

    for example in snapshot.get("selected_examples", []):
        ref_id = _clean(example.get("id"))
        if not ref_id:
            continue
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="example",
                category=_clean(example.get("post_type")) or "example",
                name=ref_id,
                ref_id=ref_id,
                metadata={"topic": example.get("topic"), "text_preview": example.get("text_preview")},
            )
        )

    for hint in snapshot.get("analytics_hints", []):
        hint_text = _clean(hint)
        if hint_text:
            influences.append(
                PostInfluence(
                    post_id=post_id,
                    source_draft_id=draft.id,
                    influence_type="analytics",
                    category="hint",
                    name=hint_text,
                )
            )

    for trend in snapshot.get("selected_trends", []):
        trend_id = _clean(trend.get("id"))
        title = _clean(trend.get("title"))
        if not trend_id or not title:
            continue
        influences.append(
            PostInfluence(
                post_id=post_id,
                source_draft_id=draft.id,
                influence_type="trend",
                category=_clean(trend.get("source_type")) or "other",
                name=title,
                ref_id=trend_id,
                metadata={
                    "topic": trend.get("topic"),
                    "urgency": trend.get("urgency"),
                    "leadpoet_angle": trend.get("leadpoet_angle"),
                },
            )
        )

    if influences:
        return influences

    for label in draft.influences:
        influence_type, _, name = label.partition(":")
        clean_name = _clean(name)
        if clean_name:
            influences.append(
                PostInfluence(
                    post_id=post_id,
                    source_draft_id=draft.id,
                    influence_type=influence_type,
                    category=influence_type,
                    name=clean_name,
                )
            )
    return influences


def _clean(value: object) -> str:
    return str(value or "").strip()


def hash_password(
    password: str,
    *,
    salt: str | None = None,
    iterations: int = PASSWORD_HASH_ITERATIONS,
) -> tuple[str, str, int]:
    salt_bytes = base64.b64decode(salt.encode("ascii")) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        iterations,
    )
    encoded_salt = base64.b64encode(salt_bytes).decode("ascii")
    encoded_hash = base64.b64encode(digest).decode("ascii")
    return encoded_salt, encoded_hash, iterations


def normalize_workspace_id(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_WORKSPACE_ID
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-._")
    if not text:
        return DEFAULT_WORKSPACE_ID
    return text[:WORKSPACE_ID_MAX_LENGTH]


def workspace_id_from_username(username: str) -> str:
    if username.strip() == DEFAULT_WEB_AUTH_USERNAME:
        return DEFAULT_WORKSPACE_ID
    return normalize_workspace_id(f"user-{username.strip().lower()}")


def _keywords(value: str) -> list[str]:
    stopwords = {"the", "and", "for", "with", "that", "this", "why", "how", "into", "from"}
    return [
        word
        for word in "".join(char.lower() if char.isalnum() else " " for char in value).split()
        if len(word) > 2 and word not in stopwords
    ]


def default_patterns() -> list[Pattern]:
    return [
        Pattern(
            name="enemy_then_better_model",
            category="structure",
            description=(
                "Name the broken default, then give the sharper model the writer believes in."
            ),
        ),
        Pattern(
            name="specific_observation",
            category="hook",
            description=(
                "Open with a concrete thing the founder keeps seeing, then explain what it means."
            ),
        ),
        Pattern(
            name="plain_english_technical",
            category="tone",
            description=(
                "Explain technical ideas without sounding academic. Use simple claims and practical stakes."
            ),
        ),
        Pattern(
            name="soft_inbound_cta",
            category="cta",
            description=(
                "End with a low-friction invitation when useful. Keep it direct, not salesy."
            ),
        ),
    ]
