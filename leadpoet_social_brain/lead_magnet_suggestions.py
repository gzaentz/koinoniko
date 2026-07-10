from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass

from leadpoet_social_brain.core.models import LeadMagnetSource, TopicIdea, TrendItem


SUPPORTED_FORMATS = ("prompt_library", "playbook", "template_pack", "scorecard")
FORMAT_LABELS = {
    "prompt_library": "prompt library",
    "playbook": "playbook",
    "template_pack": "template pack",
    "scorecard": "scorecard",
}


@dataclass(frozen=True)
class LeadMagnetSuggestion:
    id: str
    title: str
    format: str
    audience: str
    sales_outcome: str
    depth: str
    product_integration: str
    cta: str
    instruction: str
    source_ids: list[str]
    research_run_id: str | None
    evidence_summary: str
    trend_ids: list[str]
    trend_titles: list[str]
    reason: str
    score: float


@dataclass(frozen=True)
class _Theme:
    key: str
    title_stem: str
    subject: str
    keywords: tuple[str, ...]
    audience: str
    sales_outcome: str
    sections: tuple[str, ...]
    format_order: tuple[str, ...]


THEMES = (
    _Theme(
        key="buyer_signals",
        title_stem="Buyer Signal-to-Outbound",
        subject="turning buyer signals into timely outbound",
        keywords=("buyer signal", "buying signal", "buyer intent", "intent data", "account signal", "timing"),
        audience="B2B sales leaders, RevOps teams, founders, and outbound operators",
        sales_outcome=(
            "Turn buyer signals and account context into prioritized accounts, clear reasons to engage, "
            "and timely outbound actions."
        ),
        sections=("signal capture", "signal validation", "account prioritization", "outreach handoff"),
        format_order=("scorecard", "playbook", "prompt_library", "template_pack"),
    ),
    _Theme(
        key="account_research",
        title_stem="Signal-Aware Account Research",
        subject="building useful account research before outreach",
        keywords=("account research", "account context", "company research", "personalization", "research brief"),
        audience="SDRs, AEs, sales leaders, and founder-led sales teams",
        sales_outcome=(
            "Turn scattered account evidence into a concise, source-grounded research brief and a relevant outreach angle."
        ),
        sections=("account evidence", "business context", "reason to prioritize", "message angle", "claim review"),
        format_order=("prompt_library", "template_pack", "playbook", "scorecard"),
    ),
    _Theme(
        key="outbound_workflow",
        title_stem="Modern Outbound Workflow",
        subject="making outbound prospecting more repeatable",
        keywords=("outbound", "prospecting", "cold email", "outreach", "sdr", "sales engagement"),
        audience="Outbound sales teams, SDR leaders, RevOps operators, and founders",
        sales_outcome=(
            "Build a repeatable prospecting workflow that moves from account selection to researched, relevant outreach."
        ),
        sections=("account selection", "research", "message development", "quality review", "sales handoff"),
        format_order=("playbook", "prompt_library", "template_pack", "scorecard"),
    ),
    _Theme(
        key="sales_ai",
        title_stem="AI-Assisted Sales Workflow",
        subject="using AI without losing evidence or judgment",
        keywords=("sales ai", "ai sales", "ai sdr", "ai agent", "agentic", "automation", "prompt"),
        audience="Sales and RevOps teams adopting AI-assisted research and outbound workflows",
        sales_outcome=(
            "Use AI to accelerate sales research and workflow execution while keeping inputs, evidence, and human review explicit."
        ),
        sections=("input preparation", "research prompts", "evidence checks", "workflow automation", "human review"),
        format_order=("prompt_library", "playbook", "scorecard", "template_pack"),
    ),
    _Theme(
        key="revops_pipeline",
        title_stem="RevOps Pipeline Prioritization",
        subject="prioritizing pipeline with better account context",
        keywords=("revops", "revenue operations", "pipeline", "crm", "account prioritization", "scoring"),
        audience="RevOps leaders, sales operations teams, and pipeline owners",
        sales_outcome=(
            "Create a consistent way to evaluate account priority, explain the evidence, and route the next sales action."
        ),
        sections=("evidence inputs", "priority criteria", "score interpretation", "routing rules", "measurement"),
        format_order=("scorecard", "playbook", "template_pack", "prompt_library"),
    ),
    _Theme(
        key="lead_generation",
        title_stem="Intent-Led Lead Generation",
        subject="improving lead generation with intent and context",
        keywords=("lead generation", "lead gen", "demand generation", "demand gen", "prospect list", "target account"),
        audience="B2B growth, demand generation, sales, and RevOps teams",
        sales_outcome=(
            "Turn market and account signals into a focused target-account list and an actionable sales handoff."
        ),
        sections=("target criteria", "signal collection", "qualification", "prioritization", "sales handoff"),
        format_order=("playbook", "template_pack", "scorecard", "prompt_library"),
    ),
)


def build_lead_magnet_suggestions(
    sources: list[LeadMagnetSource],
    *,
    trends: list[TrendItem] | None = None,
    topic_ideas: list[TopicIdea] | None = None,
    research_run_id: str | None = None,
    limit: int = 3,
) -> list[LeadMagnetSuggestion]:
    if not sources:
        return []
    trends = list(trends or [])
    topic_ideas = list(topic_ideas or [])
    ranked_sources = sorted(sources, key=source_rank, reverse=True)
    global_formats = ranked_formats(ranked_sources)
    theme_rows = [score_theme(theme, ranked_sources, trends, topic_ideas) for theme in THEMES]
    theme_rows.sort(key=lambda row: (row[0], row[1].key), reverse=True)

    selected: list[LeadMagnetSuggestion] = []
    used_formats: set[str] = set()
    for theme_score, theme, matched_sources, matched_trends in theme_rows:
        if len(selected) >= max(1, limit):
            break
        if theme_score <= 0 and selected:
            continue
        source_pool = matched_sources or ranked_sources
        selected_sources = source_pool[:8]
        format = select_format(theme, global_formats, used_formats)
        used_formats.add(format)
        selected.append(
            suggestion_for_theme(
                theme,
                format=format,
                sources=selected_sources,
                trends=matched_trends[:3],
                research_run_id=research_run_id,
                score=theme_score,
            )
        )

    if selected and len(selected) < max(1, limit):
        top_score, top_theme, top_sources, top_trends = theme_rows[0]
        source_pool = (top_sources or ranked_sources)[:8]
        for format in [*global_formats, *top_theme.format_order, *SUPPORTED_FORMATS]:
            if len(selected) >= max(1, limit):
                break
            if format in used_formats:
                continue
            used_formats.add(format)
            selected.append(
                suggestion_for_theme(
                    top_theme,
                    format=format,
                    sources=source_pool,
                    trends=top_trends[:3],
                    research_run_id=research_run_id,
                    score=max(0.0, top_score - len(selected) * 0.25),
                )
            )

    if not selected:
        return []
    return selected[: max(1, limit)]


def score_theme(
    theme: _Theme,
    sources: list[LeadMagnetSource],
    trends: list[TrendItem],
    topic_ideas: list[TopicIdea],
) -> tuple[float, _Theme, list[LeadMagnetSource], list[TrendItem]]:
    score = 0.0
    source_matches: list[tuple[float, LeadMagnetSource]] = []
    for source in sources:
        hits = keyword_hits(source_text(source), theme.keywords)
        if not hits:
            continue
        weighted = source_rank(source) * (1.0 + min(hits, 3) * 0.35)
        score += weighted
        source_matches.append((weighted, source))

    trend_matches: list[tuple[float, TrendItem]] = []
    for trend in trends:
        hits = keyword_hits(trend_text(trend), theme.keywords)
        if not hits:
            continue
        urgency = {"high": 3.0, "medium": 2.0}.get((trend.urgency or "").lower(), 1.0)
        weighted = urgency * (1.0 + min(hits, 3) * 0.5)
        score += weighted
        trend_matches.append((weighted, trend))

    for idea in topic_ideas:
        hits = keyword_hits(topic_idea_text(idea), theme.keywords)
        if hits:
            score += (1.0 + max(0, int(idea.priority)) / 100.0) * min(hits, 3)

    source_matches.sort(key=lambda row: row[0], reverse=True)
    trend_matches.sort(key=lambda row: row[0], reverse=True)
    return (
        round(score, 3),
        theme,
        [source for _, source in source_matches],
        [trend for _, trend in trend_matches],
    )


def suggestion_for_theme(
    theme: _Theme,
    *,
    format: str,
    sources: list[LeadMagnetSource],
    trends: list[TrendItem],
    research_run_id: str | None,
    score: float,
) -> LeadMagnetSuggestion:
    title = suggestion_title(theme.title_stem, format)
    trend_titles = [trend.title for trend in trends if clean(trend.title)]
    metric_count = sum(source.evidence_strength == "metric_backed" for source in sources)
    trend_clause = f" and {len(trend_titles)} aligned current trend(s)" if trend_titles else ""
    reason = (
        f"Recommended from {len(sources)} retained source(s){trend_clause} around {theme.subject}."
    )
    evidence_summary = (
        f"Research-backed direction using {len(sources)} retained source(s), including "
        f"{metric_count} with observed engagement metrics"
        + (f", aligned with: {', '.join(trend_titles)}" if trend_titles else "")
        + "."
    )
    instruction = suggestion_instruction(theme, format=format, trend_titles=trend_titles)
    source_ids = [source.id for source in sources]
    trend_ids = [trend.id for trend in trends]
    digest = hashlib.sha256(
        f"{theme.key}:{format}:{':'.join(source_ids)}:{':'.join(trend_ids)}".encode("utf-8")
    ).hexdigest()[:16]
    return LeadMagnetSuggestion(
        id=f"leadmagnet_suggestion_{digest}",
        title=title,
        format=format,
        audience=theme.audience,
        sales_outcome=theme.sales_outcome,
        depth="standard",
        product_integration="embedded",
        cta="Use Leadpoet to apply this workflow with live buyer signals and account context.",
        instruction=instruction,
        source_ids=source_ids,
        research_run_id=research_run_id,
        evidence_summary=evidence_summary,
        trend_ids=trend_ids,
        trend_titles=trend_titles,
        reason=reason,
        score=round(score + metric_count * 0.5 + len(trend_titles), 3),
    )


def suggestion_title(stem: str, format: str) -> str:
    if format == "prompt_library":
        return f"The {stem} Prompt Library"
    if format == "playbook":
        return f"The {stem} Playbook"
    if format == "template_pack":
        return f"The {stem} Template Pack"
    return f"The {stem} Readiness Scorecard"


def suggestion_instruction(theme: _Theme, *, format: str, trend_titles: list[str]) -> str:
    format_instruction = {
        "prompt_library": "For every prompt include required inputs, the copy-ready prompt, usage steps, and an example output.",
        "playbook": "For every stage include its objective, concrete steps, an example, and exit criteria.",
        "template_pack": "Include copyable templates, completion checklists, usage notes, and a worked example.",
        "scorecard": "Include diagnostic questions, transparent scoring rules, score bands, and recommended next actions.",
    }[format]
    trend_context = f" Current trend alignment: {'; '.join(trend_titles)}." if trend_titles else ""
    return (
        f"Create a practical, original {FORMAT_LABELS[format]} organized around: {', '.join(theme.sections)}. "
        f"{format_instruction} Use only the selected source receipts for factual or performance claims; do not invent metrics, "
        "customer stories, benchmarks, or product capabilities. Position Leadpoet once as the operational step that supplies "
        f"current buyer signals and account context, without turning the resource into a sales pitch.{trend_context}"
    )


def select_format(theme: _Theme, global_formats: list[str], used_formats: set[str]) -> str:
    candidates = [*global_formats, *theme.format_order, *SUPPORTED_FORMATS]
    for format in candidates:
        if format in SUPPORTED_FORMATS and format not in used_formats:
            return format
    return theme.format_order[0]


def ranked_formats(sources: list[LeadMagnetSource]) -> list[str]:
    counts: Counter[str] = Counter()
    for source in sources:
        if source.format in SUPPORTED_FORMATS:
            counts[source.format] += source_rank(source)
    ranked = [format for format, _ in counts.most_common()]
    return [*ranked, *(format for format in SUPPORTED_FORMATS if format not in ranked)]


def source_rank(source: LeadMagnetSource) -> float:
    metric_bonus = 2.5 if source.evidence_strength == "metric_backed" else 0.5
    return metric_bonus + min(5.0, math.log1p(max(0.0, float(source.performance_score or 0.0))))


def source_text(source: LeadMagnetSource) -> str:
    return clean(
        " ".join(
            value
            for value in [
                source.title,
                source.sales_topic,
                source.promise,
                source.hook,
                source.audience,
                source.delivery_mechanism,
                (source.raw_text or "")[:3000],
            ]
            if value
        )
    )


def trend_text(trend: TrendItem) -> str:
    return clean(
        " ".join(
            value
            for value in [
                trend.title,
                trend.topic,
                trend.summary,
                trend.why_it_matters,
                trend.leadpoet_angle,
                trend.suggested_hooks,
            ]
            if value
        )
    )


def topic_idea_text(idea: TopicIdea) -> str:
    return clean(" ".join(value for value in [idea.title, idea.topic, idea.angle, idea.why_now] if value))


def keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


__all__ = ["LeadMagnetSuggestion", "build_lead_magnet_suggestions"]
