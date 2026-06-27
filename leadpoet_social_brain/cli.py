from __future__ import annotations

import argparse
import getpass

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.content_queue import (
    approve_queue_item,
    approve_week_x,
    plan_week_x,
    post_queue_item_x,
    publish_due_x,
    require_queue_item,
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
    SocialPost,
    TrendItem,
    utc_now,
)
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
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
    add_pattern_example_parser.add_argument("--window", default="manual", choices=["manual", "daily", "weekly"])
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
    refresh_pattern_parser.add_argument("--window", default="daily", choices=["daily", "weekly"])
    refresh_pattern_parser.set_defaults(func=cmd_refresh_patterns)

    generate = subparsers.add_parser("generate-x", help="Generate X drafts.")
    generate.add_argument("--topic", required=True)
    generate.add_argument("--post-type", required=True)
    generate.add_argument("--angle")
    generate.add_argument("--audience")
    generate.add_argument("--tone")
    generate.add_argument("--cta")
    generate.add_argument("--extra-context")
    generate.add_argument("--max-chars", type=int)
    generate.add_argument("--use-trends", action="store_true")
    generate.add_argument("--use-patterns", action="store_true")
    generate.add_argument("--pattern-id", action="append", dest="pattern_ids", default=[])
    generate.add_argument("--variants", type=int, default=3)
    generate.add_argument("--best-version", action="store_true")
    generate.set_defaults(func=cmd_generate_x)

    plan_week = subparsers.add_parser("plan-week-x", help="Generate a one-week X content queue.")
    plan_week.add_argument("--week-start", required=True)
    plan_week.add_argument("--posts", type=int, default=7)
    plan_week.add_argument("--use-trends", action="store_true")
    plan_week.add_argument("--use-patterns", action="store_true")
    plan_week.add_argument("--trend-window-days", type=int, default=14)
    plan_week.add_argument("--topic-mix")
    plan_week.add_argument("--cadence", default="daily_mornings")
    plan_week.add_argument("--default-post-type", default="founder_insight")
    plan_week.add_argument("--variants-per-slot", type=int, default=1)
    plan_week.add_argument("--allow-evergreen", action="store_true")
    plan_week.add_argument("--best-version", action="store_true")
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
    revise_queue.add_argument("--variants", type=int, default=3)
    revise_queue.add_argument("--best-version", action="store_true")
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
    user = store.save_web_auth_user(args.username, password)
    print(f"Saved web login for {user.username} in {store.path}")
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


def cmd_generate_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_configured_provider(store)
    if not provider:
        raise SystemExit(MISSING_PROVIDER_MESSAGE)
    generator = SocialBrainGenerator(store, provider=provider)
    print("Generator mode: best version" if args.best_version else "Generator mode: configured LLM provider")
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
        use_trends=args.use_trends,
        use_patterns=args.use_patterns,
        pattern_ids=args.pattern_ids,
    )
    drafts = generator.generate(request, variants=args.variants, best_version=args.best_version)
    for index, draft in enumerate(drafts, start=1):
        print(f"\nDraft {index} ({len(draft.text)} chars, id {draft.id})")
        print(draft.text)
        print(f"Explanation: {draft.explanation}")
    return 0


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
            variants_per_slot=args.variants_per_slot,
            allow_evergreen=args.allow_evergreen,
            best_version=args.best_version,
            use_patterns=args.use_patterns,
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
        status = args.status or item.status
        approved_at = item.approved_at
        if status == "approved" and item.status != "approved":
            approved_at = utc_now()
        updated = store.save_content_queue_item(
            ContentQueueItem(
                id=item.id,
                platform=item.platform,
                draft_id=item.draft_id,
                text=args.text if args.text is not None else item.text,
                topic=args.topic if args.topic is not None else item.topic,
                post_type=args.post_type if args.post_type is not None else item.post_type,
                angle=args.angle if args.angle is not None else item.angle,
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
            variants=args.variants,
            best_version=args.best_version,
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
