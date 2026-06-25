from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from leadpoet_social_brain.core.models import (
    ContextSection,
    DraftRequest,
    EngagementMetrics,
    GeneratedDraft,
    Pattern,
    PostInfluence,
    PostNote,
    SocialPost,
    TrendItem,
    new_id,
    utc_now,
)


DEFAULT_DB_PATH = Path("data/social_brain.sqlite3")


class SQLiteStore:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
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
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                example_post_ids TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_sections (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, name)
            );

            CREATE TABLE IF NOT EXISTS generated_drafts (
                id TEXT PRIMARY KEY,
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trend_items_lookup
            ON trend_items(status, urgency, topic, date);
            """
        )
        self._migrate_existing_schema()
        self.conn.commit()

    def _migrate_existing_schema(self) -> None:
        self._ensure_column("posts", "source_draft_id", "TEXT")
        for column in ["hook", "structure", "tone", "cta", "formatting"]:
            self._ensure_column("post_notes", column, "TEXT")
        self._ensure_column("generated_drafts", "context_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def save_post(self, post: SocialPost) -> SocialPost:
        now = utc_now()
        saved = SocialPost(
            id=post.id,
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
                id, platform, text, url, published_at, author, topic, post_type,
                status, source_draft_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
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
                id, name, category, description, example_post_ids, active,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                category = excluded.category,
                description = excluded.description,
                example_post_ids = excluded.example_post_ids,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
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
                id, kind, name, content, weight, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, name) DO UPDATE SET
                content = excluded.content,
                weight = excluded.weight,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
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
        self.conn.execute(
            """
            INSERT INTO generated_drafts (
                id, platform, topic, post_type, request_json, text, explanation,
                influences_json, context_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.id,
                draft.request.platform,
                draft.request.topic,
                draft.request.post_type,
                json.dumps(draft.request.__dict__),
                draft.text,
                draft.explanation,
                json.dumps(draft.influences),
                json.dumps(draft.context_snapshot),
                draft.created_at,
            ),
        )
        self.conn.commit()
        return draft

    def save_trend_item(self, trend: TrendItem) -> TrendItem:
        now = utc_now()
        saved = TrendItem(
            id=trend.id,
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
            created_at=trend.created_at,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO trend_items (
                id, title, source_url, source_type, topic, summary, why_it_matters,
                leadpoet_angle, suggested_hooks, urgency, date, status,
                engagement_notes, source_author, raw_text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
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
                updated_at = excluded.updated_at
            """,
            (
                saved.id,
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
                saved.created_at,
                saved.updated_at,
            ),
        )
        self.conn.commit()
        return saved

    def list_trends(
        self,
        *,
        topic: str | None = None,
        status: str | None = None,
        urgency: str | None = None,
        limit: int = 20,
    ) -> list[TrendItem]:
        clauses: list[str] = []
        params: list[object] = []
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
            "UPDATE trend_items SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), trend_id),
        )
        self.conn.commit()

    def get_generated_draft(self, draft_id: str) -> GeneratedDraft | None:
        row = self.conn.execute(
            "SELECT * FROM generated_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
        if not row:
            return None
        request = DraftRequest(**json.loads(row["request_json"]))
        return GeneratedDraft(
            id=row["id"],
            text=row["text"],
            request=request,
            explanation=row["explanation"],
            influences=json.loads(row["influences_json"]),
            context_snapshot=json.loads(row["context_snapshot_json"] or "{}"),
            created_at=row["created_at"],
        )

    def save_post_influences(self, influences: Iterable[PostInfluence]) -> None:
        rows = list(influences)
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
            f"SELECT * FROM post_influences {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [self._row_to_influence(row) for row in rows]

    def get_post(self, post_id: str) -> SocialPost | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return self._row_to_post(row) if row else None

    def list_posts(
        self,
        *,
        platform: str | None = None,
        status: str | None = None,
        topic: str | None = None,
        post_type: str | None = None,
        limit: int = 50,
    ) -> list[SocialPost]:
        clauses: list[str] = []
        params: list[object] = []
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
        rows = self.conn.execute(
            f"SELECT * FROM posts {where} ORDER BY published_at DESC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._row_to_post(row) for row in rows]

    def latest_metrics_by_post_id(self, post_ids: Iterable[str] | None = None) -> dict[str, EngagementMetrics]:
        params: list[object] = []
        filter_sql = ""
        ids = list(post_ids or [])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            filter_sql = f"WHERE post_id IN ({placeholders})"
            params.extend(ids)
        rows = self.conn.execute(
            f"""
            SELECT pm.*
            FROM post_metrics pm
            JOIN (
                SELECT post_id, MAX(measured_at) AS measured_at
                FROM post_metrics
                {filter_sql}
                GROUP BY post_id
            ) latest
            ON latest.post_id = pm.post_id AND latest.measured_at = pm.measured_at
            """,
            params,
        ).fetchall()
        return {row["post_id"]: self._row_to_metrics(row) for row in rows}

    def notes_by_post_id(self, post_ids: Iterable[str] | None = None) -> dict[str, PostNote]:
        params: list[object] = []
        where = ""
        ids = list(post_ids or [])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            where = f"WHERE post_id IN ({placeholders})"
            params.extend(ids)
        rows = self.conn.execute(f"SELECT * FROM post_notes {where}", params).fetchall()
        return {row["post_id"]: self._row_to_note(row) for row in rows}

    def list_context_sections(
        self,
        *,
        kinds: Iterable[str] | None = None,
        active_only: bool = True,
    ) -> list[ContextSection]:
        clauses: list[str] = []
        params: list[object] = []
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
        clauses: list[str] = []
        params: list[object] = []
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
    ) -> list[SocialPost]:
        clauses = ["p.platform = ?", "p.status IN ('example', 'published')"]
        params: list[object] = [platform]
        if topic:
            clauses.append("(p.topic = ? OR p.topic LIKE ?)")
            params.extend([topic, f"%{topic}%"])
        if post_type:
            clauses.append("p.post_type = ?")
            params.append(post_type)
        if imitate_only:
            clauses.append("COALESCE(n.imitate, 0) = 1")
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

    def seed_defaults(self) -> None:
        for section in default_context_sections():
            self.save_context_section(section)
        for pattern in default_patterns():
            self.save_pattern(pattern)

    def _row_to_post(self, row: sqlite3.Row) -> SocialPost:
        return SocialPost(
            id=row["id"],
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
            name=row["name"],
            category=row["category"],
            description=row["description"],
            example_post_ids=json.loads(row["example_post_ids"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_context(self, row: sqlite3.Row) -> ContextSection:
        return ContextSection(
            id=row["id"],
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
            "SELECT category FROM patterns WHERE name = ?",
            (pattern_name,),
        ).fetchone()
        return row["category"] if row else None


def default_context_sections() -> list[ContextSection]:
    return [
        ContextSection(
            kind="style_guide",
            name="gavin_founder_voice_v0",
            content=(
                "Write like Gavin: first person when useful, direct, specific, and a little opinionated. "
                "Prefer plain words over polished corporate phrasing. Make one clear point. "
                "Use concrete nouns, short paragraphs, and a human rhythm. Avoid hype, generic AI language, "
                "cliches, and em dashes."
            ),
            weight=2.0,
        ),
        ContextSection(
            kind="product",
            name="leadpoet_product_v0",
            content=(
                "Leadpoet helps turn founder insight, company context, and buyer signals into better sales "
                "language and inbound interest. Treat this as editable working context until the product "
                "positioning is made more precise."
            ),
            weight=1.5,
        ),
        ContextSection(
            kind="audience",
            name="primary_audiences_v0",
            content=(
                "Primary readers: founders, technical operators, B2B sales leaders, investors, and people "
                "watching the Bittensor/subnet ecosystem."
            ),
        ),
        ContextSection(
            kind="positioning",
            name="leadpoet_positioning_v0",
            content=(
                "Leadpoet should sound practical and ambitious: sales AI should know the buyer, the market, "
                "the timing, and the company's real point of view. The enemy is generic automation dressed "
                "up as intelligence."
            ),
            weight=1.4,
        ),
        ContextSection(
            kind="bittensor",
            name="bittensor_subnet_context_v0",
            content=(
                "When writing about Bittensor or subnets, explain the useful economic or technical point in "
                "plain English. Avoid insider jargon unless the post is explicitly for the ecosystem."
            ),
        ),
        ContextSection(
            kind="sales_llm_vision",
            name="sales_llm_vision_v0",
            content=(
                "The sales LLM vision: move from generic outreach to systems that understand buyer context, "
                "specific pain, timing, trust, and why a conversation should happen now."
            ),
            weight=1.3,
        ),
        ContextSection(
            kind="priority",
            name="company_priorities_v0",
            content=(
                "Current priorities: make Leadpoet concrete, show product progress, explain why context "
                "matters, and attract serious inbound from customers, partners, and investors."
            ),
        ),
    ]


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
                "Name the broken default, then give the sharper model Leadpoet believes in."
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
