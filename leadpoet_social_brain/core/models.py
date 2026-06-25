from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class EngagementMetrics:
    impressions: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    bookmarks: int | None = None
    engagement_rate: float | None = None
    measured_at: str = field(default_factory=utc_now)

    def total_engagements(self) -> int:
        return sum(
            value or 0
            for value in [
                self.likes,
                self.replies,
                self.reposts,
                self.quotes,
                self.bookmarks,
            ]
        )

    def normalized(self) -> "EngagementMetrics":
        if self.engagement_rate is not None:
            return self
        if not self.impressions:
            return self
        return EngagementMetrics(
            impressions=self.impressions,
            likes=self.likes,
            replies=self.replies,
            reposts=self.reposts,
            quotes=self.quotes,
            bookmarks=self.bookmarks,
            engagement_rate=self.total_engagements() / self.impressions,
            measured_at=self.measured_at,
        )


@dataclass(frozen=True)
class SocialPost:
    text: str
    platform: str
    author: str
    topic: str
    post_type: str
    url: str | None = None
    published_at: str | None = None
    status: str = "example"
    source_draft_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class PostNote:
    post_id: str
    worked_reason: str | None = None
    pattern_name: str | None = None
    hook: str | None = None
    structure: str | None = None
    tone: str | None = None
    cta: str | None = None
    formatting: str | None = None
    imitate: bool = False
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class PostInfluence:
    post_id: str
    influence_type: str
    name: str
    category: str | None = None
    ref_id: str | None = None
    source_draft_id: str | None = None
    metadata: "JsonDict" = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Pattern:
    name: str
    category: str
    description: str
    example_post_ids: list[str] = field(default_factory=list)
    active: bool = True
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ContextSection:
    kind: str
    name: str
    content: str
    weight: float = 1.0
    active: bool = True
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class TrendItem:
    title: str
    source_type: str
    topic: str
    summary: str
    why_it_matters: str
    leadpoet_angle: str
    suggested_hooks: str | None = None
    source_url: str | None = None
    urgency: str = "medium"
    date: str | None = None
    status: str = "unused"
    engagement_notes: str | None = None
    source_author: str | None = None
    raw_text: str | None = None
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class DraftRequest:
    platform: str
    topic: str
    post_type: str
    angle: str | None = None
    audience: str | None = None
    tone: str | None = None
    call_to_action: str | None = None
    max_chars: int | None = None
    extra_context: str | None = None
    use_trends: bool = False


@dataclass(frozen=True)
class GeneratedDraft:
    text: str
    request: DraftRequest
    explanation: str
    influences: list[str]
    context_snapshot: "JsonDict" = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ContextBundle:
    platform: str
    topic: str
    post_type: str
    style_sections: list[ContextSection]
    positioning_sections: list[ContextSection]
    patterns: list[Pattern]
    examples: list[SocialPost]
    notes_by_post_id: dict[str, PostNote]
    metrics_by_post_id: dict[str, EngagementMetrics]
    analytics_hints: list[str]

    def influence_labels(self) -> list[str]:
        labels: list[str] = []
        labels.extend(f"style:{section.name}" for section in self.style_sections)
        labels.extend(f"context:{section.name}" for section in self.positioning_sections)
        labels.extend(f"example:{post.id}" for post in self.examples)
        labels.extend(f"analytics:{hint}" for hint in self.analytics_hints)
        return labels


@dataclass(frozen=True)
class PerformanceRow:
    key: str
    count: int
    avg_engagement_rate: float | None
    avg_total_engagements: float
    avg_impressions: float
    avg_bookmarks: float
    avg_replies: float
    avg_reposts: float
    top_post_id: str | None


@dataclass(frozen=True)
class AnalyticsSummary:
    by_post_type: list[PerformanceRow]
    by_topic: list[PerformanceRow]
    by_pattern: list[PerformanceRow]
    by_hook: list[PerformanceRow]
    by_structure: list[PerformanceRow]
    by_tone: list[PerformanceRow]
    by_cta: list[PerformanceRow]
    by_formatting: list[PerformanceRow]
    by_trend_topic: list[PerformanceRow]
    by_trend_source_type: list[PerformanceRow]
    by_trend_urgency: list[PerformanceRow]
    by_used_trend: list[PerformanceRow]
    best_examples: list[SocialPost]
    hints: list[str]


JsonDict = dict[str, Any]
