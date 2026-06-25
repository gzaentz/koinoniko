# Leadpoet Social Brain

Local-first Social Brain v0.2 for Leadpoet.

The goal is simple: store strong X examples, preserve Leadpoet context, optionally bring in timely market trends, and generate founder-led X drafts that sound like Gavin personally wrote them.

The system is intentionally small:

- SQLite storage
- Python standard library only
- X first
- Platform adapters isolated so LinkedIn can be added later
- Markdown skill files for editable writing instructions
- Transparent analytics from stored post performance
- No agents, scraping jobs, background automation, or hidden orchestration

## Current Status

v0.2 is ready for local use with real X examples.

What it does well:

- Imports X examples one-by-one or from CSV.
- Stores post text, URL, date, author, topic, post type, notes, metrics, and optional pattern labels.
- Rebuilds an observed writing guide from high-performing `imitate=true` examples.
- Builds generation prompts from exact examples, human "why it worked" notes, Leadpoet context, skill files, analytics hints, and optional trends.
- Preserves draft-to-post linkage through `source_draft_id`.
- Tracks the examples, context sections, patterns, skill files, analytics hints, and trends used for each draft.
- Lets you add metrics after publishing and analyze what is working.
- Requires a configured LLM provider for draft generation.

Provider requirement:

- `generate-x` requires `SOCIAL_BRAIN_PROVIDER=module.path:create_provider`.
- If no provider is configured, generation fails clearly and does not create drafts.

## Project Layout

```text
leadpoet_social_brain/
  core/models.py          Platform-neutral data models
  platforms/base.py       Platform adapter protocol
  platforms/x.py          X post types, URL validation, and character limits
  platforms/x_api.py      Minimal X API client and user-post sync importer
  storage/sqlite_store.py SQLite schema, migrations, and repository functions
  ingestion.py            X example import, trend import, publish workflow
  context.py              Context retrieval and style guide rebuilds
  generation.py           Prompt assembly, provider call, draft validation
  skills.py               Tiny markdown skill loader
  providers.py            Optional provider protocol and env loader
  analytics.py            Transparent performance summaries
  cli.py                  Command-line interface
skills/
  global_voice.md
  platforms/x.md
  post_types/founder_insight.md
  post_types/product_update.md
  post_types/bittensor_update.md
  post_types/quote_tweet.md
  review_filter.md
examples/
  x_posts_sample.csv
  trends_sample.csv
tests/
  test_social_brain.py
```

Core logic works with platform-neutral models. X-specific behavior lives in `platforms/x.py`. LinkedIn should be additive: add a platform adapter, platform skill file, and CLI commands without rewriting storage, context, generation, or analytics.

## Install And Run

This repo currently has no third-party runtime dependencies.

Run commands from the repo root:

```bash
cd /Users/gavin/Code/koinonikos
python3 -m leadpoet_social_brain.cli --help
```

The default database path is:

```text
data/social_brain.sqlite3
```

Use `--db` when you want a separate test or demo database:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 init
```

Use a fresh database path when you want clean demo output.

## Quick Sample Flow

Run the sample context flow without touching the default database:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 init

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 ingest-x-batch \
  --csv examples/x_posts_sample.csv

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 rebuild-style-guide

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 ingest-trends-batch \
  --csv examples/trends_sample.csv

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 list-trends \
  --topic "sales LLMs" \
  --status unused

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 prompt-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends
```

`prompt-x` is useful for checking retrieved examples, context, skills, analytics hints, and trends. To generate drafts, configure a provider first:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends \
  --variants 2
```

Take one generated draft ID from the provider-backed output, then record it as published:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/999" \
  --date "2026-06-25" \
  --author "Gavin"
```

Take the published post ID from the output, then add performance:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 add-metrics \
  --post-id "<post id>" \
  --impressions 22000 \
  --likes 390 \
  --replies 41 \
  --reposts 62 \
  --quotes 14 \
  --bookmarks 120

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 analytics
```

## Real Workflow

Initialize the real local database:

```bash
python3 -m leadpoet_social_brain.cli init
```

Import your existing X corpus:

```bash
python3 -m leadpoet_social_brain.cli ingest-x-batch --csv data/x_posts.csv
```

Or pull your own posts from the X API:

```bash
export X_BEARER_TOKEN="<your bearer token>"

python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

Rebuild the writing guide from posts marked `imitate=true`:

```bash
python3 -m leadpoet_social_brain.cli rebuild-style-guide
```

Add or import timely market context:

```bash
python3 -m leadpoet_social_brain.cli ingest-trends-batch --csv data/trends.csv
```

Configure your provider before generating:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"
```

Generate X drafts:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer context beats AI SDR volume" \
  --use-trends \
  --variants 3
```

Publish a draft into the system:

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/<id>" \
  --date "2026-06-25" \
  --author "Gavin"
```

Add metrics after the post has had time to perform:

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

Review what is working:

```bash
python3 -m leadpoet_social_brain.cli analytics
```

## X Post Import

Batch import is the easiest way to bring in the first 30 or so X posts.

```bash
python3 -m leadpoet_social_brain.cli ingest-x-batch \
  --csv data/x_posts.csv \
  --default-author "Gavin"
```

CSV columns:

```text
text,url,date,author,topic,post_type,impressions,likes,replies,reposts,quotes,bookmarks,worked_reason,hook,structure,tone,cta,formatting,pattern,imitate
```

Required columns:

- `text`
- `topic`
- `post_type`

Recommended columns:

- `url`
- `date`
- `author`
- `impressions`
- `likes`
- `replies`
- `reposts`
- `quotes`
- `bookmarks`
- `worked_reason`
- `imitate`

Optional analysis labels:

- `hook`
- `structure`
- `tone`
- `cta`
- `formatting`
- `pattern`

Notes:

- Missing optional fields do not break import.
- Empty rows are ignored.
- Rows missing required fields are skipped and listed as errors.
- Long X examples are accepted during ingestion. Character limits apply during generation and publishing, not storage.
- If impressions are present, engagement rate is calculated automatically from likes, replies, reposts, quotes, and bookmarks.
- Manual ingest, publish, and metrics commands also accept `--engagement-rate` if you want to supply it directly.
- `imitate` accepts truthy values like `true`, `yes`, `y`, `t`, or `1`.

Manual import is still useful for one-off examples:

```bash
python3 -m leadpoet_social_brain.cli ingest-x \
  --text "Most sales AI is still just mail merge with nicer adjectives. The useful version knows why this buyer cares now." \
  --url "https://x.com/gavin/status/123" \
  --date "2026-06-25" \
  --author "Gavin" \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --impressions 12000 \
  --likes 180 \
  --replies 22 \
  --reposts 31 \
  --quotes 8 \
  --bookmarks 45 \
  --worked-reason "Clear enemy, specific point of view, plain language." \
  --hook enemy_hook \
  --structure enemy_then_better_model \
  --tone plainspoken \
  --cta none \
  --formatting single_paragraph \
  --pattern enemy_then_better_model \
  --imitate
```

Supported X post types:

```text
product_update
founder_insight
bittensor
bittensor_update
investor_facing
quote_tweet
launch
technical_explanation
light_funny
```

Dedicated post-type skill files currently exist for:

```text
founder_insight
product_update
bittensor
bittensor_update
quote_tweet
```

`bittensor` and `bittensor_update` both use `skills/post_types/bittensor_update.md`. Other supported post types still work. They use the global voice, X platform skill, review filter, retrieved examples, context, and provider.

## X API Import

Use `sync-x-user` when you want to pull your own X posts directly from the X API instead of building a CSV by hand.

Set a bearer token in your shell:

```bash
export X_BEARER_TOKEN="<your bearer token>"
```

Run a dry run first:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 25 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight \
  --dry-run
```

Import posts:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

Optional time window:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --start-time "2026-01-01T00:00:00Z" \
  --end-time "2026-06-25T00:00:00Z" \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

By default, replies and retweets are excluded. Use `--include-replies` or `--include-retweets` when you want them.

The importer:

- resolves the username to an X user ID
- calls `GET /2/users/:id/tweets`
- supports pagination and `--limit`
- normalizes API results into the existing `ingest_x_post` flow
- avoids duplicates by canonical URL or X post ID
- does not store `X_BEARER_TOKEN` in SQLite
- does not guess `worked_reason`, `hook`, `structure`, `tone`, `cta`, `formatting`, or `pattern`
- defaults `imitate` to false unless `--imitate` is passed

The importer requests public metrics only. It maps available metrics into impressions, likes, replies, reposts, quotes, and bookmarks. If a metric is missing, import still succeeds.

URL clicks, profile clicks, private analytics, and some impression data may require user-context auth or elevated API access. Those are intentionally not part of this simple importer.

## Context

Seeded context is editable and modular.

The default seed includes:

- `style_guide/gavin_founder_voice_v0`
- `product/leadpoet_product_v0`
- `audience/primary_audiences_v0`
- `positioning/leadpoet_positioning_v0`
- `bittensor/bittensor_subnet_context_v0`
- `sales_llm_vision/sales_llm_vision_v0`
- `priority/company_priorities_v0`

Add or update context:

```bash
python3 -m leadpoet_social_brain.cli add-context \
  --kind priority \
  --name current_company_priority \
  --content "Make Leadpoet feel concrete: show product progress, customer pain, and why sales LLMs need proprietary context."
```

Rebuild the observed writing guide:

```bash
python3 -m leadpoet_social_brain.cli rebuild-style-guide --limit 12
```

The rebuilt style guide is meant to be a writing guide, not a tag report. It summarizes recurring voice traits, natural post shapes, strong-post behaviors, things to avoid, and reusable writing principles.

## Skills

Reusable writing instructions live in markdown files:

```text
skills/global_voice.md
skills/platforms/x.md
skills/post_types/founder_insight.md
skills/post_types/product_update.md
skills/post_types/bittensor_update.md
skills/post_types/quote_tweet.md
skills/review_filter.md
```

Generation loads:

1. `skills/global_voice.md`
2. the platform skill, currently `skills/platforms/x.md`
3. the post-type skill if one exists
4. `skills/review_filter.md`
5. retrieved examples, context, analytics hints, and optional trends from SQLite

Generated drafts store the skill file paths and content hashes in the structured context snapshot. That makes later review possible without storing one huge prompt as the main source of truth.

## Generation

The v0.2 prompt is example-first.

Examples are shown like this:

```text
Example:
[exact post text]

Why it worked:
[human note]

Performance:
[optional concise metrics summary]
```

The prompt then includes:

- Leadpoet context
- observed style guide
- optional Trend Scout context
- human-readable analytics hints when there is enough data
- one clear request angle
- skill file instructions
- review filter

Labels such as `hook`, `structure`, `tone`, `cta`, `formatting`, and `pattern` remain useful for analytics, but they are not put front-and-center in the writing prompt.

Print a prompt without generating:

```bash
python3 -m leadpoet_social_brain.cli prompt-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends
```

`prompt-x` does not require a provider. `generate-x` does.

Generate drafts:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"

python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --audience "technical founders and sales leaders" \
  --tone "plain, sharp, useful" \
  --cta "DM me if you are working on this" \
  --extra-context "Tie this to why Leadpoet cares about buyer timing." \
  --use-trends \
  --variants 3
```

Use `--max-chars` only when you want to override the default X limit:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "Bittensor" \
  --post-type bittensor_update \
  --angle "subnets need measurable quality signals" \
  --max-chars 280
```

## LLM Provider Mode

Draft generation requires an LLM provider. Set `SOCIAL_BRAIN_PROVIDER` to `module.path:create_provider`.

If no provider is configured, `generate-x` exits with:

```text
No LLM provider configured. Set SOCIAL_BRAIN_PROVIDER=module.path:create_provider to generate drafts.
```

Provider interface:

```python
class MyProvider:
    def generate(self, prompt: str, *, variants: int) -> list[str]:
        ...

def create_provider():
    return MyProvider()
```

Example:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"

python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends \
  --variants 3
```

The provider can call any LLM you want. `generation.py` only assembles the prompt, calls `provider.generate(...)`, validates the returned drafts, and saves context metadata.

## Trend Scout

Trend Scout is optional context, not an automated research system.

Use it for timely market/news/product context around:

- AI
- sales LLMs
- lead generation
- GTM
- AI SDRs
- sales automation
- Bittensor
- subnets
- competitors
- funding
- customer signals

Add one trend manually:

```bash
python3 -m leadpoet_social_brain.cli add-trend \
  --title "AI SDR tools shift from volume to buyer context" \
  --source-url "https://example.com/ai-sdr-context" \
  --source-type news \
  --topic "sales LLMs" \
  --summary "More teams are questioning high-volume AI SDR workflows." \
  --why-it-matters "Buyers are tuning out generic automation and rewarding timely context." \
  --leadpoet-angle "Leadpoet can argue that sales AI needs buyer timing, not just more generated copy." \
  --suggested-hooks "The AI SDR debate is really a context debate." \
  --urgency high \
  --date "2026-06-25"
```

Batch import trends:

```bash
python3 -m leadpoet_social_brain.cli ingest-trends-batch --csv data/trends.csv
```

Trend CSV columns:

```text
title,source_url,source_type,topic,summary,why_it_matters,leadpoet_angle,suggested_hooks,urgency,date,status,engagement_notes,source_author,raw_text
```

Required trend columns:

- `title`
- `topic`
- `summary`

Recommended trend columns:

- `source_url`
- `source_type`
- `why_it_matters`
- `leadpoet_angle`
- `suggested_hooks`
- `urgency`
- `date`

Supported `status` values by convention:

```text
unused
used
ignored
```

Supported `urgency` values by convention:

```text
low
medium
high
```

Supported `source_type` values by convention:

```text
X_post
news
company_update
product_launch
funding
technical_update
Bittensor_update
customer_signal
competitor_update
other
```

List trends:

```bash
python3 -m leadpoet_social_brain.cli list-trends \
  --topic "sales LLMs" \
  --status unused \
  --urgency high \
  --limit 10
```

Mark a trend used manually:

```bash
python3 -m leadpoet_social_brain.cli mark-trend-used --trend-id "<trend id>"
```

When `generate-x --use-trends` is passed, the generator retrieves up to 3 relevant unused trends for the requested topic and angle. Trend context is shown concisely:

```text
Trend:
[title]

What happened:
[summary]

Why it matters:
[why_it_matters]

Leadpoet angle:
[leadpoet_angle]

Possible hooks:
[suggested_hooks]
```

When a generated draft using trends is published, those trend influences are preserved on the post. Analytics can then show performance by trend topic, source type, urgency, and used trend.

## Publishing And Feedback Loop

Generated drafts are stored in `generated_drafts`.

Each draft stores:

- request fields
- draft text
- explanation
- influence labels
- `quality_notes`
- selected examples
- selected patterns observed from examples
- selected context sections
- analytics hints
- selected trends
- skill file paths and hashes
- generation mode, currently `provider`
- provider name

Publishing a draft creates a `posts` row with `status=published` and `source_draft_id=<draft id>`.

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/<id>" \
  --date "2026-06-25" \
  --author "Gavin"
```

Metrics can be supplied during publish:

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/<id>" \
  --date "2026-06-25" \
  --author "Gavin" \
  --impressions 22000 \
  --likes 390 \
  --replies 41 \
  --reposts 62 \
  --quotes 14 \
  --bookmarks 120
```

Or added later:

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

List stored posts:

```bash
python3 -m leadpoet_social_brain.cli list-posts --platform x --limit 20
```

## Analytics

Run:

```bash
python3 -m leadpoet_social_brain.cli analytics
```

Analytics reports:

- human-readable hints when there is enough data
- post type performance
- topic performance
- reusable pattern performance
- hook performance
- structure performance
- tone performance
- CTA performance
- formatting performance
- trend topic performance
- trend source type performance
- trend urgency performance
- used trend performance

Rows include:

- count
- average engagement rate
- average impressions
- average bookmarks
- average replies
- average reposts
- average total engagements
- top post ID

Analytics is intentionally transparent. It should inform future prompts without turning the workflow into a tagging/admin chore.

## Data Model

SQLite tables:

- `posts`: platform, text, URL, published date, author, topic, post type, status, optional `source_draft_id`
- `post_metrics`: impressions, likes, replies, reposts, quotes, bookmarks, engagement rate snapshots
- `post_notes`: manual notes, why-it-worked rationale, hook, structure, tone, CTA, formatting, pattern, imitation flag
- `post_influences`: links from posts to draft-selected examples, context, patterns, analytics hints, and trends
- `generated_drafts`: draft text, request, explanation, influence labels, structured context snapshot
- `trend_items`: optional timely market context
- `patterns`: reusable named patterns
- `context_sections`: modular Leadpoet context and style guide sections

The most important linkage:

```text
generated_drafts.id -> posts.source_draft_id -> post_influences -> post_metrics
```

That lets later metrics be analyzed against the context and trends that shaped the draft.

## CLI Reference

Top-level commands:

```text
init
ingest-x
ingest-x-batch
sync-x-user
add-context
add-pattern
generate-x
add-trend
ingest-trends-batch
list-trends
mark-trend-used
publish-draft-x
add-metrics
list-posts
rebuild-style-guide
analytics
prompt-x
```

Use built-in help for details:

```bash
python3 -m leadpoet_social_brain.cli --help
python3 -m leadpoet_social_brain.cli generate-x --help
python3 -m leadpoet_social_brain.cli sync-x-user --help
python3 -m leadpoet_social_brain.cli ingest-x-batch --help
python3 -m leadpoet_social_brain.cli add-trend --help
```

## Tests

Run tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Compile check:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/social_brain_pycache python3 -m compileall leadpoet_social_brain tests
```

The `PYTHONPYCACHEPREFIX` form avoids macOS cache permission issues in sandboxed environments.

Current tests cover:

- skill file loading
- prompt assembly with global, platform, post-type, and review skills
- example-first prompt format
- long X example ingestion
- batch X import
- X API post normalization
- X API duplicate prevention and pagination
- `sync-x-user --dry-run`
- missing X API metrics
- style guide rebuild quality
- provider-required generation behavior
- draft-to-post source linkage
- trend item storage
- trend CSV import
- `generate-x --use-trends`
- trend influence preservation
- analytics by hook, structure, tone, CTA, formatting, pattern, and trend dimensions

## Design Principles

- Examples teach taste.
- Context teaches what matters now.
- Trends add timeliness, but only when requested.
- Analytics should be useful and optional.
- Labels support learning, but should not dominate writing.
- Markdown skill files should replace hardcoded voice rules.
- Provider-backed generation is the real quality path.
- LinkedIn expansion should be additive, not a rewrite.
