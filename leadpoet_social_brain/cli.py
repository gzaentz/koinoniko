from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
from collections import Counter
from dataclasses import replace

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.content_queue import (
    approve_queue_item,
    approve_week_x,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    require_queue_item,
    resolve_queue_edit_state,
    revise_queued_post,
    weekly_report_text,
)
from leadpoet_social_brain.categorization import XPostLLMCategorizer
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import (
    ContentQueueItem,
    ContextSection,
    DraftRequest,
    EngagementMetrics,
    Pattern,
    PostNote,
    QueuedPostVersion,
    SocialPost,
    TrendItem,
    utc_now,
)
from leadpoet_social_brain.editorial import dedupe_editorial_trends, editorial_theme_fingerprints
from leadpoet_social_brain.generation import (
    MISSING_PROVIDER_MESSAGE,
    PRODUCT_NAME_OPTIONAL_LANE_NAMES,
    SocialBrainGenerator,
    claim_audit_has_unsupported,
    exact_final_claim_coverage_violations,
    leadpoet_phrase_pattern_fingerprint,
    leadpoet_sentence_template_fingerprint,
    leadpoet_sentence_grammar_fingerprint,
    logic_audit_has_missing_bridge,
    natural_logic_audit_has_valid_status,
    natural_logic_audit_is_complete,
    natural_logic_audit_quotes_final,
    natural_quality_audit_contract_violations,
    primary_closing_target,
    primary_editorial_value_mode,
    primary_human_rhythm_target,
    primary_layout_target,
    primary_product_mention_lane,
    primary_product_mention_placement_target,
    primary_product_mention_syntax_target,
    primary_product_name_usage_target,
    primary_structure_shape,
    prompt_safe_automated_label,
    quality_audit_failure_warnings,
    trend_angle_for_generation,
    trend_specific_generation_angle,
)
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
from leadpoet_social_brain.lead_magnet_scout import research_lead_magnets
from leadpoet_social_brain.lead_magnets import (
    approve_lead_magnet,
    generate_lead_magnet,
    revise_lead_magnet,
)
from leadpoet_social_brain.notion_publisher import NotionPublishError, publish_to_notion
from leadpoet_social_brain.pattern_scout import (
    PATTERN_REFRESH_MISSING_PROVIDER,
    add_pattern_example,
    ingest_patterns_batch_csv,
    refresh_patterns,
)
from leadpoet_social_brain.platforms.x import get_x_adapter
from leadpoet_social_brain.platforms.x_api import (
    DEFAULT_IMPORT_POST_TYPE,
    DEFAULT_IMPORT_TOPIC,
    UNCATEGORIZED_TOPIC,
    MISSING_X_WRITE_CREDENTIALS_MESSAGE,
    XApiClient,
    XWriteClient,
    sync_x_metrics,
    sync_x_user_posts,
)
from leadpoet_social_brain.providers import load_provider_from_env, resolve_model_routing
from leadpoet_social_brain.storage.sqlite_store import DEFAULT_DB_PATH, DEFAULT_WEB_AUTH_USERNAME, SQLiteStore
from leadpoet_social_brain.trend_scout import DEFAULT_WEB_TRENDS_PER_WINDOW, refresh_web_trends

REPEATED_PHRASE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "not",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "to",
    "when",
    "where",
    "who",
    "why",
    "will",
    "with",
    "would",
}

LEADPOET_AUDIT_BAD_PATTERNS = [
    ("leadpoet is building", r"\bleadpoet\s+is\s+building\b"),
    ("leadpoet is built", r"\bleadpoet\s+is\s+built\b"),
    ("leadpoet is", r"\bleadpoet\s+is\b"),
    ("awkward spatial leadpoet phrasing", r"\b(?:near|around)\s+leadpoet\b"),
    ("for leadpoet", r"(?m)(?:^|\n\n)\s*for\s+leadpoet,\s+"),
    ("leadpoet modal lead-in", r"(?m)(?:^|[.!?]\s+|\n\n)\s*leadpoet\s+(?:should|can|needs?|has\s+to)\b"),
    ("that/this is where leadpoet", r"\b(?:that|this)\s+is\s+where\s+leadpoet\b|\bthat['’]?s\s+where\s+leadpoet\b"),
    ("that is the leadpoet closer", r"\bthat\s+is\s+the\s+leadpoet\s+(?:job|bar|point|constraint|product belief)\b"),
    ("the leadpoet noun-is definition", r"\bthe\s+leadpoet\s+(?:product belief|signal|point|constraint|job|bar|part)\s+is\b"),
    ("leadpoet fallback lead-in", r"(?m)(?:^|[.!?]\s+|\n\n)\s*leadpoet\s+(?:belongs|only\s+matters|must)\b"),
    ("leadpoet subject-verb lead-in", r"(?m)(?:^|[.!?]\s+|\n\n)\s*leadpoet\s+(?:owns?|makes?|starts?|keeps?|turns?|focuses|helps?|gives?|answers?|shows?|lets?|finds?|enables?|provides?|delivers?|powers?|unlocks?|brings?|drives?|creates?)\b"),
    ("leadpoet bet/job/role", r"(?m)(?:^|\n\n)\s*leadpoet(?:'s|’s)?\s+(?:bet|job|role)\b"),
    ("the layer worth building", r"(?m)(?:^|\n\n)\s*the\s+layer\s+worth\s+building\b"),
]

PROMPT_SEED_BAD_PATTERNS = [
    *LEADPOET_AUDIT_BAD_PATTERNS,
    ("leadpoet modifier-is-built", r"\bleadpoet(?:'s|’s)?\s+\w+\s+is\s+built\b"),
    ("generic lead-generation hook", r"\blead generation is becoming a timing problem, not just a volume problem\b"),
    ("generic sales-ai hook", r"\bthe sales ai debate is really a context debate\b"),
    ("generic ai hook", r"\bthe ai trend that matters is the one that changes buyer behavior\b"),
    ("generic saved-positioning angle", r"\buse this to talk about .+ through the lens of .+ saved positioning\b"),
]

PROMPT_SEED_SECTION_SPECS = [
    ("voice_examples", "Study these selected Voice Examples first:", ["\n\nFacts & Context:"]),
    ("facts_context", "Facts & Context:", ["\n\nVoice profile:"]),
    ("voice_profile", "Voice profile:", ["\n\nNatural Voice Calibration:"]),
    ("recent_outputs", "Recent Output Anti-Sameness:", ["\n\nActive Writing Rules:"]),
    ("market_context", "Timely market context:", ["\n\n\nUseful performance guidance:", "\n\nRequest:"]),
]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = SQLiteStore(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Leadpoet Social Brain v0.2")
    parser.add_argument(
        "--db",
        help=(
            "Path to SQLite database. Defaults to SOCIAL_BRAIN_DB, "
            f"then {DEFAULT_DB_PATH}."
        ),
    )
    subparsers = parser.add_subparsers(required=True)

    init_parser = subparsers.add_parser("init", help="Initialize DB and seed editable defaults.")
    init_parser.set_defaults(func=cmd_init)

    web_login = subparsers.add_parser("set-web-login", help="Set or reset the single web UI login.")
    web_login.add_argument("--username", default=DEFAULT_WEB_AUTH_USERNAME)
    web_login.add_argument("--workspace-id", help="Optional workspace scope for this login. Defaults to the username.")
    web_login.add_argument("--password", help="Optional. Prefer omitting this and entering the password prompt.")
    web_login.set_defaults(func=cmd_set_web_login)

    ingest = subparsers.add_parser("ingest-x", help="Ingest an X post example.")
    ingest.add_argument("--text", required=True)
    ingest.add_argument("--url")
    ingest.add_argument("--date")
    ingest.add_argument("--author", required=True)
    ingest.add_argument("--topic", required=True)
    ingest.add_argument("--post-type", required=True)
    add_metric_args(ingest)
    ingest.add_argument("--worked-reason")
    ingest.add_argument("--pattern")
    ingest.add_argument("--hook")
    ingest.add_argument("--structure")
    ingest.add_argument("--tone")
    ingest.add_argument("--cta")
    ingest.add_argument("--formatting")
    ingest.add_argument("--imitate", action="store_true")
    ingest.set_defaults(func=cmd_ingest_x)

    batch = subparsers.add_parser("ingest-x-batch", help="Ingest X post examples from a CSV file.")
    batch.add_argument("--csv", required=True, dest="csv_path")
    batch.add_argument("--default-author", default="Gavin")
    batch.set_defaults(func=cmd_ingest_x_batch)

    sync_x = subparsers.add_parser("sync-x-user", help="Pull a user's X posts from the X API.")
    sync_x.add_argument("--username", required=True)
    sync_x.add_argument("--limit", type=int, default=100)
    sync_x.add_argument("--start-time")
    sync_x.add_argument("--end-time")
    sync_x.add_argument("--exclude-replies", dest="exclude_replies", action="store_true", default=True)
    sync_x.add_argument("--include-replies", dest="exclude_replies", action="store_false")
    sync_x.add_argument("--exclude-retweets", dest="exclude_retweets", action="store_true", default=True)
    sync_x.add_argument("--include-retweets", dest="exclude_retweets", action="store_false")
    sync_x.add_argument("--default-topic")
    sync_x.add_argument("--default-post-type", default=DEFAULT_IMPORT_POST_TYPE)
    sync_x.add_argument("--auto-categorize", action="store_true")
    sync_x.add_argument("--imitate", action="store_true")
    sync_x.add_argument("--dry-run", action="store_true")
    sync_x.set_defaults(func=cmd_sync_x_user)

    sync_metrics = subparsers.add_parser("sync-x-metrics", help="Refresh public metrics for stored X posts.")
    sync_metrics.add_argument("--post-id")
    sync_metrics.add_argument("--x-post-id")
    sync_metrics.add_argument("--since-days", type=int)
    sync_metrics.add_argument("--limit", type=int, default=100)
    sync_metrics.add_argument("--dry-run", action="store_true")
    sync_metrics.set_defaults(func=cmd_sync_x_metrics)

    update_notes = subparsers.add_parser("update-post-notes", help="Update notes and labels for an existing post.")
    update_notes.add_argument("--post-id", required=True)
    update_notes.add_argument("--worked-reason")
    update_notes.add_argument("--imitate", choices=["true", "false"])
    update_notes.add_argument("--topic")
    update_notes.add_argument("--post-type")
    update_notes.add_argument("--hook")
    update_notes.add_argument("--structure")
    update_notes.add_argument("--tone")
    update_notes.add_argument("--cta")
    update_notes.add_argument("--formatting")
    update_notes.add_argument("--pattern")
    update_notes.set_defaults(func=cmd_update_post_notes)

    add_context = subparsers.add_parser("add-context", help="Add or update a context section.")
    add_context.add_argument("--kind", required=True)
    add_context.add_argument("--name", required=True)
    add_context.add_argument("--content", required=True)
    add_context.add_argument("--weight", type=float, default=1.0)
    add_context.add_argument("--inactive", action="store_true")
    add_context.set_defaults(func=cmd_add_context)

    add_pattern = subparsers.add_parser("add-pattern", help="Add or update a recurring pattern.")
    add_pattern.add_argument("--name", required=True)
    add_pattern.add_argument("--category", required=True)
    add_pattern.add_argument("--description", required=True)
    add_pattern.add_argument("--examples", default="", help="Comma-separated post ids.")
    add_pattern.add_argument("--inactive", action="store_true")
    add_pattern.set_defaults(func=cmd_add_pattern)

    add_pattern_example_parser = subparsers.add_parser("add-pattern-example", help="Add one Pattern Scout source example.")
    add_pattern_example_parser.add_argument("--source-url")
    add_pattern_example_parser.add_argument("--source-author")
    add_pattern_example_parser.add_argument("--source-text")
    add_pattern_example_parser.add_argument("--source-summary")
    add_pattern_example_parser.add_argument("--topic")
    add_pattern_example_parser.add_argument("--category")
    add_pattern_example_parser.add_argument("--pattern-name")
    add_pattern_example_parser.add_argument("--hook-style")
    add_pattern_example_parser.add_argument("--structure")
    add_pattern_example_parser.add_argument("--pacing")
    add_pattern_example_parser.add_argument("--tone")
    add_pattern_example_parser.add_argument("--formatting")
    add_pattern_example_parser.add_argument("--why-it-worked")
    add_pattern_example_parser.add_argument("--adaptation-rule")
    add_pattern_example_parser.add_argument("--engagement-notes")
    add_metric_args(add_pattern_example_parser)
    add_pattern_example_parser.add_argument("--collected-at")
    add_pattern_example_parser.add_argument("--window", default="manual", choices=["manual", "daily", "weekly", "monthly"])
    add_pattern_example_parser.add_argument("--status", default="active", choices=["active", "archived", "ignored"])
    add_pattern_example_parser.set_defaults(func=cmd_add_pattern_example)

    patterns_batch = subparsers.add_parser("ingest-patterns-batch", help="Import Pattern Scout examples from CSV.")
    patterns_batch.add_argument("--csv", required=True, dest="csv_path")
    patterns_batch.set_defaults(func=cmd_ingest_patterns_batch)

    list_patterns = subparsers.add_parser("list-patterns", help="List Pattern Scout examples.")
    list_patterns.add_argument("--status", default="active")
    list_patterns.add_argument("--window")
    list_patterns.add_argument("--limit", type=int, default=20)
    list_patterns.set_defaults(func=cmd_list_patterns)

    refresh_pattern_parser = subparsers.add_parser("refresh-patterns", help="Refresh Pattern Scout examples using ScrapingDog and the configured provider.")
    refresh_pattern_parser.add_argument("--limit", type=int, default=5)
    refresh_pattern_parser.add_argument("--window", default="weekly", choices=["weekly", "monthly"])
    refresh_pattern_parser.set_defaults(func=cmd_refresh_patterns)

    refresh_trends_parser = subparsers.add_parser("refresh-trends", help="Refresh Trend Scout sources and enrich selected trends.")
    refresh_trends_parser.add_argument("--limit", type=int, default=DEFAULT_WEB_TRENDS_PER_WINDOW)
    refresh_trends_parser.add_argument("--no-enrich", action="store_true")
    refresh_trends_parser.set_defaults(func=cmd_refresh_trends)

    research_magnets = subparsers.add_parser(
        "research-lead-magnets",
        help="Analyze recent sales/tech lead-magnet patterns and observed performance.",
    )
    research_magnets.add_argument("--window-days", type=int, default=30)
    research_magnets.add_argument("--limit", type=int, default=20)
    research_magnets.add_argument("--query")
    research_magnets.set_defaults(func=cmd_research_lead_magnets)

    generate_magnet = subparsers.add_parser("generate-lead-magnet", help="Create a review-ready long-form lead magnet.")
    generate_magnet.add_argument("--audience", required=True)
    generate_magnet.add_argument("--sales-outcome", required=True)
    generate_magnet.add_argument(
        "--format",
        choices=["prompt_library", "playbook", "template_pack", "scorecard"],
        default="prompt_library",
    )
    generate_magnet.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    generate_magnet.add_argument(
        "--product-integration",
        choices=["light", "embedded", "strong"],
        default="embedded",
    )
    generate_magnet.add_argument("--title")
    generate_magnet.add_argument("--cta")
    generate_magnet.add_argument("--instruction")
    generate_magnet.add_argument("--source-id", action="append", dest="source_ids")
    generate_magnet.add_argument("--research-run-id")
    generate_magnet.set_defaults(func=cmd_generate_lead_magnet)

    list_magnets = subparsers.add_parser("list-lead-magnets", help="List saved lead magnets.")
    list_magnets.add_argument("--status")
    list_magnets.add_argument("--format")
    list_magnets.add_argument("--limit", type=int, default=20)
    list_magnets.add_argument("--json", action="store_true", dest="json_output")
    list_magnets.set_defaults(func=cmd_list_lead_magnets)

    revise_magnet = subparsers.add_parser("revise-lead-magnet", help="Create a new AI revision of a lead magnet.")
    revise_magnet.add_argument("--lead-magnet-id", required=True)
    revise_magnet.add_argument("--instruction", required=True)
    revise_magnet.set_defaults(func=cmd_revise_lead_magnet)

    approve_magnet = subparsers.add_parser("approve-lead-magnet", help="Approve a lead magnet for Notion publishing.")
    approve_magnet.add_argument("--lead-magnet-id", required=True)
    approve_magnet.set_defaults(func=cmd_approve_lead_magnet)

    publish_magnet = subparsers.add_parser("publish-lead-magnet", help="Create or update an approved lead magnet in Notion.")
    publish_magnet.add_argument("--lead-magnet-id", required=True)
    publish_magnet.set_defaults(func=cmd_publish_lead_magnet)

    generate = subparsers.add_parser("generate-x", help="Generate X drafts.")
    generate.add_argument("--topic", required=True)
    generate.add_argument("--post-type", required=True)
    generate.add_argument("--angle")
    generate.add_argument("--audience")
    generate.add_argument("--tone")
    generate.add_argument("--cta")
    generate.add_argument("--length", choices=["short", "standard", "long"], default="standard")
    generate.add_argument("--extra-context")
    generate.add_argument("--max-chars", type=int)
    generate.add_argument("--use-trends", action="store_true")
    generate.add_argument("--use-patterns", action="store_true")
    generate.add_argument("--pattern-id", action="append", dest="pattern_ids", default=[])
    generate.set_defaults(func=cmd_generate_x)

    audit_naturalness = subparsers.add_parser(
        "audit-naturalness-x",
        help="Exercise the production natural-copy pipeline across trend posts and report quality metrics.",
    )
    audit_naturalness.add_argument("--count", type=int, default=20)
    audit_naturalness.add_argument("--trend-limit", type=int, default=0, help="Maximum matching trends to load. Use 0 for all current matching trends.")
    audit_naturalness.add_argument("--trend-status", default="unused")
    audit_naturalness.add_argument("--post-type", default="founder_insight")
    audit_naturalness.add_argument("--length", choices=["short", "standard", "long"], default="standard")
    audit_naturalness.add_argument("--save-drafts", action="store_true")
    audit_naturalness.add_argument(
        "--plan-only",
        action="store_true",
        help="Run the legacy structure/source-seed preflight without calling the provider; this does not exercise production generation.",
    )
    audit_naturalness.add_argument(
        "--legacy-structure",
        action="store_true",
        help="Exercise the former strategist/structure-matrix pipeline instead of the production natural-copy path.",
    )
    audit_naturalness.add_argument("--cover-all-trends", action="store_true", help="Raise the effective post count to cover every loaded matching trend.")
    audit_naturalness.add_argument("--json", action="store_true", dest="json_output")
    audit_naturalness.add_argument("--fail-on-warning", action="store_true")
    audit_naturalness.set_defaults(func=cmd_audit_naturalness_x)

    plan_week = subparsers.add_parser("plan-week-x", help="Generate a one-week X content queue.")
    plan_week.add_argument("--week-start", required=True)
    plan_week.add_argument("--posts", type=int, default=7)
    plan_week.add_argument("--use-trends", action="store_true")
    plan_week.add_argument("--use-patterns", action="store_true")
    plan_week.add_argument("--trend-window-days", type=int, default=14)
    plan_week.add_argument("--topic-mix")
    plan_week.add_argument("--cadence", default="daily_mornings")
    plan_week.add_argument("--default-post-type", default="founder_insight")
    plan_week.add_argument("--length", choices=["short", "standard", "long"], default="standard")
    plan_week.add_argument("--allow-evergreen", action="store_true")
    plan_week.set_defaults(func=cmd_plan_week_x)

    list_queue = subparsers.add_parser("list-content-queue", help="List queued content.")
    list_queue.add_argument("--status")
    list_queue.add_argument("--limit", type=int, default=20)
    list_queue.set_defaults(func=cmd_list_content_queue)

    update_queue = subparsers.add_parser("update-queued-post", help="Edit a queued post.")
    update_queue.add_argument("--queue-id", required=True)
    update_queue.add_argument("--text")
    update_queue.add_argument("--topic")
    update_queue.add_argument("--post-type")
    update_queue.add_argument("--angle")
    update_queue.add_argument("--scheduled-at")
    update_queue.add_argument("--notes")
    update_queue.add_argument(
        "--status",
        choices=["draft", "needs_review", "approved", "posted", "failed", "skipped"],
    )
    update_queue.set_defaults(func=cmd_update_queued_post)

    revise_queue = subparsers.add_parser("revise-queued-post", help="Generate AI revision options for a queued post.")
    revise_queue.add_argument("--queue-id", required=True)
    revise_queue.add_argument("--instruction", required=True)
    revise_queue.set_defaults(func=cmd_revise_queued_post)

    approve_queue = subparsers.add_parser("approve-queued-post", help="Approve one queued post.")
    approve_queue.add_argument("--queue-id", required=True)
    approve_queue.set_defaults(func=cmd_approve_queued_post)

    approve_week = subparsers.add_parser("approve-week-x", help="Approve all needs-review posts in a week.")
    approve_week.add_argument("--week-start", required=True)
    approve_week.set_defaults(func=cmd_approve_week_x)

    publish_due = subparsers.add_parser("publish-due-x", help="Post approved queued X items whose time has arrived.")
    publish_due.add_argument("--now")
    publish_due.add_argument("--limit", type=int, default=20)
    publish_due.add_argument("--author", default="Gavin")
    publish_due.set_defaults(func=cmd_publish_due_x)

    post_now = subparsers.add_parser("post-now-x", help="Immediately post one approved queued X item.")
    post_now.add_argument("--queue-id", required=True)
    post_now.add_argument("--author", default="Gavin")
    post_now.set_defaults(func=cmd_post_now_x)

    add_trend = subparsers.add_parser("add-trend", help="Manually add one trend item.")
    add_trend.add_argument("--title", required=True)
    add_trend.add_argument("--source-url")
    add_trend.add_argument("--source-type", default="other")
    add_trend.add_argument("--topic", required=True)
    add_trend.add_argument("--summary", required=True)
    add_trend.add_argument("--why-it-matters")
    add_trend.add_argument("--leadpoet-angle")
    add_trend.add_argument("--suggested-hooks")
    add_trend.add_argument("--urgency", default="medium")
    add_trend.add_argument("--date")
    add_trend.add_argument("--status", default="unused")
    add_trend.add_argument("--engagement-notes")
    add_trend.add_argument("--source-author")
    add_trend.add_argument("--raw-text")
    add_trend.set_defaults(func=cmd_add_trend)

    trends_batch = subparsers.add_parser("ingest-trends-batch", help="Import trend items from CSV.")
    trends_batch.add_argument("--csv", required=True, dest="csv_path")
    trends_batch.set_defaults(func=cmd_ingest_trends_batch)

    list_trends = subparsers.add_parser("list-trends", help="List recent trends.")
    list_trends.add_argument("--topic")
    list_trends.add_argument("--status", default="unused")
    list_trends.add_argument("--urgency")
    list_trends.add_argument("--limit", type=int, default=10)
    list_trends.set_defaults(func=cmd_list_trends)

    mark_trend = subparsers.add_parser("mark-trend-used", help="Mark a trend as used.")
    mark_trend.add_argument("--trend-id", required=True)
    mark_trend.set_defaults(func=cmd_mark_trend_used)

    publish = subparsers.add_parser("publish-draft-x", help="Record a generated draft as a published X post.")
    publish.add_argument("--draft-id", required=True)
    publish.add_argument("--url", required=True)
    publish.add_argument("--date", required=True)
    publish.add_argument("--author", required=True)
    add_metric_args(publish)
    publish.set_defaults(func=cmd_publish_draft_x)

    metrics = subparsers.add_parser("add-metrics", help="Add a metrics snapshot to a post.")
    metrics.add_argument("--post-id", required=True)
    add_metric_args(metrics)
    metrics.set_defaults(func=cmd_add_metrics)

    list_posts = subparsers.add_parser("list-posts", help="List stored posts.")
    list_posts.add_argument("--platform")
    list_posts.add_argument("--status")
    list_posts.add_argument("--limit", type=int, default=20)
    list_posts.set_defaults(func=cmd_list_posts)

    rebuild = subparsers.add_parser("rebuild-style-guide", help="Rebuild style guide from imitate=true winners.")
    rebuild.add_argument("--limit", type=int, default=12)
    rebuild.set_defaults(func=cmd_rebuild_style_guide)

    analytics = subparsers.add_parser("analytics", help="Show transparent performance feedback.")
    analytics.set_defaults(func=cmd_analytics)

    weekly_report = subparsers.add_parser("weekly-report-x", help="Summarize recent X performance.")
    weekly_report.add_argument("--since-days", type=int, default=7)
    weekly_report.set_defaults(func=cmd_weekly_report_x)

    prompt = subparsers.add_parser("prompt-x", help="Print the generation prompt without drafting.")
    prompt.add_argument("--topic", required=True)
    prompt.add_argument("--post-type", required=True)
    prompt.add_argument("--angle")
    prompt.add_argument("--audience")
    prompt.add_argument("--tone")
    prompt.add_argument("--cta")
    prompt.add_argument("--extra-context")
    prompt.add_argument("--max-chars", type=int)
    prompt.add_argument("--use-trends", action="store_true")
    prompt.add_argument("--use-patterns", action="store_true")
    prompt.add_argument("--pattern-id", action="append", dest="pattern_ids", default=[])
    prompt.set_defaults(func=cmd_prompt_x)

    return parser


def add_metric_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--impressions", type=int)
    parser.add_argument("--likes", type=int)
    parser.add_argument("--replies", type=int)
    parser.add_argument("--reposts", type=int)
    parser.add_argument("--quotes", type=int)
    parser.add_argument("--bookmarks", type=int)
    parser.add_argument("--engagement-rate", type=float)


def cmd_init(store: SQLiteStore, args: argparse.Namespace) -> int:
    store.seed_defaults()
    print(f"Initialized Social Brain DB at {store.path}")
    return 0


def cmd_set_web_login(store: SQLiteStore, args: argparse.Namespace) -> int:
    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match.")
    user = store.save_web_auth_user(args.username, password, workspace_id=args.workspace_id)
    print(f"Saved web login for {user.username} in workspace {user.workspace_id} at {store.path}")
    return 0


def cmd_ingest_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    metrics = metrics_from_args(args)
    post = ingest_x_post(
        store,
        text=args.text,
        url=args.url,
        date=args.date,
        author=args.author,
        topic=args.topic,
        post_type=args.post_type,
        metrics=metrics,
        worked_reason=args.worked_reason,
        pattern_name=args.pattern,
        hook=args.hook,
        structure=args.structure,
        tone=args.tone,
        cta=args.cta,
        formatting=args.formatting,
        imitate=args.imitate,
    )
    print(f"Ingested X post {post.id}")
    return 0


def cmd_ingest_x_batch(store: SQLiteStore, args: argparse.Namespace) -> int:
    result = ingest_x_batch_csv(store, args.csv_path, default_author=args.default_author)
    print("Imported X batch")
    print(f"Posts imported: {result.imported}")
    print(f"Posts skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
        if len(result.errors) > 20:
            print(f"- ... {len(result.errors) - 20} more")
    print("Top topics:")
    print_counter(result.topics)
    print("Top post types:")
    print_counter(result.post_types)
    return 0


def cmd_sync_x_user(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        client = XApiClient.from_env()
        categorizer = None
        if args.auto_categorize:
            provider = load_configured_provider(store)
            if not provider:
                raise SystemExit(MISSING_PROVIDER_MESSAGE)
            categorizer = XPostLLMCategorizer(provider).categorize
        default_topic = args.default_topic or (
            UNCATEGORIZED_TOPIC if args.auto_categorize else DEFAULT_IMPORT_TOPIC
        )
        result = sync_x_user_posts(
            store,
            client,
            username=args.username,
            limit=args.limit,
            default_topic=default_topic,
            default_post_type=args.default_post_type,
            start_time=args.start_time,
            end_time=args.end_time,
            exclude_replies=args.exclude_replies,
            exclude_retweets=args.exclude_retweets,
            dry_run=args.dry_run,
            imitate=args.imitate,
            categorizer=categorizer,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    mode = "Dry run" if args.dry_run else "Sync"
    print(f"{mode} X user @{args.username}")
    print(f"Auto categorize: {'on' if args.auto_categorize else 'off'}")
    print(f"Tweets fetched: {result.fetched}")
    if args.dry_run:
        print(f"Would import: {result.would_import}")
    else:
        print(f"Posts imported: {result.imported}")
    print(f"Duplicates skipped: {result.duplicates}")
    print(f"Posts skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
        if len(result.errors) > 20:
            print(f"- ... {len(result.errors) - 20} more")
    print("Top topics:")
    print_counter(result.topics)
    print("Top post types:")
    print_counter(result.post_types)
    if args.dry_run and result.dry_run_posts:
        print("Dry-run preview:")
        for post in result.dry_run_posts[:5]:
            preview = post.text.replace("\n", " ")[:100]
            print(f"- {post.date or 'no date'} | {post.topic} | {post.post_type} | {post.url} | {preview}")
    return 0


def cmd_sync_x_metrics(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        client = XApiClient.from_env()
        result = sync_x_metrics(
            store,
            client,
            post_id=args.post_id,
            x_post_id=args.x_post_id,
            since_days=args.since_days,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    mode = "Dry run" if args.dry_run else "Sync"
    print(f"{mode} X metrics")
    print(f"Posts checked: {result.checked}")
    if args.dry_run:
        print(f"Would update: {result.would_update}")
    else:
        print(f"Metrics updated: {result.updated}")
    print(f"Skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
        if len(result.errors) > 20:
            print(f"- ... {len(result.errors) - 20} more")
    print("Top movers:")
    if result.top_movers:
        for mover in result.top_movers:
            impression = (
                f", impressions +{mover.impression_delta}"
                if mover.impression_delta is not None
                else ""
            )
            print(
                f"- {mover.post_id}: engagements +{mover.total_engagement_delta}"
                f"{impression} ({mover.url or 'no url'})"
            )
    else:
        print("- no movement yet")
    return 0


def cmd_update_post_notes(store: SQLiteStore, args: argparse.Namespace) -> int:
    post = store.get_post(args.post_id)
    if not post:
        raise SystemExit(f"No post found for id {args.post_id}")

    if args.post_type and post.platform == "x":
        get_x_adapter().validate_post_type(args.post_type)
    if args.topic or args.post_type:
        post = store.save_post(
            SocialPost(
                id=post.id,
                platform=post.platform,
                text=post.text,
                url=post.url,
                published_at=post.published_at,
                author=post.author,
                topic=args.topic or post.topic,
                post_type=args.post_type or post.post_type,
                status=post.status,
                source_draft_id=post.source_draft_id,
                created_at=post.created_at,
                updated_at=post.updated_at,
            )
        )

    existing = store.notes_by_post_id([post.id]).get(post.id)
    note = store.save_note(
        PostNote(
            post_id=post.id,
            worked_reason=value_or_existing(args.worked_reason, existing.worked_reason if existing else None),
            pattern_name=value_or_existing(args.pattern, existing.pattern_name if existing else None),
            hook=value_or_existing(args.hook, existing.hook if existing else None),
            structure=value_or_existing(args.structure, existing.structure if existing else None),
            tone=value_or_existing(args.tone, existing.tone if existing else None),
            cta=value_or_existing(args.cta, existing.cta if existing else None),
            formatting=value_or_existing(args.formatting, existing.formatting if existing else None),
            imitate=(
                parse_bool_arg(args.imitate)
                if args.imitate is not None
                else (existing.imitate if existing else False)
            ),
        )
    )

    print(f"Updated post {post.id}")
    print(f"Topic: {post.topic}")
    print(f"Post type: {post.post_type}")
    print(f"Imitate: {str(note.imitate).lower()}")
    print(f"Worked reason: {note.worked_reason or 'not set'}")
    print(f"Pattern: {note.pattern_name or 'not set'}")
    print(f"Hook: {note.hook or 'not set'}")
    print(f"Structure: {note.structure or 'not set'}")
    print(f"Tone: {note.tone or 'not set'}")
    print(f"CTA: {note.cta or 'not set'}")
    print(f"Formatting: {note.formatting or 'not set'}")
    return 0


def cmd_add_context(store: SQLiteStore, args: argparse.Namespace) -> int:
    section = store.save_context_section(
        ContextSection(
            kind=args.kind,
            name=args.name,
            content=args.content,
            weight=args.weight,
            active=not args.inactive,
        )
    )
    print(f"Saved context section {section.kind}/{section.name}")
    return 0


def cmd_add_pattern(store: SQLiteStore, args: argparse.Namespace) -> int:
    examples = [item.strip() for item in args.examples.split(",") if item.strip()]
    pattern = store.save_pattern(
        Pattern(
            name=args.name,
            category=args.category,
            description=args.description,
            example_post_ids=examples,
            active=not args.inactive,
        )
    )
    print(f"Saved pattern {pattern.category}/{pattern.name}")
    return 0


def cmd_add_pattern_example(store: SQLiteStore, args: argparse.Namespace) -> int:
    pattern = add_pattern_example(
        store,
        source_url=args.source_url,
        source_author=args.source_author,
        source_text=args.source_text,
        source_summary=args.source_summary,
        topic=args.topic,
        category=args.category,
        pattern_name=args.pattern_name,
        hook_style=args.hook_style,
        structure=args.structure,
        pacing=args.pacing,
        tone=args.tone,
        formatting=args.formatting,
        why_it_worked=args.why_it_worked,
        adaptation_rule=args.adaptation_rule,
        engagement_notes=args.engagement_notes,
        likes=args.likes,
        replies=args.replies,
        reposts=args.reposts,
        quotes=args.quotes,
        bookmarks=args.bookmarks,
        impressions=args.impressions,
        collected_at=args.collected_at,
        window=args.window,
        status=args.status,
    )
    print(f"Saved Pattern Scout example {pattern.id}")
    print(f"Pattern: {pattern.pattern_name or 'Observed pattern'}")
    print(f"Structure: {pattern.structure or 'not set'}")
    print(f"Rule: {pattern.adaptation_rule or 'Use the structure, not the wording.'}")
    return 0


def cmd_ingest_patterns_batch(store: SQLiteStore, args: argparse.Namespace) -> int:
    result = ingest_patterns_batch_csv(store, args.csv_path)
    print("Imported Pattern Scout batch")
    print(f"Patterns imported: {result.imported}")
    print(f"Patterns skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
    print("Top categories:")
    print_counter(result.categories)
    print("Top pattern names:")
    print_counter(result.pattern_names)
    return 0


def cmd_list_patterns(store: SQLiteStore, args: argparse.Namespace) -> int:
    patterns = store.list_viral_patterns(status=args.status, window=args.window, limit=args.limit)
    if not patterns:
        print("No Pattern Scout examples found.")
        return 0
    for pattern in patterns:
        signal = pattern_engagement_signal(pattern)
        print(
            f"{pattern.id} | {pattern.window} | {pattern.category or 'other'} | "
            f"{pattern.pattern_name or 'Observed pattern'} | {signal}"
        )
        if pattern.structure:
            print(f"  Structure: {pattern.structure}")
        if pattern.why_it_worked:
            print(f"  Why: {pattern.why_it_worked}")
        if pattern.source_url:
            print(f"  Source: {pattern.source_url}")
    return 0


def cmd_refresh_patterns(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit(PATTERN_REFRESH_MISSING_PROVIDER)
    try:
        result = refresh_patterns(store, provider, limit=args.limit, window=args.window)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("Pattern Scout refresh")
    print(f"Candidates fetched: {result.fetched}")
    print(f"Patterns stored: {result.stored}")
    print(f"Skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
    return 0


def cmd_refresh_trends(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = None
    provider_warning = ""
    if not args.no_enrich:
        try:
            provider = load_configured_provider(store)
        except Exception as exc:  # noqa: BLE001 - keep trend refresh usable without digest enrichment.
            provider_warning = f"Trend enrichment provider unavailable: {exc}"
    groups = refresh_web_trends(
        store,
        limit=args.limit,
        provider=provider,
        enrich=not args.no_enrich,
        enrich_limit=args.limit,
    )
    print("Trend Scout refresh")
    print(f"Candidates found: {groups.candidates_found}")
    print(f"Trends stored: {groups.trends_stored}")
    print(f"Enriched: {groups.enriched}")
    print(f"Partial: {groups.partial}")
    print(f"Failed: {groups.failed}")
    print(f"Daily: {len(groups.daily)}")
    print(f"Weekly: {len(groups.weekly)}")
    if groups.sources:
        print(f"Sources: {', '.join(groups.sources)}")
    errors = [*groups.errors, provider_warning] if provider_warning else list(groups.errors)
    if errors:
        print("Errors:")
        for error in errors[:20]:
            print(f"- {error}")
    return 0


def cmd_research_lead_magnets(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        provider = load_configured_provider(store)
    except Exception as exc:  # noqa: BLE001 - deterministic research remains useful without classification.
        provider = None
        print(f"Provider classification unavailable: {exc}")
    try:
        result = research_lead_magnets(
            store,
            provider,
            window_days=args.window_days,
            limit=args.limit,
            query=args.query,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Research run: {result.run.id}")
    print(f"Window: {result.run.window_start} to {result.run.window_end}")
    print(f"Candidates found: {result.candidates_found}")
    print(f"Qualified sources: {len(result.sources)}")
    print(
        "Metric-backed sources: "
        f"{sum(1 for source in result.sources if source.evidence_strength == 'metric_backed')}"
    )
    for source in result.sources:
        print(
            f"- {source.id} | {source.format} | {source.evidence_strength} | "
            f"score={source.performance_score or 0:.3f} | {source.title}"
        )
        if source.source_url:
            print(f"  {source.source_url}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
    return 0


def cmd_generate_lead_magnet(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit("Lead-magnet generation requires a configured LLM provider.")
    try:
        result = generate_lead_magnet(
            store,
            provider,
            audience=args.audience,
            sales_outcome=args.sales_outcome,
            format=args.format,
            depth=args.depth,
            product_integration=args.product_integration,
            title=args.title,
            cta=args.cta,
            instruction=args.instruction,
            source_ids=args.source_ids,
            research_run_id=args.research_run_id,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Created lead magnet {result.item.id} in {result.item.status}")
    print(f"Title: {result.item.title}")
    print(f"Format: {result.item.format}")
    print(f"Version: {result.version.id}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
    print("\n" + result.item.markdown)
    return 0


def cmd_list_lead_magnets(store: SQLiteStore, args: argparse.Namespace) -> int:
    items = store.list_lead_magnets(status=args.status, format=args.format, limit=args.limit)
    if args.json_output:
        print(
            json.dumps(
                [
                    {
                        "id": item.id,
                        "title": item.title,
                        "promise": item.promise,
                        "audience": item.audience,
                        "sales_outcome": item.sales_outcome,
                        "format": item.format,
                        "status": item.status,
                        "source_ids": item.source_ids,
                        "notion_url": item.notion_url,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                    }
                    for item in items
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not items:
        print("No lead magnets found.")
        return 0
    for item in items:
        print(f"{item.id} | {item.status} | {item.format} | {item.title}")
        print(f"  Audience: {item.audience} | Outcome: {item.sales_outcome}")
        if item.notion_url:
            print(f"  Notion: {item.notion_url}")
    return 0


def cmd_revise_lead_magnet(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit("Lead-magnet revision requires a configured LLM provider.")
    try:
        result = revise_lead_magnet(
            store,
            provider,
            lead_magnet_id=args.lead_magnet_id,
            instruction=args.instruction,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Saved revision {result.version.id} for {result.item.id}; status reset to {result.item.status}.")
    return 0


def cmd_approve_lead_magnet(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        item = approve_lead_magnet(store, args.lead_magnet_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Approved lead magnet {item.id}")
    return 0


def cmd_publish_lead_magnet(store: SQLiteStore, args: argparse.Namespace) -> int:
    item = store.get_lead_magnet(args.lead_magnet_id)
    if not item:
        raise SystemExit(f"No lead magnet found for id {args.lead_magnet_id}")
    if item.status not in {"approved", "published"}:
        raise SystemExit("Approve the lead magnet before publishing it to Notion.")
    try:
        published = publish_to_notion(
            item.content,
            title=item.title,
            existing_page_id=item.notion_page_id,
            icon="🧲",
        )
    except Exception as exc:  # noqa: BLE001 - persist retry state for partial page creation.
        partial_page_id = exc.page_id if isinstance(exc, NotionPublishError) else item.notion_page_id
        store.save_lead_magnet(
            replace(
                item,
                status="approved",
                notion_page_id=partial_page_id,
                failure_reason=str(exc),
                updated_at=utc_now(),
            )
        )
        raise SystemExit(str(exc)) from exc
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
    print(f"Published lead magnet {saved.id} to {saved.notion_url or saved.notion_page_id}")
    print(f"Notion blocks: {published.block_count} in {published.batch_count} batch(es)")
    return 0


def cmd_generate_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit(MISSING_PROVIDER_MESSAGE)
    generator = SocialBrainGenerator(store, provider=provider)
    request = DraftRequest(
        platform="x",
        topic=args.topic,
        post_type=args.post_type,
        angle=args.angle,
        audience=args.audience,
        tone=args.tone,
        call_to_action=args.cta,
        length_mode=args.length,
        max_chars=args.max_chars,
        extra_context=args.extra_context,
        use_trends=args.use_trends,
        use_patterns=args.use_patterns,
        pattern_ids=args.pattern_ids,
        prompt_profile="natural",
    )
    drafts = generator.generate(request)
    for index, draft in enumerate(drafts, start=1):
        print(f"\nDraft {index} ({len(draft.text)} chars, id {draft.id})")
        print(draft.text)
        print(f"Explanation: {draft.explanation}")
    return 0


def cmd_audit_naturalness_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    if args.plan_only:
        payload = build_naturalness_plan_audit(
            store,
            count=args.count,
            trend_limit=args.trend_limit,
            trend_status=args.trend_status,
            post_type=args.post_type,
            length_mode=args.length,
            cover_all_trends=args.cover_all_trends,
        )
    else:
        provider = load_configured_provider(store)
        if not provider:
            raise SystemExit(MISSING_PROVIDER_MESSAGE)
        generator = SocialBrainGenerator(store, provider=provider)
        builder = build_naturalness_audit if args.legacy_structure else build_production_naturalness_audit
        payload = builder(
            store,
            generator,
            count=args.count,
            trend_limit=args.trend_limit,
            trend_status=args.trend_status,
            post_type=args.post_type,
            length_mode=args.length,
            save_drafts=args.save_drafts,
            cover_all_trends=args.cover_all_trends,
        )
    if args.json_output:
        print(json.dumps(payload, indent=2))
        return 1 if args.fail_on_warning and payload["warnings"] else 0
    if payload.get("mode") == "plan_only":
        print(f"Planned: {payload['post_count']} post(s) across {payload['trend_count']} trend(s)")
        print("Provider calls: no")
        print("Scope: legacy structure/source-seed preflight only; production generation was not exercised")
    else:
        print(f"Generated: {payload['post_count']} post(s) across {payload['trend_count']} trend(s)")
    print(f"Trend coverage: {payload['covered_trend_count']} covered, {payload['uncovered_trend_count']} uncovered")
    if payload.get("mode") == "production_natural":
        print("Pipeline: production natural writer -> evaluator -> final quality gate")
        print(f"Attempted: {payload['attempted_count']}")
        print(f"Generation failures: {payload['generation_failure_count']}")
        print(
            "Verified pipeline receipts: "
            f"{payload['pipeline_receipt_pass_count']} passed, "
            f"{payload['pipeline_receipt_failure_count']} failed"
        )
        print(f"Saved drafts: {'yes' if args.save_drafts else 'no'}")
        print(f"Unique first lines: {payload['unique_first_lines']}")
        print(f"Unique closing lines: {payload['unique_last_lines']}")
        print(f"Exact duplicate posts: {payload['exact_duplicate_count']}")
        print(
            "Character length: "
            f"min {payload['min_chars']}, avg {payload['average_chars']}, max {payload['max_chars']}"
        )
        print(f"Posts naming Leadpoet: {payload['leadpoet_mentioned_count']}")
        print("Bad phrase counts:")
        for key, value in sorted(payload["bad_phrase_counts"].items()):
            print(f"- {key}: {value}")
        print("Evaluator selection modes:")
        for key, value in sorted(payload["selection_mode_counts"].items()):
            print(f"- {key}: {value}")
        print("Evaluator contract attempts:")
        for key, value in sorted(payload["evaluator_contract_attempt_counts"].items()):
            print(f"- {key}: {value}")
        print("Final quality gate resolutions:")
        for key, value in sorted(payload["quality_gate_resolution_counts"].items()):
            print(f"- {key}: {value}")
        print("Editorial thesis counts:")
        for key, value in sorted(payload["editorial_theme_counts"].items()):
            print(f"- {key}: {value}")
        print("Generated layout fingerprint counts:")
        for key, value in sorted(payload["layout_fingerprint_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet grammar fingerprint counts:")
        for key, value in sorted(payload["leadpoet_grammar_fingerprint_counts"].items()):
            print(f"- {key}: {value}")
        print("Repeated stock phrase counts:")
        for key, value in sorted(payload["repeated_phrase_counts"].items()):
            print(f"- {key}: {value}")
        if payload["warnings"]:
            print("Warnings:")
            for warning in payload["warnings"]:
                print(f"- {warning}")
        return 1 if args.fail_on_warning and payload["warnings"] else 0
    if payload.get("mode") != "plan_only":
        print(f"Saved drafts: {'yes' if args.save_drafts else 'no'}")
        print("Leadpoet mention shapes:")
        for key, value in sorted(payload["leadpoet_shapes"].items()):
            print(f"- {key}: {value}")
        print("Bad phrase counts:")
        for key, value in sorted(payload["bad_phrase_counts"].items()):
            print(f"- {key}: {value}")
        print(f"Unique first lines: {payload['unique_first_lines']}")
        print(f"Unique closing lines: {payload['unique_last_lines']}")
    print("Primary structure option counts:")
    for key, value in sorted(payload["primary_structure_option_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary reader value mode counts:")
    for key, value in sorted(payload["primary_editorial_value_mode_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary product mention lane counts:")
    for key, value in sorted(payload["primary_product_mention_lane_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary product mention placement target counts:")
    for key, value in sorted(payload["primary_product_mention_placement_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary product mention syntax target counts:")
    for key, value in sorted(payload["primary_product_mention_syntax_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary product name usage target counts:")
    for key, value in sorted(payload["primary_product_name_usage_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary layout target counts:")
    for key, value in sorted(payload["primary_layout_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary closing target counts:")
    for key, value in sorted(payload["primary_closing_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Primary human rhythm target counts:")
    for key, value in sorted(payload["primary_human_rhythm_target_counts"].items()):
        print(f"- {key}: {value}")
    print("Request angle counts:")
    for key, value in sorted(payload["request_angle_counts"].items()):
        print(f"- {key}: {value}")
    if payload.get("prompt_seed_violations"):
        print("Prompt seed scan:")
        for violation in payload["prompt_seed_violations"][:10]:
            print(
                f"- post {violation['index']} {violation['section']} {violation['label']}: "
                f"{violation['excerpt']}"
            )
    if payload.get("mode") != "plan_only":
        print("Leadpoet mention pattern counts:")
        for key, value in sorted(payload["leadpoet_mention_pattern_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet actual syntax counts:")
        for key, value in sorted(payload["leadpoet_actual_syntax_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet grammar fingerprint counts:")
        for key, value in sorted(payload["leadpoet_grammar_fingerprint_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet phrase pattern counts:")
        for key, value in sorted(payload["leadpoet_phrase_pattern_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet sentence template counts:")
        for key, value in sorted(payload["leadpoet_sentence_template_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet paragraph position counts:")
        for key, value in sorted(payload["leadpoet_paragraph_position_counts"].items()):
            print(f"- {key}: {value}")
        print("Leadpoet sentence position counts:")
        for key, value in sorted(payload["leadpoet_sentence_position_counts"].items()):
            print(f"- {key}: {value}")
        print(f"Leadpoet final-line mentions: {payload['leadpoet_final_line_mentions']}")
        print(f"Leadpoet sentence-start mentions: {payload['leadpoet_sentence_start_mentions']}")
        print("Generated layout fingerprint counts:")
        for key, value in sorted(payload["layout_fingerprint_counts"].items()):
            print(f"- {key}: {value}")
        print("Repeated stock phrase counts:")
        for key, value in sorted(payload["repeated_phrase_counts"].items()):
            print(f"- {key}: {value}")
    if payload["warnings"]:
        print("Warnings:")
        for warning in payload["warnings"]:
            print(f"- {warning}")
    return 1 if args.fail_on_warning and payload["warnings"] else 0


def build_naturalness_plan_audit(
    store: SQLiteStore,
    *,
    count: int,
    trend_limit: int,
    trend_status: str | None,
    post_type: str,
    length_mode: str,
    cover_all_trends: bool = False,
) -> dict[str, object]:
    if count <= 0:
        raise SystemExit("--count must be greater than 0.")
    effective_trend_limit = trend_limit if trend_limit > 0 else 1000
    trends = dedupe_editorial_trends(
        store.list_trends(status=trend_status or None, limit=effective_trend_limit)
    )
    if not trends:
        raise SystemExit("No trends found for naturalness audit.")
    requested_count = count
    if cover_all_trends:
        count = max(count, len(trends))
    audit_plan = naturalness_audit_request_plan(
        trends,
        count=count,
        post_type=post_type,
        length_mode=length_mode,
    )
    rows: list[dict[str, object]] = []
    for index, trend, request, assignment in audit_plan:
        rows.append(
            {
                "index": index + 1,
                "trend_id": trend.id,
                "trend_title": trend.title,
                "topic": trend.topic,
                "request_angle": request.angle or "",
                **assignment,
            }
        )
    prompt_seed_violations = prompt_seed_violations_for_audit_plan(store, audit_plan)
    covered_trend_ids = {str(row["trend_id"]) for row in rows if row["trend_id"]}
    uncovered_trend_count = max(0, len(trends) - len(covered_trend_ids))
    primary_structure_counts = Counter(str(row["primary_structure_option"]) for row in rows)
    primary_editorial_value_counts = Counter(str(row["primary_editorial_value_mode"]) for row in rows)
    primary_product_lane_counts = Counter(str(row["primary_product_mention_lane"]) for row in rows)
    primary_product_placement_counts = Counter(str(row["primary_product_mention_placement_target"]) for row in rows)
    primary_product_syntax_counts = Counter(str(row["primary_product_mention_syntax_target"]) for row in rows)
    primary_product_name_usage_counts = Counter(str(row["primary_product_name_usage_target"]) for row in rows)
    request_angle_counts = Counter(str(row["request_angle"]) for row in rows if row["request_angle"])
    product_name_optional_guidance_count = sum(
        1
        for row in rows
        if str(row["primary_product_mention_lane"]) in PRODUCT_NAME_OPTIONAL_LANE_NAMES
        or str(row["primary_product_mention_syntax_target"]) == "name omitted"
    )
    primary_layout_target_counts = Counter(str(row["primary_layout_target"]) for row in rows)
    primary_closing_target_counts = Counter(str(row["primary_closing_target"]) for row in rows)
    primary_human_rhythm_target_counts = Counter(str(row["primary_human_rhythm_target"]) for row in rows)
    warnings: list[str] = []
    if len(primary_structure_counts) < min(5, len(trends), count):
        warnings.append("Structure guidance did not vary enough across trends.")
    if len(primary_editorial_value_counts) < min(6, len(trends), count):
        warnings.append("Reader value guidance did not vary enough across trends.")
    if len(primary_product_lane_counts) < min(5, len(trends), count):
        warnings.append("Product mention guidance did not vary enough across trends.")
    if len(primary_product_placement_counts) < min(4, len(trends), count):
        warnings.append("Product mention placement guidance did not vary enough across trends.")
    if len(primary_product_syntax_counts) < min(4, len(trends), count):
        warnings.append("Product mention syntax guidance did not vary enough across trends.")
    if len(primary_product_name_usage_counts) < min(3, len(trends), count):
        warnings.append("Product name usage guidance did not vary enough across trends.")
    if len(rows) >= 8 and product_name_optional_guidance_count < max(2, len(rows) // 5):
        warnings.append("Product-name-optional guidance did not appear often enough across trends.")
    if len(primary_layout_target_counts) < min(4, len(trends), count):
        warnings.append("Layout target guidance did not vary enough across trends.")
    if len(primary_closing_target_counts) < min(4, len(trends), count):
        warnings.append("Closing target guidance did not vary enough across trends.")
    if len(primary_human_rhythm_target_counts) < min(4, len(trends), count):
        warnings.append("Human rhythm guidance did not vary enough across trends.")
    if len(covered_trend_ids) > 1 and len(request_angle_counts) < min(5, len(covered_trend_ids), count):
        warnings.append("Request angles did not vary enough across covered trends.")
    append_plan_concentration_warning(warnings, "Structure guidance", primary_structure_counts, len(rows))
    append_plan_concentration_warning(warnings, "Reader value guidance", primary_editorial_value_counts, len(rows))
    append_plan_concentration_warning(warnings, "Product mention guidance", primary_product_lane_counts, len(rows))
    append_plan_concentration_warning(
        warnings,
        "Product mention placement guidance",
        primary_product_placement_counts,
        len(rows),
    )
    append_plan_concentration_warning(
        warnings,
        "Product mention syntax guidance",
        primary_product_syntax_counts,
        len(rows),
    )
    append_plan_concentration_warning(
        warnings,
        "Product name usage guidance",
        primary_product_name_usage_counts,
        len(rows),
    )
    append_plan_concentration_warning(warnings, "Layout target guidance", primary_layout_target_counts, len(rows))
    append_plan_concentration_warning(warnings, "Closing target guidance", primary_closing_target_counts, len(rows))
    append_plan_concentration_warning(warnings, "Human rhythm guidance", primary_human_rhythm_target_counts, len(rows))
    if len(covered_trend_ids) > 1:
        append_plan_concentration_warning(warnings, "Request angle guidance", request_angle_counts, len(rows))
    if prompt_seed_violations:
        first = prompt_seed_violations[0]
        warnings.append(
            "Prompt seed scan found formulaic or generic source text before provider generation: "
            f"{len(prompt_seed_violations)} issue(s), first={first['label']} in {first['section']}."
        )
    if uncovered_trend_count:
        warnings.append(
            f"{uncovered_trend_count} loaded trend(s) were not covered; raise --count or lower --trend-limit for full coverage."
        )
    return {
        "mode": "plan_only",
        "scope": "legacy_structure_source_seed_only",
        "production_pipeline_exercised": False,
        "trend_count": len(trends),
        "post_count": len(rows),
        "requested_count": requested_count,
        "cover_all_trends": cover_all_trends,
        "covered_trend_count": len(covered_trend_ids),
        "uncovered_trend_count": uncovered_trend_count,
        "loaded_all_matching_trends": trend_limit <= 0,
        "trend_limit": trend_limit,
        "primary_structure_option_counts": dict(primary_structure_counts),
        "primary_editorial_value_mode_counts": dict(primary_editorial_value_counts),
        "primary_product_mention_lane_counts": dict(primary_product_lane_counts),
        "primary_product_mention_placement_target_counts": dict(primary_product_placement_counts),
        "primary_product_mention_syntax_target_counts": dict(primary_product_syntax_counts),
        "primary_product_name_usage_target_counts": dict(primary_product_name_usage_counts),
        "product_name_optional_guidance_count": product_name_optional_guidance_count,
        "request_angle_counts": dict(request_angle_counts),
        "primary_layout_target_counts": dict(primary_layout_target_counts),
        "primary_closing_target_counts": dict(primary_closing_target_counts),
        "primary_human_rhythm_target_counts": dict(primary_human_rhythm_target_counts),
        "prompt_seed_violations": prompt_seed_violations,
        "warnings": warnings,
        "rows": rows,
    }


def prompt_seed_violations_for_audit_plan(
    store: SQLiteStore,
    audit_plan: list[tuple[int, TrendItem, DraftRequest, dict[str, str]]],
) -> list[dict[str, object]]:
    prompt_seed_violations: list[dict[str, object]] = []
    prompt_builder = SocialBrainGenerator(store)
    for index, trend, request, _assignment in audit_plan:
        prompt = prompt_builder.build_prompt(request)
        for violation in prompt_seed_violations_for_prompt(prompt):
            prompt_seed_violations.append(
                {
                    "index": index + 1,
                    "trend_id": trend.id,
                    "trend_title": trend.title,
                    **violation,
                }
            )
    return prompt_seed_violations


def prompt_seed_gate_message(violations: list[dict[str, object]]) -> str:
    first = violations[0] if violations else {}
    return (
        "Prompt seed scan found formulaic or generic source text before provider generation: "
        f"{len(violations)} issue(s), first={first.get('label')} in {first.get('section')}. "
        "Run the plan-only audit for details."
    )


def prompt_seed_violations_for_prompt(prompt: str) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for section, text in prompt_seed_sections(prompt):
        for label, pattern in PROMPT_SEED_BAD_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.I):
                excerpt = prompt_seed_excerpt(text, match.start(), match.end())
                key = (section, label, excerpt.lower())
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    {
                        "section": section,
                        "label": label,
                        "excerpt": excerpt,
                    }
                )
    return violations


def prompt_seed_sections(prompt: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for name, start_marker, end_markers in PROMPT_SEED_SECTION_SPECS:
        start = prompt.find(start_marker)
        if start < 0:
            continue
        start += len(start_marker)
        ends = [prompt.find(marker, start) for marker in end_markers]
        ends = [end for end in ends if end >= 0]
        end = min(ends) if ends else len(prompt)
        section = prompt[start:end].strip()
        if section:
            sections.append((name, section))
    return sections


def prompt_seed_excerpt(text: str, start: int, end: int, *, window: int = 80) -> str:
    prefix_start = max(0, start - window)
    suffix_end = min(len(text), end + window)
    excerpt = " ".join(text[prefix_start:suffix_end].split())
    if prefix_start > 0:
        excerpt = f"...{excerpt}"
    if suffix_end < len(text):
        excerpt = f"{excerpt}..."
    return excerpt[:220]


def append_plan_concentration_warning(
    warnings: list[str],
    label: str,
    counts: Counter[str],
    total: int,
) -> None:
    if not counts or total <= 1:
        return
    threshold = max(4, (total + 1) // 2)
    value, value_count = counts.most_common(1)[0]
    if value_count >= threshold:
        warnings.append(f"{label} is too concentrated: {value} appears {value_count}/{total} planned post(s).")


NATURALNESS_ASSIGNMENT_FIELDS = (
    "primary_structure_option",
    "primary_editorial_value_mode",
    "primary_product_mention_lane",
    "primary_product_mention_placement_target",
    "primary_product_mention_syntax_target",
    "primary_product_name_usage_target",
    "primary_layout_target",
    "primary_closing_target",
    "primary_human_rhythm_target",
)


def naturalness_audit_request_plan(
    trends: list[TrendItem],
    *,
    count: int,
    post_type: str,
    length_mode: str,
) -> list[tuple[int, TrendItem, DraftRequest, dict[str, str]]]:
    trends = dedupe_editorial_trends(trends)
    if not trends:
        raise ValueError("At least one distinct trend is required for a naturalness audit plan.")
    counters: dict[str, Counter[str]] = {
        field: Counter()
        for field in NATURALNESS_ASSIGNMENT_FIELDS
    }
    combo_counts: Counter[tuple[str, ...]] = Counter()
    plan: list[tuple[int, TrendItem, DraftRequest, dict[str, str]]] = []
    candidate_slot_count = max(60, count * 3)
    for index in range(count):
        trend = trends[index % len(trends)]
        best_score: tuple[int, ...] | None = None
        best_request: DraftRequest | None = None
        best_assignment: dict[str, str] | None = None
        for slot in range(candidate_slot_count):
            request = naturalness_audit_request_for_trend(
                trend,
                post_type=post_type,
                length_mode=length_mode,
                variation_slot=slot,
            )
            assignment = naturalness_guidance_assignment(request, trend)
            score = naturalness_assignment_balance_score(
                assignment,
                counters=counters,
                combo_counts=combo_counts,
                previous_assignment=plan[-1][3] if plan else None,
                slot=slot,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_request = request
                best_assignment = assignment
        if best_request is None or best_assignment is None:
            raise RuntimeError("Unable to build naturalness audit request plan.")
        for field in NATURALNESS_ASSIGNMENT_FIELDS:
            counters[field][best_assignment[field]] += 1
        combo_counts[tuple(best_assignment[field] for field in NATURALNESS_ASSIGNMENT_FIELDS)] += 1
        plan.append((index, trend, best_request, best_assignment))
    return plan


def naturalness_guidance_assignment(request: DraftRequest, trend: TrendItem) -> dict[str, str]:
    trends = [trend]
    return {
        "primary_structure_option": primary_structure_shape(request, trends)[0],
        "primary_editorial_value_mode": primary_editorial_value_mode(request, trends)[0],
        "primary_product_mention_lane": primary_product_mention_lane(request, trends)[0],
        "primary_product_mention_placement_target": primary_product_mention_placement_target(request, trends)[0],
        "primary_product_mention_syntax_target": primary_product_mention_syntax_target(request, trends)[0],
        "primary_product_name_usage_target": primary_product_name_usage_target(request, trends)[0],
        "primary_layout_target": primary_layout_target(request, trends)[0],
        "primary_closing_target": primary_closing_target(request, trends)[0],
        "primary_human_rhythm_target": primary_human_rhythm_target(request, trends)[0],
    }


def naturalness_assignment_balance_score(
    assignment: dict[str, str],
    *,
    counters: dict[str, Counter[str]],
    combo_counts: Counter[tuple[str, ...]],
    previous_assignment: dict[str, str] | None,
    slot: int,
) -> tuple[int, ...]:
    next_counts = [
        counters[field][assignment[field]] + 1
        for field in NATURALNESS_ASSIGNMENT_FIELDS
    ]
    core_fields = tuple(
        field for field in NATURALNESS_ASSIGNMENT_FIELDS
        if field != "primary_product_name_usage_target"
    )
    core_next_counts = [
        counters[field][assignment[field]] + 1
        for field in core_fields
    ]
    combo = tuple(assignment[field] for field in NATURALNESS_ASSIGNMENT_FIELDS)
    consecutive_overlap = 0
    if previous_assignment:
        consecutive_overlap = sum(
            1
            for field in NATURALNESS_ASSIGNMENT_FIELDS
            if assignment[field] == previous_assignment[field]
        )
    return (
        max(core_next_counts),
        counters["primary_structure_option"][assignment["primary_structure_option"]] + 1,
        counters["primary_editorial_value_mode"][assignment["primary_editorial_value_mode"]] + 1,
        counters["primary_product_mention_lane"][assignment["primary_product_mention_lane"]] + 1,
        max(next_counts),
        sum(count * count for count in core_next_counts),
        sum(count * count for count in next_counts),
        combo_counts[combo] + 1,
        consecutive_overlap,
        slot,
    )


def naturalness_audit_request_for_trend(
    trend: TrendItem,
    *,
    post_type: str,
    length_mode: str,
    variation_slot: int | None = None,
) -> DraftRequest:
    extra_context = "Naturalness audit: use this trend, vary the post structure, and avoid a formulaic product definition."
    if variation_slot is not None:
        extra_context = (
            f"{extra_context} Batch variation slot {variation_slot}: use a distinct route for structure, "
            "Leadpoet placement, sentence rhythm, and close. Do not print the slot."
        )
    source_label = prompt_safe_automated_label(
        trend.title,
        fallback="the selected source",
        max_chars=180,
    )
    return DraftRequest(
        platform="x",
        topic=prompt_safe_automated_label(
            trend.topic,
            fallback="sales AI",
            max_chars=100,
        ),
        post_type=post_type,
        angle=(
            f"Use {source_label} as source evidence. Extract one source-specific lesson and do not force "
            "a product, buyer-timing, or account-context conclusion."
        ),
        extra_context=extra_context,
        angle_is_factual_evidence=False,
        extra_context_is_factual_evidence=False,
        use_trends=True,
        trend_ids=[trend.id],
        length_mode=length_mode,
    )


def production_naturalness_audit_request_for_trend(
    trend: TrendItem,
    *,
    post_type: str,
    length_mode: str,
) -> DraftRequest:
    """Build the same request shape used by normal production generation.

    The legacy audit injects structure and product-placement variation instructions
    into ``extra_context``. A production audit must not teach the model that it is
    being tested or route through those matrices, so the trend, idea, and natural
    prompt profile are the only audit-specific inputs.
    """

    return DraftRequest(
        platform="x",
        topic=prompt_safe_automated_label(
            trend.topic,
            fallback="sales AI",
            max_chars=100,
        ),
        post_type=post_type,
        angle=trend_specific_generation_angle(trend),
        angle_is_factual_evidence=False,
        use_trends=True,
        trend_ids=[trend.id],
        length_mode=length_mode,
        prompt_profile="natural",
    )


def production_pipeline_receipt(
    draft: object,
    *,
    trend_id: str,
) -> dict[str, object]:
    """Return independently checkable evidence that the production path ran."""

    request = getattr(draft, "request", None)
    snapshot = getattr(draft, "context_snapshot", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_request = snapshot.get("request")
    snapshot_request = snapshot_request if isinstance(snapshot_request, dict) else {}
    evaluation = snapshot.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    contract = evaluation.get("contract")
    contract = contract if isinstance(contract, dict) else {}
    quality_gate = evaluation.get("natural_quality_gate")
    quality_gate = quality_gate if isinstance(quality_gate, dict) else {}
    originality_receipt = quality_gate.get("originality")
    originality_receipt = originality_receipt if isinstance(originality_receipt, dict) else {}
    originality_diagnostics = originality_receipt.get("diagnostics")
    originality_diagnostics = originality_diagnostics if isinstance(originality_diagnostics, dict) else {}
    models_used = snapshot.get("models_used")
    models_used = models_used if isinstance(models_used, dict) else {}
    selected_trends = snapshot.get("selected_trends")
    selected_trends = selected_trends if isinstance(selected_trends, list) else []
    warnings = evaluation.get("warnings")
    writer_diversity = evaluation.get("writer_option_diversity")
    writer_diversity = writer_diversity if isinstance(writer_diversity, dict) else {}
    exact_final_audit = evaluation.get("exact_final_audit")
    exact_final_audit = exact_final_audit if isinstance(exact_final_audit, dict) else {}
    draft_text = str(getattr(draft, "text", "") or "")
    draft_text_hash = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    logic_audit = evaluation.get("logic_audit")
    if isinstance(logic_audit, dict) and any(key in logic_audit for key in ("bridge_status", "status")):
        logic_rows: list[object] = [logic_audit]
    elif isinstance(logic_audit, dict):
        logic_rows = list(logic_audit.values())
    elif isinstance(logic_audit, list):
        logic_rows = logic_audit
    else:
        logic_rows = []
    valid_logic_audit = (
        natural_logic_audit_has_valid_status(logic_audit)
        and not logic_audit_has_missing_bridge(logic_audit)
        and natural_logic_audit_is_complete(logic_audit)
        and natural_logic_audit_quotes_final(logic_audit, final_text=draft_text)
    )
    claim_audit = evaluation.get("claim_audit")
    claim_coverage_violations = (
        exact_final_claim_coverage_violations(
            claim_audit,
            final_text=draft_text,
        )
        if isinstance(claim_audit, (dict, list))
        else ["claim audit is missing"]
    )
    quality_audit = evaluation.get("quality_audit")
    quality_audit_violations = [
        *natural_quality_audit_contract_violations(
            quality_audit,
            final_text=draft_text,
        ),
        *quality_audit_failure_warnings(quality_audit),
    ]
    exact_source = str(exact_final_audit.get("source") or "")
    exact_attempts = exact_final_audit.get("audit_call_attempts")
    initial_reuse_violations = exact_final_audit.get("initial_reuse_violations")
    exact_source_consistent = (
        exact_source == "initial_typed_evaluator"
        and type(exact_attempts) is int
        and exact_attempts == 0
        and exact_final_audit.get("reused_initial_audit") is True
        and evaluation.get("initial_evaluator_audited_text_sha256") == draft_text_hash
        and isinstance(initial_reuse_violations, list)
        and not initial_reuse_violations
    ) or (
        exact_source == "exact_final_typed_evaluator"
        and type(exact_attempts) is int
        and exact_attempts in {1, 2}
        and exact_final_audit.get("reused_initial_audit") is False
        and isinstance(initial_reuse_violations, list)
    )
    try:
        contract_attempts = int(contract.get("attempts") or 0)
    except (TypeError, ValueError):
        contract_attempts = 0
    try:
        writer_diversity_attempts = int(writer_diversity.get("attempts") or 0)
    except (TypeError, ValueError):
        writer_diversity_attempts = 0

    checks = {
        "draft_request_uses_natural_profile": getattr(request, "prompt_profile", None) == "natural",
        "snapshot_request_uses_natural_profile": snapshot_request.get("prompt_profile") == "natural",
        "provider_generation_recorded": snapshot.get("generation_mode") == "provider",
        "quality_review_recorded": snapshot.get("quality_mode") == "quality_review",
        "natural_strategy_recorded": isinstance(snapshot.get("strategy_summary"), dict),
        "strategist_call_skipped": "strategist" in models_used and models_used.get("strategist") is None,
        "writer_route_recorded": "natural_writer" in models_used,
        "evaluator_route_recorded": "evaluator" in models_used,
        "evaluator_contract_valid": contract.get("valid") is True,
        "evaluator_contract_attempts_recorded": contract_attempts in {1, 2},
        "writer_option_diversity_recorded": (
            writer_diversity.get("valid") is True
            and writer_diversity_attempts in {1, 2}
            and isinstance(writer_diversity.get("initial_violations"), list)
        ),
        "logic_audit_recorded": bool(logic_rows) and valid_logic_audit,
        "claim_audit_recorded": (
            isinstance(claim_audit, (dict, list))
            and not claim_coverage_violations
            and not claim_audit_has_unsupported(claim_audit)
        ),
        "structured_quality_audit_recorded": (
            isinstance(quality_audit, dict)
            and not quality_audit_violations
        ),
        "evaluator_warnings_cleared": isinstance(warnings, list) and not warnings,
        "selection_recorded": evaluation.get("selection_mode") in {"writer_option", "evaluator_rewrite"},
        "final_quality_gate_passed": quality_gate.get("passed") is True,
        "final_quality_gate_resolution_recorded": bool(str(quality_gate.get("resolution") or "").strip()),
        "final_originality_gate_passed": (
            originality_receipt.get("passed") is True
            and originality_receipt.get("violations") == []
        ),
        "final_originality_receipt_recorded": (
            isinstance(originality_diagnostics.get("source_count"), int)
            and isinstance(originality_diagnostics.get("raw_source_count"), int)
            and isinstance(originality_diagnostics.get("matches"), list)
        ),
        "semantic_originality_receipt_recorded": (
            isinstance(originality_diagnostics.get("semantic_source_count"), int)
            and isinstance(originality_diagnostics.get("semantic_lineage_count"), int)
            and isinstance(originality_diagnostics.get("semantic_candidate_atoms"), list)
            and isinstance(originality_diagnostics.get("semantic_exempt_atoms"), list)
            and isinstance(originality_diagnostics.get("semantic_prior_lineage_counts"), dict)
            and isinstance(originality_diagnostics.get("semantic_matches"), list)
        ),
        "exact_final_audit_recorded": (
            exact_final_audit.get("contract_version") == "natural_exact_final_v1"
            and exact_final_audit.get("passed") is True
            and exact_final_audit.get("audited_text_sha256") == draft_text_hash
            and exact_source_consistent
        ),
        "selected_trend_recorded": any(
            isinstance(value, dict) and str(value.get("id") or "") == str(trend_id)
            for value in selected_trends
        ),
        "context_receipt_containers_recorded": (
            isinstance(snapshot.get("selected_context_sections"), list)
            and isinstance(snapshot.get("selected_examples"), list)
            and isinstance(snapshot.get("skills"), list)
            and isinstance(snapshot.get("active_writing_rules"), dict)
        ),
        "quality_notes_recorded": bool(str(snapshot.get("quality_notes") or "").strip()),
    }
    violations = [name for name, passed in checks.items() if not passed]
    return {
        "passed": not violations,
        "checks": checks,
        "violations": violations,
        "selection_mode": str(evaluation.get("selection_mode") or "missing"),
        "evaluator_contract_attempts": contract_attempts,
        "quality_gate_resolution": str(quality_gate.get("resolution") or "missing"),
    }


def build_production_naturalness_audit(
    store: SQLiteStore,
    generator: SocialBrainGenerator,
    *,
    count: int,
    trend_limit: int,
    trend_status: str | None,
    post_type: str,
    length_mode: str,
    save_drafts: bool,
    cover_all_trends: bool = False,
) -> dict[str, object]:
    """Exercise and audit the real two-call natural-copy production path."""

    if count <= 0:
        raise SystemExit("--count must be greater than 0.")
    effective_trend_limit = trend_limit if trend_limit > 0 else 1000
    trends = dedupe_editorial_trends(
        store.list_trends(status=trend_status or None, limit=effective_trend_limit)
    )
    if not trends:
        raise SystemExit("No trends found for naturalness audit.")
    requested_count = count
    if cover_all_trends:
        count = max(count, len(trends))

    rows: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    batch_recent_outputs: list[str] = []
    for index in range(count):
        trend = trends[index % len(trends)]
        request = production_naturalness_audit_request_for_trend(
            trend,
            post_type=post_type,
            length_mode=length_mode,
        )
        try:
            generated = generator.generate(
                request,
                variants=1,
                save=save_drafts,
                recent_output_texts=batch_recent_outputs,
            )
            if not generated:
                raise ValueError("Production generator returned no draft.")
            draft = generated[0]
        except (ValueError, RuntimeError) as exc:
            failures.append(
                {
                    "index": index + 1,
                    "trend_id": trend.id,
                    "trend_title": trend.title,
                    "error_type": type(exc).__name__,
                    "error": " ".join(str(exc).split())[:500],
                }
            )
            continue

        snapshot = draft.context_snapshot if isinstance(draft.context_snapshot, dict) else {}
        evaluation = snapshot.get("evaluation")
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        receipt = production_pipeline_receipt(draft, trend_id=trend.id)
        text = str(draft.text)
        row = {
            "index": index + 1,
            "trend_id": trend.id,
            "trend_title": trend.title,
            "topic": trend.topic,
            "request_angle": draft.request.angle or "",
            "text": text,
            "chars": len(text),
            "first_line": first_nonempty_line(text),
            "last_line": last_nonempty_line(text),
            "layout_fingerprint": post_layout_fingerprint(text),
            "editorial_themes": editorial_theme_fingerprints(text),
            "leadpoet_grammar_fingerprint": leadpoet_grammar_fingerprint_for_text(text),
            "pipeline_receipt": receipt,
            "evaluation_selection_mode": receipt["selection_mode"],
            "evaluator_contract_attempts": receipt["evaluator_contract_attempts"],
            "quality_gate_resolution": receipt["quality_gate_resolution"],
            "quality_notes": str(snapshot.get("quality_notes") or ""),
        }
        rows.append(row)
        batch_recent_outputs.append(text)

    warnings: list[str] = []
    receipt_failures = [
        {
            "index": row["index"],
            "trend_id": row["trend_id"],
            "violations": row["pipeline_receipt"]["violations"],
        }
        for row in rows
        if not row["pipeline_receipt"]["passed"]
    ]
    if failures:
        warnings.append(
            f"Production generation failed for {len(failures)}/{count} attempted post(s)."
        )
    if receipt_failures:
        warnings.append(
            f"Production pipeline receipt verification failed for {len(receipt_failures)}/{len(rows)} generated post(s)."
        )

    covered_trend_ids = {
        str(row["trend_id"])
        for row in rows
        if row["trend_id"] and row["pipeline_receipt"]["passed"]
    }
    uncovered_trend_count = max(0, len(trends) - len(covered_trend_ids))
    if uncovered_trend_count:
        warnings.append(
            f"{uncovered_trend_count} loaded trend(s) produced no verified post; raise --count, use --cover-all-trends, or inspect generation failures."
        )

    texts = [str(row["text"]) for row in rows]
    text_blob = "\n\n".join(texts).lower()
    bad_phrase_counts = leadpoet_bad_phrase_counts(text_blob)
    if any(bad_phrase_counts.values()):
        warnings.append("Cookie-cutter or formulaic Leadpoet phrasing still appeared in production output.")

    unique_first_lines = len({str(row["first_line"]).casefold() for row in rows if row["first_line"]})
    unique_last_lines = len({str(row["last_line"]).casefold() for row in rows if row["last_line"]})
    if rows and unique_first_lines < min(5, len(rows)):
        warnings.append("Production first lines are not varied enough.")
    if rows and unique_last_lines < min(5, len(rows)):
        warnings.append("Production closing lines are not varied enough.")

    normalized_counts = Counter(" ".join(text.casefold().split()) for text in texts if text.strip())
    exact_duplicate_count = sum(value - 1 for value in normalized_counts.values() if value > 1)
    if exact_duplicate_count:
        warnings.append(f"Production output contained {exact_duplicate_count} exact duplicate post(s).")

    layout_counts = Counter(str(row["layout_fingerprint"]) for row in rows if row["layout_fingerprint"])
    if rows and len(layout_counts) < min(4, len(rows)):
        warnings.append("Production post layouts are not varied enough.")
    elif layout_counts and layout_counts.most_common(1)[0][1] >= max(3, (len(rows) + 1) // 2):
        layout, layout_count = layout_counts.most_common(1)[0]
        warnings.append(
            f"Production post layouts are too concentrated: {layout} appears {layout_count}/{len(rows)} times."
        )

    editorial_theme_counts: Counter[str] = Counter()
    for row in rows:
        editorial_theme_counts.update(str(value) for value in row["editorial_themes"] if str(value))
    if len(rows) >= 5 and editorial_theme_counts:
        theme, theme_count = editorial_theme_counts.most_common(1)[0]
        if theme_count >= max(3, (len(rows) + 1) // 2):
            warnings.append(
                f"Production editorial thesis is too concentrated: {theme} appears {theme_count}/{len(rows)} times."
            )

    grammar_counts = Counter(
        str(row["leadpoet_grammar_fingerprint"])
        for row in rows
        if row["leadpoet_grammar_fingerprint"] and row["leadpoet_grammar_fingerprint"] != "no_leadpoet"
    )
    leadpoet_mentioned_count = sum(grammar_counts.values())
    if leadpoet_mentioned_count >= 3 and grammar_counts.most_common(1)[0][1] >= max(
        3, (leadpoet_mentioned_count + 1) // 2
    ):
        grammar, grammar_count = grammar_counts.most_common(1)[0]
        warnings.append(
            f"Production Leadpoet grammar is too concentrated: {grammar} appears {grammar_count}/{leadpoet_mentioned_count} mentions."
        )
    if len(rows) >= 5 and leadpoet_mentioned_count >= max(4, (len(rows) * 4 + 4) // 5):
        warnings.append(
            f"Leadpoet is named in too much of the thought-leadership batch: {leadpoet_mentioned_count}/{len(rows)} post(s)."
        )

    repeated_phrase_counts = repeated_batch_phrase_counts(texts)
    if repeated_phrase_counts:
        phrase, phrase_count = next(iter(repeated_phrase_counts.items()))
        warnings.append(
            f"Repeated stock phrase appeared across production posts: '{phrase}' appears {phrase_count}/{len(rows)} times."
        )

    selection_mode_counts = Counter(str(row["evaluation_selection_mode"]) for row in rows)
    evaluator_contract_attempt_counts = Counter(str(row["evaluator_contract_attempts"]) for row in rows)
    quality_gate_resolution_counts = Counter(str(row["quality_gate_resolution"]) for row in rows)
    contract_retry_count = sum(1 for row in rows if row["evaluator_contract_attempts"] == 2)
    if len(rows) >= 4 and contract_retry_count >= max(2, (len(rows) + 3) // 4):
        warnings.append(
            f"Evaluator contract retries are too frequent: {contract_retry_count}/{len(rows)} generated post(s)."
        )

    char_counts = [int(row["chars"]) for row in rows]
    return {
        "mode": "production_natural",
        "pipeline": "natural_writer_evaluator_final_gate",
        "trend_count": len(trends),
        "post_count": len(rows),
        "attempted_count": count,
        "requested_count": requested_count,
        "cover_all_trends": cover_all_trends,
        "covered_trend_count": len(covered_trend_ids),
        "uncovered_trend_count": uncovered_trend_count,
        "loaded_all_matching_trends": trend_limit <= 0,
        "trend_limit": trend_limit,
        "generation_failure_count": len(failures),
        "generation_failures": failures,
        "pipeline_receipt_pass_count": len(rows) - len(receipt_failures),
        "pipeline_receipt_failure_count": len(receipt_failures),
        "pipeline_receipt_failures": receipt_failures,
        "selection_mode_counts": dict(selection_mode_counts),
        "evaluator_contract_attempt_counts": dict(evaluator_contract_attempt_counts),
        "evaluator_contract_retry_count": contract_retry_count,
        "quality_gate_resolution_counts": dict(quality_gate_resolution_counts),
        "bad_phrase_counts": bad_phrase_counts,
        "unique_first_lines": unique_first_lines,
        "unique_last_lines": unique_last_lines,
        "exact_duplicate_count": exact_duplicate_count,
        "layout_fingerprint_counts": dict(layout_counts),
        "editorial_theme_counts": dict(editorial_theme_counts),
        "leadpoet_grammar_fingerprint_counts": dict(grammar_counts),
        "leadpoet_mentioned_count": leadpoet_mentioned_count,
        "repeated_phrase_counts": repeated_phrase_counts,
        "min_chars": min(char_counts) if char_counts else 0,
        "max_chars": max(char_counts) if char_counts else 0,
        "average_chars": round(sum(char_counts) / len(char_counts), 1) if char_counts else 0.0,
        "warnings": warnings,
        "rows": rows,
    }


def build_naturalness_audit(
    store: SQLiteStore,
    generator: SocialBrainGenerator,
    *,
    count: int,
    trend_limit: int,
    trend_status: str | None,
    post_type: str,
    length_mode: str,
    save_drafts: bool,
    cover_all_trends: bool = False,
) -> dict[str, object]:
    if count <= 0:
        raise SystemExit("--count must be greater than 0.")
    effective_trend_limit = trend_limit if trend_limit > 0 else 1000
    trends = dedupe_editorial_trends(
        store.list_trends(status=trend_status or None, limit=effective_trend_limit)
    )
    if not trends:
        raise SystemExit("No trends found for naturalness audit.")
    requested_count = count
    if cover_all_trends:
        count = max(count, len(trends))
    rows: list[dict[str, object]] = []
    batch_recent_outputs: list[str] = []
    audit_plan = naturalness_audit_request_plan(
        trends,
        count=count,
        post_type=post_type,
        length_mode=length_mode,
    )
    prompt_seed_violations = prompt_seed_violations_for_audit_plan(store, audit_plan)
    if prompt_seed_violations:
        raise SystemExit(prompt_seed_gate_message(prompt_seed_violations))
    for index, trend, request, _assignment in audit_plan:
        draft = generator.generate(
            request,
            variants=1,
            save=save_drafts,
            recent_output_texts=batch_recent_outputs,
        )[0]
        primary_structure = ""
        first_structure = ""
        primary_option = (
            draft.context_snapshot.get("primary_structure_option")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(primary_option, dict):
            primary_structure = str(primary_option.get("name") or "")
        options = draft.context_snapshot.get("structure_variation_options") if isinstance(draft.context_snapshot, dict) else None
        if isinstance(options, list) and options and isinstance(options[0], dict):
            first_structure = str(options[0].get("name") or "")
        if not primary_structure:
            primary_structure = first_structure
        primary_editorial_value = ""
        editorial_value_option = (
            draft.context_snapshot.get("primary_editorial_value_mode")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(editorial_value_option, dict):
            primary_editorial_value = str(editorial_value_option.get("name") or "")
        primary_product_lane = ""
        first_product_lane = ""
        primary_product_option = (
            draft.context_snapshot.get("primary_product_mention_lane")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(primary_product_option, dict):
            primary_product_lane = str(primary_product_option.get("name") or "")
        product_options = draft.context_snapshot.get("product_mention_options") if isinstance(draft.context_snapshot, dict) else None
        if isinstance(product_options, list) and product_options and isinstance(product_options[0], dict):
            first_product_lane = str(product_options[0].get("name") or "")
        if not primary_product_lane:
            primary_product_lane = first_product_lane
        primary_product_placement = ""
        product_placement_option = (
            draft.context_snapshot.get("primary_product_mention_placement_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(product_placement_option, dict):
            primary_product_placement = str(product_placement_option.get("name") or "")
        primary_product_syntax = ""
        product_syntax_option = (
            draft.context_snapshot.get("primary_product_mention_syntax_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(product_syntax_option, dict):
            primary_product_syntax = str(product_syntax_option.get("name") or "")
        primary_product_name_usage = ""
        product_name_usage_option = (
            draft.context_snapshot.get("primary_product_name_usage_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(product_name_usage_option, dict):
            primary_product_name_usage = str(product_name_usage_option.get("name") or "")
        primary_layout_target = ""
        layout_option = (
            draft.context_snapshot.get("primary_layout_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(layout_option, dict):
            primary_layout_target = str(layout_option.get("name") or "")
        primary_closing_target = ""
        closing_option = (
            draft.context_snapshot.get("primary_closing_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(closing_option, dict):
            primary_closing_target = str(closing_option.get("name") or "")
        primary_human_rhythm_target = ""
        rhythm_option = (
            draft.context_snapshot.get("primary_human_rhythm_target")
            if isinstance(draft.context_snapshot, dict)
            else None
        )
        if isinstance(rhythm_option, dict):
            primary_human_rhythm_target = str(rhythm_option.get("name") or "")
        rows.append(
            {
                "index": index + 1,
                "trend_id": trend.id,
                "trend_title": trend.title,
                "text": draft.text,
                "chars": len(draft.text),
                "request_angle": draft.request.angle or "",
                "leadpoet_shape": leadpoet_mention_shape(draft.text),
                "leadpoet_mention_pattern": leadpoet_mention_pattern(draft.text),
                "leadpoet_actual_syntax": leadpoet_actual_syntax(draft.text),
                "leadpoet_grammar_fingerprint": leadpoet_grammar_fingerprint_for_text(draft.text),
                "leadpoet_phrase_pattern": leadpoet_phrase_pattern_for_text(draft.text),
                "leadpoet_sentence_template": leadpoet_sentence_template(draft.text),
                "leadpoet_paragraph_position": leadpoet_text_position(draft.text),
                "leadpoet_in_final_line": leadpoet_in_closing_text_unit(draft.text),
                "leadpoet_sentence_position": leadpoet_sentence_position(draft.text),
                "leadpoet_starts_sentence": leadpoet_starts_sentence(draft.text),
                "layout_fingerprint": post_layout_fingerprint(draft.text),
                "first_line": first_nonempty_line(draft.text),
                "last_line": last_nonempty_line(draft.text),
                "primary_structure_option": primary_structure,
                "primary_editorial_value_mode": primary_editorial_value,
                "first_structure_option": first_structure,
                "primary_product_mention_lane": primary_product_lane,
                "first_product_mention_lane": first_product_lane,
                "primary_product_mention_placement_target": primary_product_placement,
                "primary_product_mention_syntax_target": primary_product_syntax,
                "primary_product_name_usage_target": primary_product_name_usage,
                "primary_layout_target": primary_layout_target,
                "primary_closing_target": primary_closing_target,
                "primary_human_rhythm_target": primary_human_rhythm_target,
            }
        )
        batch_recent_outputs.append(draft.text)
    text_blob = "\n\n".join(str(row["text"]) for row in rows).lower()
    warnings: list[str] = []
    covered_trend_ids = {str(row["trend_id"]) for row in rows if row["trend_id"]}
    uncovered_trend_count = max(0, len(trends) - len(covered_trend_ids))
    bad_phrase_counts = leadpoet_bad_phrase_counts(text_blob)
    if any(bad_phrase_counts.values()):
        warnings.append("Cookie-cutter or formulaic Leadpoet phrasing still appeared.")
    unique_first_lines = len({str(row["first_line"]).lower() for row in rows if row["first_line"]})
    if unique_first_lines < min(5, len(rows)):
        warnings.append("First lines are not varied enough.")
    unique_last_lines = len({str(row["last_line"]).lower() for row in rows if row["last_line"]})
    if unique_last_lines < min(5, len(rows)):
        warnings.append("Closing lines are not varied enough.")
    primary_structure_counts = Counter(str(row["primary_structure_option"]) for row in rows if row["primary_structure_option"])
    primary_editorial_value_counts = Counter(
        str(row["primary_editorial_value_mode"])
        for row in rows
        if row["primary_editorial_value_mode"]
    )
    first_structure_counts = Counter(str(row["first_structure_option"]) for row in rows if row["first_structure_option"])
    if len(primary_structure_counts) < min(5, len(trends), count):
        warnings.append("Structure guidance did not vary enough across trends.")
    if len(primary_editorial_value_counts) < min(6, len(trends), count):
        warnings.append("Reader value guidance did not vary enough across trends.")
    primary_product_lane_counts = Counter(
        str(row["primary_product_mention_lane"]) for row in rows if row["primary_product_mention_lane"]
    )
    first_product_lane_counts = Counter(str(row["first_product_mention_lane"]) for row in rows if row["first_product_mention_lane"])
    if len(primary_product_lane_counts) < min(5, len(trends), count):
        warnings.append("Product mention guidance did not vary enough across trends.")
    primary_product_placement_counts = Counter(
        str(row["primary_product_mention_placement_target"])
        for row in rows
        if row["primary_product_mention_placement_target"]
    )
    if len(primary_product_placement_counts) < min(4, len(trends), count):
        warnings.append("Product mention placement guidance did not vary enough across trends.")
    primary_product_syntax_counts = Counter(
        str(row["primary_product_mention_syntax_target"])
        for row in rows
        if row["primary_product_mention_syntax_target"]
    )
    primary_product_name_usage_counts = Counter(
        str(row["primary_product_name_usage_target"])
        for row in rows
        if row["primary_product_name_usage_target"]
    )
    request_angle_counts = Counter(str(row["request_angle"]) for row in rows if row["request_angle"])
    product_name_optional_guidance_count = sum(
        1
        for row in rows
        if str(row["primary_product_mention_lane"]) in PRODUCT_NAME_OPTIONAL_LANE_NAMES
        or str(row["primary_product_mention_syntax_target"]) == "name omitted"
    )
    if len(primary_product_syntax_counts) < min(4, len(trends), count):
        warnings.append("Product mention syntax guidance did not vary enough across trends.")
    if len(primary_product_name_usage_counts) < min(3, len(trends), count):
        warnings.append("Product name usage guidance did not vary enough across trends.")
    if len(rows) >= 8 and product_name_optional_guidance_count < max(2, len(rows) // 5):
        warnings.append("Product-name-optional guidance did not appear often enough across trends.")
    primary_layout_target_counts = Counter(str(row["primary_layout_target"]) for row in rows if row["primary_layout_target"])
    if len(primary_layout_target_counts) < min(4, len(trends), count):
        warnings.append("Layout target guidance did not vary enough across trends.")
    primary_closing_target_counts = Counter(str(row["primary_closing_target"]) for row in rows if row["primary_closing_target"])
    if len(primary_closing_target_counts) < min(4, len(trends), count):
        warnings.append("Closing target guidance did not vary enough across trends.")
    primary_human_rhythm_target_counts = Counter(
        str(row["primary_human_rhythm_target"]) for row in rows if row["primary_human_rhythm_target"]
    )
    if len(primary_human_rhythm_target_counts) < min(4, len(trends), count):
        warnings.append("Human rhythm guidance did not vary enough across trends.")
    if len(covered_trend_ids) > 1 and len(request_angle_counts) < min(5, len(covered_trend_ids), count):
        warnings.append("Request angles did not vary enough across covered trends.")
    append_plan_concentration_warning(warnings, "Structure guidance", primary_structure_counts, len(rows))
    append_plan_concentration_warning(warnings, "Reader value guidance", primary_editorial_value_counts, len(rows))
    append_plan_concentration_warning(warnings, "Product mention guidance", primary_product_lane_counts, len(rows))
    append_plan_concentration_warning(
        warnings,
        "Product mention placement guidance",
        primary_product_placement_counts,
        len(rows),
    )
    append_plan_concentration_warning(
        warnings,
        "Product mention syntax guidance",
        primary_product_syntax_counts,
        len(rows),
    )
    append_plan_concentration_warning(
        warnings,
        "Product name usage guidance",
        primary_product_name_usage_counts,
        len(rows),
    )
    append_plan_concentration_warning(warnings, "Layout target guidance", primary_layout_target_counts, len(rows))
    append_plan_concentration_warning(warnings, "Closing target guidance", primary_closing_target_counts, len(rows))
    append_plan_concentration_warning(warnings, "Human rhythm guidance", primary_human_rhythm_target_counts, len(rows))
    if len(covered_trend_ids) > 1:
        append_plan_concentration_warning(warnings, "Request angle guidance", request_angle_counts, len(rows))
    if uncovered_trend_count:
        warnings.append(
            f"{uncovered_trend_count} loaded trend(s) were not covered; raise --count or lower --trend-limit for full coverage."
        )
    mention_pattern_counts = Counter(
        str(row["leadpoet_mention_pattern"])
        for row in rows
        if row["leadpoet_mention_pattern"] and row["leadpoet_mention_pattern"] != "no_leadpoet"
    )
    if mention_pattern_counts and mention_pattern_counts.most_common(1)[0][1] >= max(3, (len(rows) + 1) // 2):
        pattern, pattern_count = mention_pattern_counts.most_common(1)[0]
        warnings.append(f"Leadpoet mention phrasing is too concentrated: {pattern} appears {pattern_count}/{len(rows)} times.")
    actual_syntax_counts = Counter(
        str(row["leadpoet_actual_syntax"])
        for row in rows
        if row["leadpoet_actual_syntax"] and row["leadpoet_actual_syntax"] != "no_leadpoet"
    )
    if actual_syntax_counts and len(actual_syntax_counts) < min(3, sum(actual_syntax_counts.values())):
        warnings.append("Leadpoet mention syntax is not varied enough.")
    elif actual_syntax_counts and actual_syntax_counts.most_common(1)[0][1] >= max(3, (sum(actual_syntax_counts.values()) + 1) // 2):
        syntax, syntax_count = actual_syntax_counts.most_common(1)[0]
        warnings.append(f"Leadpoet mention syntax is too concentrated: {syntax} appears {syntax_count}/{sum(actual_syntax_counts.values())} times.")
    leadpoet_grammar_fingerprint_counts = Counter(
        str(row["leadpoet_grammar_fingerprint"])
        for row in rows
        if row["leadpoet_grammar_fingerprint"] and row["leadpoet_grammar_fingerprint"] != "no_leadpoet"
    )
    if leadpoet_grammar_fingerprint_counts and len(leadpoet_grammar_fingerprint_counts) < min(
        3, sum(leadpoet_grammar_fingerprint_counts.values())
    ):
        warnings.append("Leadpoet grammar fingerprints are not varied enough.")
    elif leadpoet_grammar_fingerprint_counts and leadpoet_grammar_fingerprint_counts.most_common(1)[0][1] >= max(
        3, (sum(leadpoet_grammar_fingerprint_counts.values()) + 1) // 2
    ):
        grammar, grammar_count = leadpoet_grammar_fingerprint_counts.most_common(1)[0]
        warnings.append(
            f"Leadpoet grammar fingerprints are too concentrated: {grammar} appears {grammar_count}/{sum(leadpoet_grammar_fingerprint_counts.values())} times."
        )
    leadpoet_phrase_pattern_counts = Counter(
        str(row["leadpoet_phrase_pattern"])
        for row in rows
        if row["leadpoet_phrase_pattern"] and row["leadpoet_phrase_pattern"] != "no_leadpoet"
    )
    if leadpoet_phrase_pattern_counts and len(leadpoet_phrase_pattern_counts) < min(
        3, sum(leadpoet_phrase_pattern_counts.values())
    ):
        warnings.append("Leadpoet phrase patterns are not varied enough.")
    elif leadpoet_phrase_pattern_counts and leadpoet_phrase_pattern_counts.most_common(1)[0][1] >= max(
        3, (sum(leadpoet_phrase_pattern_counts.values()) + 1) // 2
    ):
        phrase_pattern, phrase_pattern_count = leadpoet_phrase_pattern_counts.most_common(1)[0]
        warnings.append(
            f"Leadpoet phrase patterns are too concentrated: {phrase_pattern} appears {phrase_pattern_count}/{sum(leadpoet_phrase_pattern_counts.values())} times."
        )
    leadpoet_sentence_template_counts = Counter(
        str(row["leadpoet_sentence_template"])
        for row in rows
        if row["leadpoet_sentence_template"] and row["leadpoet_sentence_template"] != "no_leadpoet"
    )
    if leadpoet_sentence_template_counts and leadpoet_sentence_template_counts.most_common(1)[0][1] >= max(
        3, (sum(leadpoet_sentence_template_counts.values()) + 1) // 2
    ):
        template, template_count = leadpoet_sentence_template_counts.most_common(1)[0]
        warnings.append(
            f"Leadpoet sentence templates are too concentrated: {template} appears {template_count}/{sum(leadpoet_sentence_template_counts.values())} times."
        )
    leadpoet_position_counts = Counter(
        str(row["leadpoet_paragraph_position"])
        for row in rows
        if row["leadpoet_paragraph_position"] and row["leadpoet_paragraph_position"] != "none"
    )
    leadpoet_mentioned_count = sum(leadpoet_position_counts.values())
    if leadpoet_position_counts and len(leadpoet_position_counts) < min(3, leadpoet_mentioned_count):
        warnings.append("Leadpoet paragraph placement is not varied enough.")
    elif leadpoet_position_counts and leadpoet_position_counts.most_common(1)[0][1] >= max(
        3, (leadpoet_mentioned_count + 1) // 2
    ):
        position, position_count = leadpoet_position_counts.most_common(1)[0]
        warnings.append(f"Leadpoet paragraph placement is too concentrated: {position} appears {position_count}/{leadpoet_mentioned_count} times.")
    leadpoet_final_line_mentions = sum(1 for row in rows if row["leadpoet_in_final_line"])
    if leadpoet_mentioned_count and leadpoet_final_line_mentions >= max(3, (leadpoet_mentioned_count + 1) // 2):
        warnings.append(
            f"Leadpoet appears in the final line too often: {leadpoet_final_line_mentions}/{leadpoet_mentioned_count} mention(s)."
        )
    leadpoet_sentence_position_counts = Counter(
        str(row["leadpoet_sentence_position"])
        for row in rows
        if row["leadpoet_sentence_position"] and row["leadpoet_sentence_position"] != "none"
    )
    leadpoet_sentence_mentions = sum(leadpoet_sentence_position_counts.values())
    if leadpoet_sentence_position_counts and len(leadpoet_sentence_position_counts) < min(3, leadpoet_sentence_mentions):
        warnings.append("Leadpoet sentence placement is not varied enough.")
    elif leadpoet_sentence_position_counts and leadpoet_sentence_position_counts.most_common(1)[0][1] > max(
        3, leadpoet_sentence_mentions // 2
    ):
        sentence_position, sentence_position_count = leadpoet_sentence_position_counts.most_common(1)[0]
        warnings.append(
            f"Leadpoet sentence placement is too concentrated: {sentence_position} appears {sentence_position_count}/{leadpoet_sentence_mentions} times."
        )
    leadpoet_sentence_start_mentions = sum(1 for row in rows if row["leadpoet_starts_sentence"])
    if leadpoet_sentence_mentions and leadpoet_sentence_start_mentions > max(3, leadpoet_sentence_mentions // 2):
        warnings.append(
            f"Leadpoet starts the product sentence too often: {leadpoet_sentence_start_mentions}/{leadpoet_sentence_mentions} mention(s)."
        )
    layout_counts = Counter(str(row["layout_fingerprint"]) for row in rows if row["layout_fingerprint"])
    if len(layout_counts) < min(4, len(rows)):
        warnings.append("Generated post layouts are not varied enough.")
    elif layout_counts and layout_counts.most_common(1)[0][1] >= max(3, (len(rows) + 1) // 2):
        layout, layout_count = layout_counts.most_common(1)[0]
        warnings.append(f"Generated post layouts are too concentrated: {layout} appears {layout_count}/{len(rows)} times.")
    repeated_phrase_counts = repeated_batch_phrase_counts([str(row["text"]) for row in rows])
    if repeated_phrase_counts:
        phrase, phrase_count = next(iter(repeated_phrase_counts.items()))
        warnings.append(f"Repeated stock phrase appeared across generated posts: '{phrase}' appears {phrase_count}/{len(rows)} times.")
    return {
        "trend_count": len(trends),
        "post_count": len(rows),
        "requested_count": requested_count,
        "cover_all_trends": cover_all_trends,
        "covered_trend_count": len(covered_trend_ids),
        "uncovered_trend_count": uncovered_trend_count,
        "loaded_all_matching_trends": trend_limit <= 0,
        "trend_limit": trend_limit,
        "leadpoet_shapes": dict(Counter(str(row["leadpoet_shape"]) for row in rows)),
        "bad_phrase_counts": bad_phrase_counts,
        "unique_first_lines": unique_first_lines,
        "unique_last_lines": unique_last_lines,
        "primary_structure_option_counts": dict(primary_structure_counts),
        "primary_editorial_value_mode_counts": dict(primary_editorial_value_counts),
        "primary_product_mention_lane_counts": dict(primary_product_lane_counts),
        "primary_product_mention_placement_target_counts": dict(primary_product_placement_counts),
        "primary_product_mention_syntax_target_counts": dict(primary_product_syntax_counts),
        "primary_product_name_usage_target_counts": dict(primary_product_name_usage_counts),
        "product_name_optional_guidance_count": product_name_optional_guidance_count,
        "request_angle_counts": dict(request_angle_counts),
        "primary_layout_target_counts": dict(primary_layout_target_counts),
        "primary_closing_target_counts": dict(primary_closing_target_counts),
        "primary_human_rhythm_target_counts": dict(primary_human_rhythm_target_counts),
        "first_structure_option_counts": dict(first_structure_counts or primary_structure_counts),
        "first_product_mention_lane_counts": dict(first_product_lane_counts or primary_product_lane_counts),
        "leadpoet_mention_pattern_counts": dict(mention_pattern_counts),
        "leadpoet_actual_syntax_counts": dict(actual_syntax_counts),
        "leadpoet_grammar_fingerprint_counts": dict(leadpoet_grammar_fingerprint_counts),
        "leadpoet_phrase_pattern_counts": dict(leadpoet_phrase_pattern_counts),
        "leadpoet_sentence_template_counts": dict(leadpoet_sentence_template_counts),
        "leadpoet_paragraph_position_counts": dict(leadpoet_position_counts),
        "leadpoet_sentence_position_counts": dict(leadpoet_sentence_position_counts),
        "leadpoet_final_line_mentions": leadpoet_final_line_mentions,
        "leadpoet_sentence_start_mentions": leadpoet_sentence_start_mentions,
        "layout_fingerprint_counts": dict(layout_counts),
        "repeated_phrase_counts": repeated_phrase_counts,
        "warnings": warnings,
        "rows": rows,
    }


def post_layout_fingerprint(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", text.strip()) if paragraph.strip()]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    paragraph_bucket = bucket_count(len(paragraphs), "p")
    line_bucket = bucket_count(len(lines), "l")
    opener = lines[0] if lines else ""
    return f"{paragraph_bucket}|{line_bucket}|{opener_shape(opener)}|leadpoet_{leadpoet_paragraph_position(paragraphs)}"


def leadpoet_text_position(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", text.strip()) if paragraph.strip()]
    return leadpoet_paragraph_position(paragraphs)


def leadpoet_sentence_position(text: str) -> str:
    sentences = text_sentences(text)
    leadpoet_indexes = [
        index for index, sentence in enumerate(sentences)
        if re.search(r"\bleadpoet\b", sentence, flags=re.I)
    ]
    if not leadpoet_indexes:
        return "none"
    if len(sentences) == 1:
        return "single_sentence"
    first_index = leadpoet_indexes[0]
    if first_index == 0:
        return "first_sentence"
    if first_index == len(sentences) - 1:
        return "last_sentence"
    return "middle_sentence"


def leadpoet_starts_sentence(text: str) -> bool:
    for sentence in text_sentences(text):
        cleaned = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", sentence.strip())
        if re.match(r"leadpoet\b", cleaned, flags=re.I):
            return True
    return False


def leadpoet_actual_syntax(text: str) -> str:
    if not re.search(r"\bleadpoet\b", text, flags=re.I):
        return "no_leadpoet"
    sentences = text_sentences(text)
    leadpoet_sentence = next(
        (sentence for sentence in sentences if re.search(r"\bleadpoet\b", sentence, flags=re.I)),
        text,
    )
    cleaned = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", leadpoet_sentence.strip())
    lowered = cleaned.lower().replace("’", "'")
    if re.match(r"leadpoet\s+(?:is|owns?|can|makes?|starts?|keeps?|turns?|focuses|helps?|gives?|answers?|shows?|lets?|finds?|enables?|provides?|delivers?|powers?|unlocks?|brings?|drives?|creates?|needs?|should|has\s+to|belongs|must|only\s+matters)\b", lowered):
        return "product_subject_verb"
    if re.search(r"\b(?:put|place|keep|carry|bring|move|tuck|embed|mention|name|use)\b[^.!?]{0,80}\bleadpoet\b", lowered):
        return "product_as_object"
    if re.search(r"\b(?:before|after)\s+leadpoet\s+\w+\b", lowered):
        return "clause_embedded"
    if re.search(r"\b(?:inside|in|into|through|within|before|after|around|near|next\s+to)\s+leadpoet\b", lowered):
        return "prepositional_tuck_in"
    if re.search(r"\bleadpoet's\s+\w+|\bleadpoet\s+(?:signal|line|mention|lane|edge|question|constraint|part|work|side|wedge)\b", lowered):
        return "product_modifier"
    if re.search(r"\bleadpoet\b[^.!?]{0,80}\b(?:before|after|when|where|if|because|as)\b", lowered):
        return "clause_embedded"
    if re.match(r"(?:the|a|that|this)\b.*\bleadpoet\b", lowered):
        return "contextual_noun_phrase"
    if re.search(r"(?m)^\s*leadpoet\b", cleaned, flags=re.I):
        return "product_line_start"
    return "contextual"


def leadpoet_grammar_fingerprint_for_text(text: str) -> str:
    sentence = next(
        (sentence for sentence in text_sentences(text) if re.search(r"\bleadpoet\b", sentence, flags=re.I)),
        "",
    )
    return leadpoet_sentence_grammar_fingerprint(sentence) or "no_leadpoet"


def leadpoet_phrase_pattern_for_text(text: str) -> str:
    sentence = first_leadpoet_sentence(text)
    return leadpoet_phrase_pattern_fingerprint(sentence) or "no_leadpoet"


def leadpoet_sentence_template(text: str) -> str:
    sentence = first_leadpoet_sentence(text)
    return leadpoet_sentence_template_fingerprint(sentence) or "no_leadpoet"


def first_leadpoet_sentence(text: str) -> str:
    return next(
        (sentence for sentence in text_sentences(text) if re.search(r"\bleadpoet\b", sentence, flags=re.I)),
        "",
    )


def text_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if part.strip()]


def bucket_count(count: int, prefix: str) -> str:
    if count >= 4:
        return f"{prefix}4plus"
    return f"{prefix}{count}"


def opener_shape(line: str) -> str:
    stripped = line.strip()
    lowered = stripped.lower()
    if not stripped:
        return "empty"
    if re.match(r"(?:[-*•]\s+|\d+[.)]\s+)", stripped):
        return "list"
    if stripped.endswith("?"):
        return "question"
    if ":" in stripped:
        return "colon"
    if re.search(r"\b(?:not|instead|but|versus|vs\.?|beats|before|after)\b", lowered):
        return "contrast"
    if len(stripped) <= 55:
        return "short_claim"
    return "long_claim"


def leadpoet_paragraph_position(paragraphs: list[str]) -> str:
    leadpoet_indexes = [
        index for index, paragraph in enumerate(paragraphs)
        if re.search(r"\bleadpoet\b", paragraph, flags=re.I)
    ]
    if not leadpoet_indexes:
        return "none"
    if len(paragraphs) == 1:
        return "single"
    first_index = leadpoet_indexes[0]
    if first_index == 0:
        return "first"
    if first_index == len(paragraphs) - 1:
        return "last"
    return "middle"


def leadpoet_mention_shape(text: str) -> str:
    lowered = text.lower()
    if "leadpoet is building" in lowered:
        return "leadpoet_is_building"
    if "leadpoet is built" in lowered:
        return "leadpoet_is_built"
    if "leadpoet is" in lowered:
        return "leadpoet_is"
    if "leadpoet" not in lowered:
        return "no_leadpoet"
    if re.search(r"(?m)^\s*leadpoet\b", text, flags=re.I):
        return "leadpoet_paragraph_start"
    return "leadpoet_embedded"


def leadpoet_mention_pattern(text: str) -> str:
    if not re.search(r"\bleadpoet\b", text, flags=re.I):
        return "no_leadpoet"
    if re.search(r"\bleadpoet\s+is\s+building\b", text, flags=re.I):
        return "leadpoet_is_building"
    if re.search(r"\bleadpoet\s+is\b", text, flags=re.I):
        return "leadpoet_is"
    if re.search(r"\bfor\s+leadpoet\b", text, flags=re.I):
        return "for_leadpoet"
    if re.search(r"\b(?:that|this)\s+is\s+where\s+leadpoet\b|\bthat['’]?s\s+where\s+leadpoet\b", text, flags=re.I):
        return "that_is_where_leadpoet"
    if re.search(r"\bthat\s+is\s+the\s+leadpoet\s+(?:job|bar|point|constraint|product belief)\b", text, flags=re.I):
        return "that_is_the_leadpoet_closer"
    if re.search(r"\bthe\s+leadpoet\s+(?:product belief|signal|point|constraint|job|bar|part)\s+is\b", text, flags=re.I):
        return "the_leadpoet_noun_is"
    if re.search(r"\bleadpoet\s+belongs\b", text, flags=re.I):
        return "leadpoet_belongs"
    if re.search(r"\bleadpoet\s+only\s+matters\b", text, flags=re.I):
        return "leadpoet_only_matters"
    if re.search(r"\bleadpoet\s+must\b", text, flags=re.I):
        return "leadpoet_must"
    if re.search(r"\bleadpoet\s+question\b", text, flags=re.I):
        return "leadpoet_question"
    if re.search(r"\bleadpoet\s+handoff\b|\bhandoff\s+into\s+leadpoet\b", text, flags=re.I):
        return "leadpoet_handoff"
    if re.search(r"\bleadpoet\s+pressure\s+test\b|\bpressure\s+test\s+in\s+leadpoet\b", text, flags=re.I):
        return "leadpoet_pressure_test"
    if re.search(r"\bleadpoet\s+comes\s+after\b|\bdestination\s+can\s+be\s+leadpoet\b", text, flags=re.I):
        return "leadpoet_destination"
    if re.search(r"\bbelief\s+stays\s+practical\s+in\s+leadpoet\b", text, flags=re.I):
        return "in_leadpoet_belief"
    if re.search(r"\bnext\s+account\s+choice\s+in\s+leadpoet\b", text, flags=re.I):
        return "leadpoet_consequence"
    if re.search(r"\b(?:specific|concrete)\s+in\s+leadpoet\b", text, flags=re.I):
        return "specific_in_leadpoet"
    if re.search(r"\bleadpoet\s+lane\b|\bleadpoet's\s+lane\b|\bleadpoet’s\s+lane\b", text, flags=re.I):
        return "leadpoet_lane"
    if re.search(r"\bleadpoet\s+signal\b|\bleadpoet's\s+signal\b|\bleadpoet’s\s+signal\b", text, flags=re.I):
        return "leadpoet_signal"
    if re.search(r"\bleadpoet\s+constraint\b|\bleadpoet's\s+constraint\b|\bleadpoet’s\s+constraint\b", text, flags=re.I):
        return "leadpoet_constraint"
    if re.search(r"\bleadpoet\s+has\s+to\b", text, flags=re.I):
        return "leadpoet_has_to"
    if re.search(r"\bleadpoet\s+should\b", text, flags=re.I):
        return "leadpoet_should"
    if re.search(r"\bleadpoet\s+needs?\b", text, flags=re.I):
        return "leadpoet_needs"
    if re.search(r"\bleadpoet\s+owns?\b", text, flags=re.I):
        return "leadpoet_owns"
    if re.search(r"\bleadpoet\s+starts?\b", text, flags=re.I):
        return "leadpoet_starts"
    if re.search(r"\bleadpoet\s+can\b", text, flags=re.I):
        return "leadpoet_can"
    if re.search(r"\bleadpoet\s+focuses\b", text, flags=re.I):
        return "leadpoet_focuses"
    if re.search(r"\bleadpoet\s+helps?\b", text, flags=re.I):
        return "leadpoet_helps"
    if re.search(r"\bleadpoet\s+gives?\b", text, flags=re.I):
        return "leadpoet_gives"
    if re.search(r"\bleadpoet\s+answers?\b", text, flags=re.I):
        return "leadpoet_answers"
    if re.search(r"\bleadpoet\s+shows?\b", text, flags=re.I):
        return "leadpoet_shows"
    if re.search(r"\bleadpoet\s+lets?\b", text, flags=re.I):
        return "leadpoet_lets"
    if re.search(r"\bleadpoet\s+finds?\b", text, flags=re.I):
        return "leadpoet_finds"
    if re.search(r"\bleadpoet\s+enables?\b", text, flags=re.I):
        return "leadpoet_enables"
    if re.search(r"\bleadpoet\s+provides?\b", text, flags=re.I):
        return "leadpoet_provides"
    if re.search(r"\bleadpoet\s+delivers?\b", text, flags=re.I):
        return "leadpoet_delivers"
    if re.search(r"\bleadpoet\s+powers?\b", text, flags=re.I):
        return "leadpoet_powers"
    if re.search(r"\bleadpoet\s+unlocks?\b", text, flags=re.I):
        return "leadpoet_unlocks"
    if re.search(r"\bleadpoet\s+brings?\b", text, flags=re.I):
        return "leadpoet_brings"
    if re.search(r"\bleadpoet\s+drives?\b", text, flags=re.I):
        return "leadpoet_drives"
    if re.search(r"\bleadpoet\s+creates?\b", text, flags=re.I):
        return "leadpoet_creates"
    if re.search(r"\binside\s+leadpoet\b", text, flags=re.I):
        return "inside_leadpoet"
    if re.search(r"\bwhere\s+leadpoet\s+enters\b", text, flags=re.I):
        return "where_leadpoet_enters"
    if re.search(r"\bbefore\s+leadpoet\s+earns\b", text, flags=re.I):
        return "before_leadpoet_earns"
    if re.search(r"\bleadpoet\s+earns\b", text, flags=re.I):
        return "leadpoet_earns"
    if re.search(r"\bwhen\s+(?:a\s+rep|they)\s+opens?\s+leadpoet\b", text, flags=re.I):
        return "rep_opens_leadpoet"
    if re.search(r"\bclear\s+enough\s+in\s+leadpoet\b", text, flags=re.I):
        return "clear_enough_in_leadpoet"
    if re.search(r"\bin\s+leadpoet,\s+the\s+account\s+reason\b", text, flags=re.I):
        return "in_leadpoet_account_reason"
    if re.search(r"\bin\s+leadpoet,\s+that\s+belief\b", text, flags=re.I):
        return "in_leadpoet_belief"
    if re.search(r"\bin\s+leadpoet,\b", text, flags=re.I):
        return "in_leadpoet"
    if re.search(r"\b(?:put|place|keep|carry|bring|move|tuck|embed|mention|name|use)\b[^.!?]{0,80}\bleadpoet\b", text, flags=re.I):
        return "leadpoet_as_object"
    if re.search(r"\breaches\s+leadpoet\b|\bleadpoet\b[^.!?]{0,80}\b(?:before|after|when|where|if|because|as)\b", text, flags=re.I):
        return "leadpoet_clause_embedded"
    if re.search(r"(?m)^\s*leadpoet\b", text, flags=re.I):
        return "leadpoet_starts_line"
    if re.search(r"\bleadpoet(?:'s|’s)\s+\w+", text, flags=re.I):
        return "leadpoet_possessive"
    return "leadpoet_contextual"


def leadpoet_bad_phrase_counts(text: str) -> dict[str, int]:
    return {
        key: len(re.findall(pattern, text, flags=re.I))
        for key, pattern in LEADPOET_AUDIT_BAD_PATTERNS
    }


def repeated_batch_phrase_counts(texts: list[str], *, phrase_size: int = 4) -> dict[str, int]:
    if len(texts) < 4:
        return {}
    threshold = min(len(texts), max(4, (len(texts) + 2) // 3))
    counts: Counter[str] = Counter()
    for text in texts:
        tokens = repeated_phrase_tokens(text)
        seen = {
            " ".join(tokens[index:index + phrase_size])
            for index in range(0, max(0, len(tokens) - phrase_size + 1))
        }
        for phrase in seen:
            if repeated_phrase_is_meaningful(phrase):
                counts[phrase] += 1
    repeated = sorted(
        ((phrase, count) for phrase, count in counts.items() if count >= threshold),
        key=lambda item: (-item[1], item[0]),
    )
    return dict(repeated[:10])


def repeated_phrase_tokens(text: str) -> list[str]:
    normalized = text.lower().replace("’", "'")
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", normalized)


def repeated_phrase_is_meaningful(phrase: str) -> bool:
    tokens = phrase.split()
    if len(tokens) < 4:
        return False
    content_tokens = [token for token in tokens if token not in REPEATED_PHRASE_STOPWORDS]
    return len(set(content_tokens)) >= 2


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def leadpoet_in_closing_text_unit(text: str) -> bool:
    closing_line = last_nonempty_line(text)
    if not closing_line:
        return False
    sentences = text_sentences(closing_line)
    closing_unit = sentences[-1] if sentences else closing_line
    return bool(re.search(r"\bleadpoet\b", closing_unit, flags=re.I))


def cmd_plan_week_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit(MISSING_PROVIDER_MESSAGE)
    generator = SocialBrainGenerator(store, provider=provider)
    try:
        result = plan_week_x(
            store,
            generator,
            week_start=args.week_start,
            posts=args.posts,
            use_trends=args.use_trends,
            trend_window_days=args.trend_window_days,
            topic_mix=args.topic_mix,
            cadence=args.cadence,
            default_post_type=args.default_post_type,
            variants_per_slot=1,
            allow_evergreen=args.allow_evergreen,
            use_patterns=args.use_patterns,
            length_mode=args.length,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Planned {len(result.queued)} X post(s)")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    for item in result.queued:
        trend_titles = [
            trend.title for trend_id in item.trend_ids
            if (trend := store.get_trend_item(trend_id)) is not None
        ]
        print(f"\n{item.scheduled_at} | {item.topic} | {item.post_type} | id {item.id}")
        print(item.text)
        print(f"Trend/source used: {', '.join(trend_titles) if trend_titles else 'none'}")
        print(item.reviewer_notes or "Why worth publishing: needs review.")
    return 0


def cmd_list_content_queue(store: SQLiteStore, args: argparse.Namespace) -> int:
    items = store.list_content_queue(status=args.status, platform="x", limit=args.limit)
    if not items:
        print("No queued posts found.")
        return 0
    for item in items:
        print(
            f"{item.id} | {item.status} | {item.scheduled_at} | "
            f"{item.topic} | {item.post_type}"
        )
        print(f"  {item.text}")
        if item.reviewer_notes:
            print(f"  Notes: {item.reviewer_notes}")
        if item.x_url:
            print(f"  Posted: {item.x_url}")
        if item.failure_reason:
            print(f"  Failure: {item.failure_reason}")
    return 0


def cmd_update_queued_post(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        item = require_queue_item(store, args.queue_id)
        if args.post_type:
            get_x_adapter().validate_post_type(args.post_type)
        new_text = args.text if args.text is not None else item.text
        new_topic = args.topic if args.topic is not None else item.topic
        new_post_type = args.post_type if args.post_type is not None else item.post_type
        new_angle = args.angle if args.angle is not None else item.angle
        status, approved_at, _ = resolve_queue_edit_state(
            item,
            text=new_text,
            topic=new_topic,
            post_type=new_post_type,
            angle=new_angle,
            requested_status=args.status or item.status,
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
        updated = store.save_content_queue_item(
            ContentQueueItem(
                id=item.id,
                platform=item.platform,
                draft_id=item.draft_id,
                topic_idea_id=item.topic_idea_id,
                text=new_text,
                topic=new_topic,
                post_type=new_post_type,
                angle=new_angle,
                trend_ids=item.trend_ids,
                scheduled_at=args.scheduled_at if args.scheduled_at is not None else item.scheduled_at,
                status=status,
                reviewer_notes=args.notes if args.notes is not None else item.reviewer_notes,
                created_at=item.created_at,
                approved_at=approved_at,
                posted_at=item.posted_at,
                x_post_id=item.x_post_id,
                x_url=item.x_url,
                failure_reason=item.failure_reason,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Updated queued post {updated.id}")
    print(f"Status: {updated.status}")
    print(f"Scheduled at: {updated.scheduled_at}")
    print(f"Topic: {updated.topic}")
    print(f"Post type: {updated.post_type}")
    print(updated.text)
    return 0


def cmd_revise_queued_post(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit(MISSING_PROVIDER_MESSAGE)
    try:
        versions = revise_queued_post(
            store,
            provider,
            queue_id=args.queue_id,
            instruction=args.instruction,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Generated {len(versions)} revision option(s) for {args.queue_id}")
    for version in versions:
        print(f"\nRevision {version.id}")
        print(version.text)
    return 0


def cmd_approve_queued_post(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        item = approve_queue_item(store, args.queue_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Approved queued post {item.id} for {item.scheduled_at}")
    return 0


def cmd_approve_week_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        items = approve_week_x(store, week_start=args.week_start)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Approved {len(items)} queued post(s) for week starting {args.week_start}")
    for item in items:
        print(f"- {item.id} | {item.scheduled_at} | {item.topic}")
    return 0


def cmd_publish_due_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        client = XWriteClient.from_env()
        results = publish_due_x(store, client, now=args.now, limit=args.limit, author=args.author)
    except ValueError as exc:
        message = str(exc) or MISSING_X_WRITE_CREDENTIALS_MESSAGE
        raise SystemExit(message) from exc

    if not results:
        print("No approved X posts are due.")
        return 0
    print(f"Processed {len(results)} due queued post(s)")
    for result in results:
        if result.posted:
            print(f"- posted {result.queue_item.id}: {result.posted.url}")
        else:
            print(f"- failed {result.queue_item.id}: {result.error}")
    return 0


def cmd_post_now_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        item = require_queue_item(store, args.queue_id)
        client = XWriteClient.from_env()
        result = post_queue_item_x(store, client, item=item, author=args.author)
    except ValueError as exc:
        message = str(exc) or MISSING_X_WRITE_CREDENTIALS_MESSAGE
        raise SystemExit(message) from exc
    if result.posted:
        print(f"Posted queued item {result.queue_item.id}: {result.posted.url}")
        return 0
    print(f"Failed queued item {result.queue_item.id}: {result.error}")
    return 1


def cmd_publish_draft_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        post = publish_x_draft(
            store,
            draft_id=args.draft_id,
            url=args.url,
            date=args.date,
            author=args.author,
            metrics=metrics_from_args(args),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Published draft {args.draft_id} as post {post.id}")
    return 0


def cmd_add_trend(store: SQLiteStore, args: argparse.Namespace) -> int:
    trend = store.save_trend_item(
        TrendItem(
            title=args.title,
            source_url=args.source_url,
            source_type=args.source_type,
            topic=args.topic,
            summary=args.summary,
            why_it_matters=args.why_it_matters
            or "Worth watching because it may affect buyer priorities or market timing.",
            leadpoet_angle=args.leadpoet_angle
            or "Connect the trend to practical sales context and better buyer timing.",
            suggested_hooks=args.suggested_hooks,
            urgency=args.urgency,
            date=args.date,
            status=args.status,
            engagement_notes=args.engagement_notes,
            source_author=args.source_author,
            raw_text=args.raw_text,
        )
    )
    print(f"Saved trend {trend.id}: {trend.title}")
    return 0


def cmd_ingest_trends_batch(store: SQLiteStore, args: argparse.Namespace) -> int:
    result = ingest_trends_batch_csv(store, args.csv_path)
    print("Imported trends batch")
    print(f"Trends imported: {result.imported}")
    print(f"Trends skipped: {result.skipped}")
    if result.errors:
        print("Errors:")
        for error in result.errors[:20]:
            print(f"- {error}")
        if len(result.errors) > 20:
            print(f"- ... {len(result.errors) - 20} more")
    print("Top topics:")
    print_counter(result.topics)
    print("Top source types:")
    print_counter(result.source_types)
    return 0


def cmd_list_trends(store: SQLiteStore, args: argparse.Namespace) -> int:
    trends = store.list_trends(
        topic=args.topic,
        status=args.status,
        urgency=args.urgency,
        limit=args.limit,
    )
    if not trends:
        print("No trends found.")
        return 0
    for trend in trends:
        print(f"{trend.id} | {trend.status} | {trend.urgency} | {trend.source_type} | {trend.topic}")
        print(f"  {trend.title}")
        print(f"  {trend.leadpoet_angle}")
    return 0


def cmd_mark_trend_used(store: SQLiteStore, args: argparse.Namespace) -> int:
    store.mark_trend_status(args.trend_id, "used")
    print(f"Marked trend used: {args.trend_id}")
    return 0


def cmd_add_metrics(store: SQLiteStore, args: argparse.Namespace) -> int:
    if not store.get_post(args.post_id):
        raise SystemExit(f"No post found for id {args.post_id}")
    metrics = metrics_from_args(args)
    if not metrics:
        raise SystemExit("At least one metric is required.")
    saved = store.add_metrics(args.post_id, metrics)
    rate = f"{saved.engagement_rate:.4f}" if saved.engagement_rate is not None else "not available"
    print(f"Saved metrics for {args.post_id}. Engagement rate: {rate}")
    return 0


def cmd_list_posts(store: SQLiteStore, args: argparse.Namespace) -> int:
    posts = store.list_posts(platform=args.platform, status=args.status, limit=args.limit)
    if not posts:
        print("No posts found.")
        return 0
    for post in posts:
        source = f" | source_draft={post.source_draft_id}" if post.source_draft_id else ""
        print(f"{post.id} | {post.platform} | {post.post_type} | {post.topic} | {post.author}{source}")
        print(f"  {post.text}")
    return 0


def cmd_rebuild_style_guide(store: SQLiteStore, args: argparse.Namespace) -> int:
    section = ContextManager(store).rebuild_style_guide_from_examples(limit=args.limit)
    print(f"Rebuilt style guide: {section.name}")
    print(section.content)
    return 0


def cmd_analytics(store: SQLiteStore, args: argparse.Namespace) -> int:
    summary = build_analytics_summary(store)
    print("Hints")
    for hint in summary.hints:
        print(f"- {hint}")
    print("\nBy post type")
    print_rows(summary.by_post_type)
    print("\nBy topic")
    print_rows(summary.by_topic)
    print("\nBy pattern")
    print_rows(summary.by_pattern)
    print("\nBy hook")
    print_rows(summary.by_hook)
    print("\nBy structure")
    print_rows(summary.by_structure)
    print("\nBy tone")
    print_rows(summary.by_tone)
    print("\nBy CTA")
    print_rows(summary.by_cta)
    print("\nBy formatting")
    print_rows(summary.by_formatting)
    print("\nBy trend topic")
    print_rows(summary.by_trend_topic)
    print("\nBy trend source type")
    print_rows(summary.by_trend_source_type)
    print("\nBy trend urgency")
    print_rows(summary.by_trend_urgency)
    print("\nBy used trend")
    print_rows(summary.by_used_trend)
    return 0


def cmd_weekly_report_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    print(weekly_report_text(store, since_days=args.since_days))
    return 0


def cmd_prompt_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    generator = SocialBrainGenerator(store)
    request = DraftRequest(
        platform="x",
        topic=args.topic,
        post_type=args.post_type,
        angle=args.angle,
        audience=args.audience,
        tone=args.tone,
        call_to_action=args.cta,
        max_chars=args.max_chars,
        extra_context=args.extra_context,
        use_trends=getattr(args, "use_trends", False),
        use_patterns=getattr(args, "use_patterns", False),
        pattern_ids=getattr(args, "pattern_ids", []),
    )
    print(generator.build_prompt(request))
    return 0


def metrics_from_args(args: argparse.Namespace) -> EngagementMetrics | None:
    values = [
        args.impressions,
        args.likes,
        args.replies,
        args.reposts,
        args.quotes,
        args.bookmarks,
        args.engagement_rate,
    ]
    if all(value is None for value in values):
        return None
    return EngagementMetrics(
        impressions=args.impressions,
        likes=args.likes,
        replies=args.replies,
        reposts=args.reposts,
        quotes=args.quotes,
        bookmarks=args.bookmarks,
        engagement_rate=args.engagement_rate,
    )


def print_rows(rows) -> None:
    if not rows:
        print("- no data yet")
        return
    for row in rows[:10]:
        rate = f"{row.avg_engagement_rate:.4f}" if row.avg_engagement_rate is not None else "n/a"
        print(
            f"- {row.key}: count={row.count}, avg_rate={rate}, "
            f"avg_impressions={row.avg_impressions:.1f}, "
            f"avg_bookmarks={row.avg_bookmarks:.1f}, avg_replies={row.avg_replies:.1f}, "
            f"avg_reposts={row.avg_reposts:.1f}, avg_engagements={row.avg_total_engagements:.1f}, "
            f"top={row.top_post_id or 'n/a'}"
        )


def print_counter(counter) -> None:
    if not counter:
        print("- no data")
        return
    for key, count in counter.most_common(5):
        print(f"- {key}: {count}")


def pattern_engagement_signal(pattern) -> str:
    if pattern.bookmarks is not None:
        return f"{pattern.bookmarks} bookmarks"
    if pattern.replies is not None:
        return f"{pattern.replies} replies"
    if pattern.reposts is not None:
        return f"{pattern.reposts} reposts"
    if pattern.likes is not None:
        return f"{pattern.likes} likes"
    if pattern.impressions is not None:
        return f"{pattern.impressions} impressions"
    return "no metrics"


def load_configured_provider(store: SQLiteStore):
    return load_provider_from_env(model_override=resolve_model_routing(store).default)


def parse_bool_arg(value: str) -> bool:
    return value.strip().lower() == "true"


def value_or_existing(value, existing):
    return value if value is not None else existing


if __name__ == "__main__":
    raise SystemExit(main())
