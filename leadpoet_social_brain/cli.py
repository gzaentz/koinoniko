from __future__ import annotations

import argparse
from pathlib import Path

from leadpoet_social_brain.analytics import build_analytics_summary
from leadpoet_social_brain.context import ContextManager
from leadpoet_social_brain.core.models import ContextSection, DraftRequest, EngagementMetrics, Pattern, TrendItem
from leadpoet_social_brain.generation import MISSING_PROVIDER_MESSAGE, SocialBrainGenerator
from leadpoet_social_brain.ingestion import ingest_trends_batch_csv, ingest_x_batch_csv, ingest_x_post, publish_x_draft
from leadpoet_social_brain.providers import load_provider_from_env
from leadpoet_social_brain.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore


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
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database.")
    subparsers = parser.add_subparsers(required=True)

    init_parser = subparsers.add_parser("init", help="Initialize DB and seed editable defaults.")
    init_parser.set_defaults(func=cmd_init)

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
    generate.add_argument("--variants", type=int, default=3)
    generate.set_defaults(func=cmd_generate_x)

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
    print(f"Initialized Social Brain DB at {Path(args.db)}")
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


def cmd_generate_x(store: SQLiteStore, args: argparse.Namespace) -> int:
    provider = load_provider_from_env()
    if not provider:
        raise SystemExit(MISSING_PROVIDER_MESSAGE)
    generator = SocialBrainGenerator(store, provider=provider)
    print("Generator mode: configured LLM provider")
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
    )
    drafts = generator.generate(request, variants=args.variants)
    for index, draft in enumerate(drafts, start=1):
        print(f"\nDraft {index} ({len(draft.text)} chars, id {draft.id})")
        print(draft.text)
        print(f"Explanation: {draft.explanation}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
