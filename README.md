# Leadpoet Social Brain

A clean v0.1 system for storing X examples, maintaining reusable Leadpoet context, generating founder-led X drafts, and learning from post performance.

The system is intentionally local-first and simple:

- SQLite for storage
- Python standard library only
- X is the first platform
- Platform adapters are isolated so LinkedIn can be added later
- Generation keeps context modular and explains its influences
- No agents, background automation, or hidden feedback loops

## Architecture

```text
leadpoet_social_brain/
  core/models.py          Platform-neutral data models
  platforms/base.py       Platform adapter protocol
  platforms/x.py          X-specific limits and validation
  storage/sqlite_store.py SQLite schema, migrations, and repository functions
  ingestion.py            X example ingestion workflow
  context.py              Modular context retrieval and style guide rebuilds
  generation.py           Prompt assembler, provider boundary, fallback generator
  skills.py               Tiny markdown skill loader
  providers.py            Optional provider loader and provider protocol
  analytics.py            Simple performance summaries
  cli.py                  Thin command-line interface
skills/
  global_voice.md         Durable voice instructions
  platforms/x.md          X-specific writing instructions
  post_types/*.md         Post-type behavior
  review_filter.md        Final review rules
```

The clean boundary is:

```text
platform adapter -> ingestion/generation services -> storage/context/analytics
```

Core logic only sees platform-neutral models. X-specific behavior lives in `platforms/x.py`.

## Real Workflow

Create the database and seed editable Leadpoet context:

```bash
python3 -m leadpoet_social_brain.cli init
```

Batch import existing X posts from CSV:

```bash
python3 -m leadpoet_social_brain.cli ingest-x-batch --csv data/x_posts.csv
```

See `examples/x_posts_sample.csv` for a small working import file.

CSV columns:

```text
text,url,date,author,topic,post_type,impressions,likes,replies,reposts,quotes,bookmarks,worked_reason,hook,structure,tone,cta,formatting,pattern,imitate
```

Only `text`, `topic`, and `post_type` are required. Missing optional fields are skipped cleanly. Long X examples are accepted during ingestion; X length limits are enforced during generation and publishing.

Supported X post types are `product_update`, `founder_insight`, `bittensor`, `bittensor_update`, `investor_facing`, `quote_tweet`, `launch`, `technical_explanation`, and `light_funny`. Only some post types have dedicated markdown skill files so far; unsupported skill files are simply skipped.

You can still ingest one X example manually:

```bash
python3 -m leadpoet_social_brain.cli ingest-x \
  --text "Most sales AI is still just mail merge with nicer adjectives. The useful version knows why this buyer cares now." \
  --url "https://x.com/gavin/status/123" \
  --date "2026-06-25" \
  --author "Gavin" \
  --topic "sales LLMs" \
  --post-type "founder_insight" \
  --impressions 12000 \
  --likes 180 \
  --replies 22 \
  --reposts 31 \
  --quotes 8 \
  --bookmarks 45 \
  --worked-reason "Clear enemy, specific point of view, plain language." \
  --hook "enemy_hook" \
  --structure "enemy_then_better_model" \
  --tone "plainspoken" \
  --cta "none" \
  --formatting "single_paragraph" \
  --pattern "enemy_then_better_model" \
  --imitate
```

Add or update modular context:

```bash
python3 -m leadpoet_social_brain.cli add-context \
  --kind "priority" \
  --name "current_company_priority" \
  --content "Make Leadpoet feel concrete: show product progress, customer pain, and why sales LLMs need proprietary context."
```

Generate X drafts:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type "founder_insight" \
  --angle "why context beats generic automation" \
  --variants 3
```

By default this runs in demo/test template fallback mode. For real generation, plug in a provider with the existing interface:

```python
class MyProvider:
    def generate(self, prompt: str, *, variants: int) -> list[str]:
        ...

def create_provider():
    return MyProvider()
```

Then point the CLI at it:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type "founder_insight" \
  --angle "why context beats generic automation"
```

Record post results after publishing:

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/456" \
  --date "2026-06-25" \
  --author "Gavin" \
  --impressions 22000 \
  --likes 390 \
  --replies 41 \
  --reposts 62 \
  --quotes 14 \
  --bookmarks 120
```

Or add a later metrics snapshot to any stored post:

```bash
python3 -m leadpoet_social_brain.cli add-metrics \
  --post-id "<post id>" \
  --impressions 22000 \
  --likes 390 \
  --replies 41 \
  --reposts 62 \
  --quotes 14 \
  --bookmarks 120
```

View feedback:

```bash
python3 -m leadpoet_social_brain.cli analytics
```

Analytics reports performance by post type, topic, generic pattern, hook, structure, tone, CTA, and formatting. Draft-published posts keep `source_draft_id` and copied influence rows, so later metrics can be analyzed against the context and patterns used to generate them.

## Generation Model

The v0.1 generator has three inputs:

1. Markdown skill files for durable writing instructions.
2. Retrieved DB context: examples, patterns, Leadpoet context, and analytics hints.
3. A provider boundary for real generation, with a deterministic demo/test fallback.

Edit the markdown files in `skills/` to adjust voice, platform behavior, post-type behavior, or the final review filter without changing code. Generated drafts store the skill file paths and content hashes in their structured context snapshot.

The provider protocol and environment loader are in `providers.py`; `generation.py` only assembles the prompt and calls the provider. Plugging in an LLM should only require passing an object with:

```python
generate(prompt: str, *, variants: int) -> list[str]
```

## Data Model

The SQLite schema stores:

- `posts`: text, URL, date, author, topic, post type, platform, status, optional `source_draft_id`
- `post_metrics`: impressions, likes, replies, reposts, quotes, bookmarks, engagement rate snapshots
- `post_notes`: manual notes on why the post worked, hook, structure, tone, CTA, formatting, pattern, and imitation flag
- `post_influences`: category-aware links from posts to patterns, draft examples, context sections, analytics hints, and source drafts
- `patterns`: reusable hooks, structures, tones, pacing notes, topics, CTAs, formatting
- `context_sections`: modular style, positioning, product, audience, Bittensor, sales LLM vision, priorities
- `generated_drafts`: draft text, request, explanation, influence labels, and structured context snapshot with selected examples, patterns, context sections, analytics hints, skill file paths, and skill content hashes

## Design Notes

- Keep examples and context separate. Examples teach taste. Context teaches what matters now.
- Keep feedback simple. Engagement snapshots produce transparent summaries by post type, topic, hook, structure, tone, CTA, formatting, and pattern.
- Keep prompts lean. Context retrieval selects relevant sections, high-performing examples, and active patterns instead of dumping everything into every generation.
- Keep platform behavior isolated. LinkedIn can be added with a new adapter plus CLI command without rewriting storage or generation.
