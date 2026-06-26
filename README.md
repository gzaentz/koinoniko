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
- Plans a simple weekly X content queue for review and approval.
- Provides a minimal login-gated web UI with Dashboard, Content Reviewer, and Account settings.
- Keeps the database synced with posts created directly on X through Account sync controls.
- Supports a protected daily X sync endpoint for Railway Cron.
- Lets you refresh or add metrics after publishing and analyze what is working.
- Requires a configured LLM provider for draft generation.

Provider requirement:

- `generate-x` requires `SOCIAL_BRAIN_PROVIDER=module.path:create_provider`.
- If no provider is configured, generation fails clearly and does not create drafts.
- Built-in OpenRouter provider: `leadpoet_social_brain.openrouter_provider:create_provider`.

## Project Layout

```text
leadpoet_social_brain/
  core/models.py          Platform-neutral data models
  platforms/base.py       Platform adapter protocol
  platforms/x.py          X post types, URL validation, and character limits
  platforms/x_api.py      Minimal X API import and public metrics sync
  storage/sqlite_store.py SQLite schema, migrations, and repository functions
  ingestion.py            X example import, trend import, publish workflow
  content_queue.py        Weekly planning, local queue, approval, posting helpers
  context.py              Context retrieval and style guide rebuilds
  generation.py           Prompt assembly, provider call, draft validation
  skills.py               Tiny markdown skill loader
  providers.py            Optional provider protocol and env loader
  openrouter_provider.py  OpenRouter provider for provider-backed generation
  analytics.py            Transparent performance summaries
  cli.py                  Command-line interface
  web/app.py              Minimal login-gated HTTP API and static UI server
  web/static/             Single-page local UI assets
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
cd koinoniko
python3 -m leadpoet_social_brain.cli --help
```

The default database path is:

```text
data/social_brain.sqlite3
```

Database path resolution is:

1. explicit `--db`
2. `SOCIAL_BRAIN_DB`
3. `RAILWAY_VOLUME_MOUNT_PATH/social_brain.sqlite3`
4. `data/social_brain.sqlite3`

Use `--db` when you want a separate test or demo database:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 init
```

Use `SOCIAL_BRAIN_DB` when the app should share one configured SQLite path across CLI and UI:

```bash
export SOCIAL_BRAIN_DB="/app/data/social_brain.sqlite3"
```

Use a fresh database path when you want clean demo output.

For local secrets, copy the example env file, fill in your real values, then load it into your shell:

```bash
cp .env.example .env
set -a; source .env; set +a
```

`.env` is ignored by git. Keep real X/OpenRouter tokens there locally or in Railway Variables for production.

## Web UI

The web UI is a thin layer over the same SQLite store and core functions used by the CLI. It is protected by a single SQLite-backed login and can run locally or on Railway.

Start it from the repo root:

```bash
python3 -m leadpoet_social_brain.web.app --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

Optional flags:

```bash
python3 -m leadpoet_social_brain.web.app \
  --db data/social_brain.sqlite3 \
  --host 127.0.0.1 \
  --port 8787 \
  --env-file .env
```

The UI loads `.env` by default if present. It only reads environment variables into the local process; it does not display token values.

The UI requires login. The username and password hash are stored in SQLite, not in environment variables or source code.

```bash
python3 -m leadpoet_social_brain.cli set-web-login --username gavinz444
```

The command prompts for the password and stores only a salted PBKDF2 hash in the configured SQLite database. If no web login has been stored yet, the app fails closed and shows a configuration-required login page. Resetting the password updates the hash and invalidates existing sessions.

The UI supports:

- a focused Dashboard for daily trends, weekly trends, and recent posts
- a Content Reviewer board for review, edit, approve, skip, schedule, and approved-only post-now
- an Account page for connection status, model routing, provider testing, and X account sync
- inline AI revisions and Best Version rewrites for queued posts through the configured provider
- accepting a revision without auto-approval
- weekly queue planning from a modal
- trend add/list workflow from a modal
- manual X post import and X metrics sync

Generation and AI editing require a provider. For OpenRouter:

```bash
OPENROUTER_API_KEY="<your OpenRouter key>"
SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
```

Set model routing in the web UI under `Account -> Advanced Model Settings`. Standard mode uses the default model in one provider call. Best Version mode uses a simple strategist -> natural writer -> evaluator flow, then saves the suggested final version.

Model names are not secrets and may be stored in SQLite app settings. API keys stay in environment variables only. For OpenRouter, use OpenRouter model IDs.

Optional model env fallbacks:

```bash
SOCIAL_BRAIN_DEFAULT_MODEL="..."
SOCIAL_BRAIN_STRATEGIST_MODEL="..."
SOCIAL_BRAIN_NATURAL_WRITER_MODEL="..."
SOCIAL_BRAIN_EVALUATOR_MODEL="..."
```

If a role-specific model is blank, Koinoniko uses the default model. If the default model is blank, the provider can use its own default. Legacy `OPENROUTER_MODEL` and `SOCIAL_BRAIN_MODEL` still work as default-model fallbacks.

Posting to X requires write credentials. Read-only X import and metric sync use `X_BEARER_TOKEN`; posting needs either `X_OAUTH2_ACCESS_TOKEN` or OAuth 1.0a keys. The UI does not auto-post or auto-approve anything.

## Account Sync

Koinoniko can stay in sync with posts created directly on X.

In the web UI, open `Account` and use:

- `Sync X Posts` to import authored posts from `gavinzaentz` that are missing from SQLite.
- `Sync X Metrics` to append fresh public metric snapshots for stored X posts.
- `Sync All X Data` to run post sync first, then metrics sync.

Defaults:

- username: `gavinzaentz`
- default topic: `Leadpoet`
- default post type: `founder_insight`
- exclude replies: `true`
- exclude retweets: `true`
- `imitate=false`

The account sync does not guess `worked_reason`, hook, structure, tone, CTA, formatting, or pattern labels. Add those manually only for posts you want the system to learn from.

The Account page shows X read/write status, provider status, last post sync, last metrics sync, daily sync status, total stored X posts, latest imported X post date, and latest sync error. The Dashboard shows a quiet `Last X sync` line above recent posts.

Sync metadata is stored in SQLite under simple keys:

- `x_posts_sync`
- `x_metrics_sync`
- `x_daily_sync`

Only timestamps, status, JSON summaries, and error messages are stored. Tokens are never stored in SQLite.

Requirements:

- `X_BEARER_TOKEN` for post import and public metrics.
- Private X analytics such as URL clicks and profile clicks are not included yet.

For a hosted dashboard that stays fresh, run `sync-x-posts` every 30 minutes and `sync-x-metrics` every 2 hours through Railway Cron. Manual Account buttons are still available when you want to force an immediate refresh.

## Railway Deployment

This repo includes `railway.toml` for the local web UI:

```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python -m leadpoet_social_brain.web.app --host 0.0.0.0"
```

The app reads Railway's `PORT` environment variable automatically. Locally it still defaults to `127.0.0.1:8787`.

Required Railway settings:

- Deploy from the `main` branch of `gzaentz/koinoniko`.
- Add a Railway Volume and mount it at `/app/data`.
- Set `SOCIAL_BRAIN_DB=/app/data/social_brain.sqlite3`, or rely on Railway's automatic `RAILWAY_VOLUME_MOUNT_PATH=/app/data` variable.
- Keep service replicas at `1` while using SQLite.
- Set `X_BEARER_TOKEN` if you want X import and metric sync.
- Set `OPENROUTER_API_KEY` and `SOCIAL_BRAIN_PROVIDER=leadpoet_social_brain.openrouter_provider:create_provider` before using generation, planning, or AI edits.
- Set model routing from the hosted app's Account page. `OPENROUTER_MODEL` remains available as a legacy default-model fallback.
- Set X write credentials only if you want approved posts to be posted from Railway.
- Set `TASK_SECRET` if you want Railway Cron to run protected X sync endpoints.
- Optional: set `SOCIAL_BRAIN_TASK_TOKEN` if you want an external cron service to trigger hosted task endpoints.

SQLite notes for Railway:

- The app creates the DB parent directory before opening SQLite.
- The deploy logs print the active DB path as `Social Brain DB path: ...`; on Railway it should be `/app/data/social_brain.sqlite3`.
- SQLite opens with WAL mode, a `5000ms` busy timeout, and foreign keys enabled.
- Do not write or initialize the DB during build or pre-deploy steps; Railway volumes are mounted at runtime.
- Opening the app safely runs schema creation/migrations at runtime.
- Run `python -m leadpoet_social_brain.cli init` once after deploy if you need the default context/pattern seed data.
- Run `python -m leadpoet_social_brain.cli set-web-login --username gavinz444` once against the mounted volume to store the web login before using the public domain.
- Do not add Postgres for v0.2.
- Do not store API tokens in SQLite. Keep X/API tokens in Railway environment variables.

If Railway shows "Unexposed service", open the service settings and generate a public domain after the deployment succeeds. The build/start config makes the app listen on the correct host and port; the domain is still a Railway service setting.

## First Run Checklist

For a new hosted database:

1. Add a Railway Volume mounted at `/app/data`.
2. Set `SOCIAL_BRAIN_DB=/app/data/social_brain.sqlite3` or rely on `RAILWAY_VOLUME_MOUNT_PATH`.
3. Deploy the app and confirm logs show `Social Brain DB path: /app/data/social_brain.sqlite3`.
4. Run `python -m leadpoet_social_brain.cli init` against that DB if seed context is needed.
5. Run `python -m leadpoet_social_brain.cli set-web-login --username gavinz444`.
6. Set `X_BEARER_TOKEN`.
7. Open Account and run `Sync X Posts`, or run `sync-x-user` from the CLI.
8. Use `update-post-notes` to mark strongest examples with `imitate=true`.
9. Run `rebuild-style-guide`.
10. Set `OPENROUTER_API_KEY` and `SOCIAL_BRAIN_PROVIDER`.
11. Open Account in the web UI and save model routing, or leave role models blank to use the default model.
12. Add trends if you want timely planning context.
13. Plan the week, review/edit, then approve only posts you are ready to publish.
14. Set X write auth before posting from the hosted app.
15. Set `TASK_SECRET` if Railway Cron will call protected X sync endpoints.
16. Set `SOCIAL_BRAIN_TASK_TOKEN` only if an external scheduler will call the older `/api/tasks/*` endpoints.

## Hosted Task Triggers

Social Brain does not run a scheduler or background worker. On Railway, approved scheduled posts will stay queued until something triggers publishing, and the dashboard stays current only after a sync task refreshes SQLite.

Use these protected endpoints for hosted sync:

```text
POST /tasks/sync-x-posts
POST /tasks/sync-x-metrics
POST /tasks/sync-x-all
POST /tasks/sync-x-daily
POST /tasks/refresh-trends
```

These endpoints use `TASK_SECRET`:

```bash
TASK_SECRET="<long random secret>"
```

Recommended Railway Cron setup for a fresh dashboard:

```text
X post sync:    every 30 minutes    */30 * * * *
X metrics sync: every 2 hours        15 */2 * * *
Trend refresh:  daily                25 6 * * *
```

Create Railway Cron services that run these start commands and exit:

```bash
python -m leadpoet_social_brain.web.task_client sync-x-posts
python -m leadpoet_social_brain.web.task_client sync-x-metrics
python -m leadpoet_social_brain.web.task_client refresh-trends
```

Set these variables on each Cron service:

```bash
SOCIAL_BRAIN_URL="https://<app-domain>"
TASK_SECRET="<same TASK_SECRET as the web app>"
```

The task client calls the main web app with:

```text
POST https://<app-domain>/tasks/sync-x-posts
Authorization: Bearer <TASK_SECRET>

POST https://<app-domain>/tasks/sync-x-metrics
Authorization: Bearer <TASK_SECRET>
```

`sync-x-posts` imports recent authored posts from X and skips duplicates by X post ID/canonical URL. `sync-x-metrics` appends fresh public metric snapshots for stored posts. The Dashboard reads the latest stored data and shows `Last X sync`.
`refresh-trends` pulls the top 5 daily and top 5 weekly AI, sales, and lead generation trends through Scrapingdog when `SCRAPINGDOG_API_KEY` is configured, with the no-key fallback still available.

If you prefer one job instead of two, run this hourly:

```bash
python -m leadpoet_social_brain.web.task_client sync-x-all
```

The older daily endpoint remains available:

```text
POST /tasks/sync-x-daily
```

It imports recent posts, refreshes recent metrics, and stores a daily summary under Account. For the always-current dashboard, prefer the frequent post and metrics jobs above.

Older task endpoints are still available for external schedulers that already use `SOCIAL_BRAIN_TASK_TOKEN`:

```text
POST /api/tasks/sync-x-user
POST /api/tasks/publish-due-x
POST /api/tasks/sync-x-metrics
POST /api/tasks/weekly-report-x
```

Set a task token in Railway Variables:

```bash
SOCIAL_BRAIN_TASK_TOKEN="<long random secret>"
```

Then call the endpoint from Railway Cron, GitHub Actions, cron-job.org, or another scheduler:

```bash
curl -X POST "$SOCIAL_BRAIN_URL/api/tasks/publish-due-x" \
  -H "Authorization: Bearer $SOCIAL_BRAIN_TASK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"limit":20}'

curl -X POST "$SOCIAL_BRAIN_URL/api/tasks/sync-x-user" \
  -H "Authorization: Bearer $SOCIAL_BRAIN_TASK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username":"gavinzaentz","limit":100,"auto_categorize":true,"exclude_replies":true,"exclude_retweets":true}'

curl -X POST "$SOCIAL_BRAIN_URL/api/tasks/sync-x-metrics" \
  -H "Authorization: Bearer $SOCIAL_BRAIN_TASK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"since_days":90,"limit":100}'

curl -X POST "$SOCIAL_BRAIN_URL/api/tasks/weekly-report-x" \
  -H "Authorization: Bearer $SOCIAL_BRAIN_TASK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"since_days":7}'
```

If `SOCIAL_BRAIN_TASK_TOKEN` is missing, task endpoints fail closed. UI endpoints still require the normal web login session.

If `TASK_SECRET` is missing, `/tasks/sync-x-daily` fails closed. The endpoint also requires `X_BEARER_TOKEN`.

## Hosted Security

The hosted UI is intentionally simple but should stay locked down:

- The app fails closed until a SQLite-backed web login is created.
- Session cookies are signed, `HttpOnly`, `SameSite=Strict`, and `Secure` on Railway by default.
- UI mutation endpoints require login and use POST/PATCH.
- Daily X sync requires `TASK_SECRET`; older `/api/tasks/*` endpoints require `SOCIAL_BRAIN_TASK_TOKEN`.
- API tokens are read from environment variables and are not displayed in the UI or stored in SQLite.

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
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
# Optional CLI fallback if no model is saved in Account:
# export OPENROUTER_MODEL="<any OpenRouter model slug>"

python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends \
  --variants 2
```

For a single higher-quality final draft, use Best Version mode:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends \
  --best-version
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

The normal v0.2 loop is:

1. Open Account and run `Sync All X Data`, or use `sync-x-user`/`sync-x-metrics` from the CLI.
2. `list-posts` to review them.
3. `update-post-notes` to mark the strongest posts with `imitate=true` and `worked_reason`.
4. `rebuild-style-guide`.
5. Add/import current trends.
6. `plan-week-x` to create a reviewable weekly queue.
7. Edit and approve queued posts.
8. `publish-due-x` or `post-now-x` for approved items only.
9. `sync-x-metrics` after posting.
10. Review analytics or `weekly-report-x`.

You can do most of this in the web UI after importing posts, annotating winners, and configuring a provider. Account sync keeps posts created directly on X available to the Dashboard and analytics.

Initialize the real local database:

```bash
python3 -m leadpoet_social_brain.cli init
```

Pull your own posts from the X API:

```bash
export X_BEARER_TOKEN="<your bearer token>"

python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 3200 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

CSV import remains available if you already have a hand-built corpus:

```bash
python3 -m leadpoet_social_brain.cli ingest-x-batch --csv data/x_posts.csv
```

Review imported posts and copy the IDs for the strongest examples:

```bash
python3 -m leadpoet_social_brain.cli list-posts --platform x --limit 30
```

Mark the best examples with human notes:

```bash
python3 -m leadpoet_social_brain.cli update-post-notes \
  --post-id "<post id>" \
  --worked-reason "Strong because it makes a sharp contrast and explains the product point in plain language." \
  --imitate true \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --structure enemy_then_better_model
```

Rebuild the writing guide from posts marked `imitate=true`:

```bash
python3 -m leadpoet_social_brain.cli rebuild-style-guide
```

Add or import timely market context:

```bash
python3 -m leadpoet_social_brain.cli ingest-trends-batch --csv data/trends.csv
```

Configure OpenRouter before generating:

```bash
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
# Optional CLI fallback if no model is saved in Account:
# export OPENROUTER_MODEL="<any OpenRouter model slug>"
```

Generate standalone X drafts:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer context beats AI SDR volume" \
  --use-trends \
  --variants 3
```

Best Version mode uses strategist -> natural writer -> evaluator routing and returns the suggested final version:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer context beats AI SDR volume" \
  --use-trends \
  --best-version
```

Or plan a week of queued X posts:

```bash
python3 -m leadpoet_social_brain.cli plan-week-x \
  --week-start 2026-06-29 \
  --posts 7 \
  --use-trends \
  --trend-window-days 14 \
  --topic-mix "sales LLMs:3,Bittensor:2,product:2" \
  --default-post-type founder_insight \
  --best-version
```

Review, edit, and approve:

```bash
python3 -m leadpoet_social_brain.cli list-content-queue --status needs_review

python3 -m leadpoet_social_brain.cli update-queued-post \
  --queue-id "<queue id>" \
  --text "edited post text" \
  --scheduled-at "2026-06-30T10:00:00-04:00"

python3 -m leadpoet_social_brain.cli revise-queued-post \
  --queue-id "<queue id>" \
  --instruction "Make this more natural and founder-led." \
  --best-version

python3 -m leadpoet_social_brain.cli approve-queued-post --queue-id "<queue id>"
```

Approved queued posts can be published when due:

```bash
python3 -m leadpoet_social_brain.cli publish-due-x
```

Publish a draft into the system:

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/<id>" \
  --date "2026-06-25" \
  --author "Gavin"
```

Refresh public metrics after the post has had time to perform:

```bash
python3 -m leadpoet_social_brain.cli sync-x-metrics --post-id "<post id>"

python3 -m leadpoet_social_brain.cli sync-x-metrics --since-days 90
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

Use Account `Sync X Posts` for the normal web workflow. It imports authored posts from `gavinzaentz` with safe defaults and no LLM labeling.

Use CLI `sync-x-user` when you want more control, a dry run, a time window, or optional LLM topic/post-type categorization instead of building a CSV by hand.

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

Import posts with safe defaults:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 3200 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

For optional CLI LLM categorization, also configure your generation provider. With OpenRouter:

```bash
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
# Optional CLI fallback if no model is saved in Account:
# export OPENROUTER_MODEL="<any OpenRouter model slug>"
```

Then add `--auto-categorize`:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --exclude-replies \
  --exclude-retweets \
  --auto-categorize
```

Optional time window:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --start-time "2026-01-01T00:00:00Z" \
  --end-time "2026-06-25T00:00:00Z" \
  --auto-categorize
```

By default, replies and retweets are excluded. Use `--include-replies` or `--include-retweets` when you want them.

`--auto-categorize` uses the configured LLM provider to classify only `topic` and `post_type`. It does not guess `worked_reason`, hooks, structures, tone, CTAs, formatting, patterns, or `imitate`.

The importer:

- resolves the username to an X user ID
- calls `GET /2/users/:id/tweets`
- supports pagination and `--limit`
- normalizes API results into the existing `ingest_x_post` flow
- avoids duplicates by canonical URL or X post ID
- does not store `X_BEARER_TOKEN` in SQLite
- can use an LLM provider for `topic` and `post_type` when `--auto-categorize` is passed
- does not guess `worked_reason`, `hook`, `structure`, `tone`, `cta`, `formatting`, or `pattern`
- defaults `imitate` to false unless `--imitate` is passed

The importer requests public metrics only. It maps available metrics into impressions, likes, replies, reposts, quotes, and bookmarks. If a metric is missing, import still succeeds.

URL clicks, profile clicks, private analytics, and some impression data may require user-context auth or elevated API access. Those are intentionally not part of this simple importer.

## X Metrics Sync

Use `sync-x-metrics` to refresh public metrics for posts already stored in Social Brain.

```bash
export X_BEARER_TOKEN="<your bearer token>"

python3 -m leadpoet_social_brain.cli sync-x-metrics --since-days 90

python3 -m leadpoet_social_brain.cli sync-x-metrics --post-id "<post id>"

python3 -m leadpoet_social_brain.cli sync-x-metrics --dry-run
```

What to know:

- `sync-x-user` imports posts and their initial public metrics when the X API returns them.
- `sync-x-metrics` refreshes metrics for existing stored posts with parseable X URLs.
- Each refresh appends a new metric snapshot. It does not overwrite older snapshots.
- Analytics uses the latest metric snapshot by default.
- `add-metrics` remains available for manual corrections or metrics you collect elsewhere.
- Public metrics are supported for now: impressions, likes, replies, reposts, quotes, and bookmarks when available.
- Private clicks, profile clicks, and richer creator analytics are not included yet.

Useful filters:

```bash
python3 -m leadpoet_social_brain.cli sync-x-metrics \
  --x-post-id "<x post id>"

python3 -m leadpoet_social_brain.cli sync-x-metrics \
  --since-days 30 \
  --limit 50
```

The command prints posts checked, metrics updated, skipped posts, errors, and top movers since the previous snapshot when there is enough history.

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
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
# Optional CLI fallback if no model is saved in Account:
# export OPENROUTER_MODEL="<any OpenRouter model slug>"

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

## Weekly Content Calendar

`plan-week-x` creates a local posting queue. It does not post anything.

It uses:

- high-performing examples
- observed writing guide
- Leadpoet context
- recent unused Trend Scout items when requested
- analytics hints
- skill files

Plan a week:

```bash
python3 -m leadpoet_social_brain.cli plan-week-x \
  --week-start 2026-06-29 \
  --posts 7 \
  --use-trends \
  --trend-window-days 14 \
  --topic-mix "sales LLMs:3,Bittensor:2,product:2" \
  --cadence daily_mornings \
  --default-post-type founder_insight
```

If `--use-trends` is passed and there are no recent unused trends, planning fails clearly instead of inventing news. To allow evergreen authority posts, pass:

```bash
python3 -m leadpoet_social_brain.cli plan-week-x \
  --week-start 2026-06-29 \
  --posts 7 \
  --allow-evergreen
```

Review and edit:

```bash
python3 -m leadpoet_social_brain.cli list-content-queue --status needs_review

python3 -m leadpoet_social_brain.cli update-queued-post \
  --queue-id "<queue id>" \
  --text "edited post text" \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --scheduled-at "2026-06-30T10:00:00-04:00" \
  --notes "Looks strong after edit."
```

Approve:

```bash
python3 -m leadpoet_social_brain.cli approve-queued-post --queue-id "<queue id>"

python3 -m leadpoet_social_brain.cli approve-week-x --week-start 2026-06-29
```

Nothing posts unless the queued item has `status=approved`.

Post approved items:

```bash
python3 -m leadpoet_social_brain.cli post-now-x --queue-id "<queue id>"

python3 -m leadpoet_social_brain.cli publish-due-x
```

For simple local scheduling, use cron or launchd to run:

```bash
python3 -m leadpoet_social_brain.cli publish-due-x
```

every 15 to 60 minutes. On Railway, use the protected `/api/tasks/publish-due-x` endpoint from an external scheduler. Social Brain does not run a background service.

Weekly learning:

```bash
python3 -m leadpoet_social_brain.cli weekly-report-x --since-days 7
```

## LLM Provider Mode

Draft generation and weekly planning require an LLM provider. Set `SOCIAL_BRAIN_PROVIDER` to `module.path:create_provider`.

If no provider is configured, `generate-x` exits with:

```text
No LLM provider configured. Set SOCIAL_BRAIN_PROVIDER=module.path:create_provider to generate drafts.
```

### OpenRouter

OpenRouter support is built in. Set:

```bash
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
```

Then save model routing in the web UI under `Account -> Advanced Model Settings`. The default model is used for Standard generation. Strategist, natural writer, and evaluator models are optional and only used by Best Version mode. If a role model is blank, it falls back to the default model. If no Account model is saved, env fallbacks such as `SOCIAL_BRAIN_DEFAULT_MODEL` or legacy `OPENROUTER_MODEL` are used.

Examples of model slugs might look like:

```text
openai/gpt-4.1-mini
anthropic/claude-3.5-sonnet
google/gemini-2.5-pro
```

Use whichever model is available in your OpenRouter account. Optional tuning:

```bash
export OPENROUTER_TEMPERATURE="0.8"
export OPENROUTER_MAX_TOKENS="1200"
export OPENROUTER_SITE_URL="https://leadpoet.com"
export OPENROUTER_APP_NAME="Leadpoet Social Brain"
```

### Custom Provider

The generic provider interface remains:

```python
class MyProvider:
    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        ...

def create_provider():
    return MyProvider()
```

Providers that only implement `generate(prompt, *, variants)` still work; Koinoniko simply calls them without the optional model argument.

Custom provider example:

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

## X Write Auth

`X_BEARER_TOKEN` is for reading posts and public metrics. Posting requires user-context write credentials.

Supported env options:

OAuth 2.0 user access token:

```bash
export X_OAUTH2_ACCESS_TOKEN="<user access token with tweet.write>"
```

OAuth 1.0a user context:

```bash
export X_API_KEY="<consumer key>"
export X_API_SECRET="<consumer secret>"
export X_ACCESS_TOKEN="<access token>"
export X_ACCESS_TOKEN_SECRET="<access token secret>"
```

Credentials stay in environment variables only. They are not stored in SQLite.

If write auth is missing, posting commands exit with:

```text
No X write credentials configured. Set X write auth credentials before posting.
```

## Trend Scout

Trend Scout stores timely market/news/product context for generation and planning. The hosted task `refresh-trends` refreshes the top 5 daily and top 5 weekly web trends across AI, sales, and lead generation, then saves them as reusable trend items.

Use it for timely context around:

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

The CLI still supports adding one trend manually when you have a specific source:

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

Publishing an approved queued item also creates a normal `posts` row and keeps the queue row linked through its `draft_id`, trend IDs, X post ID, and X URL.

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

For stored X posts with parseable URLs, you can refresh public metrics from the X API instead:

```bash
python3 -m leadpoet_social_brain.cli sync-x-metrics --post-id "<post id>"
```

`sync-x-metrics` appends a new metric snapshot. `add-metrics` is still useful for manual correction.

Update notes on imported posts after review:

```bash
python3 -m leadpoet_social_brain.cli update-post-notes \
  --post-id "<post id>" \
  --worked-reason "Strong because it says the quiet product truth plainly." \
  --imitate true \
  --hook specific_observation \
  --tone plainspoken
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
- `post_metrics`: measured snapshots for impressions, likes, replies, reposts, quotes, bookmarks, and engagement rate
- `post_notes`: manual notes, why-it-worked rationale, hook, structure, tone, CTA, formatting, pattern, imitation flag
- `post_influences`: links from posts to draft-selected examples, context, patterns, analytics hints, and trends
- `generated_drafts`: draft text, request, explanation, influence labels, structured context snapshot
- `content_queue`: local review/approval/posting queue for planned X content
- `queued_post_versions`: manual and AI edit history for queued posts
- `trend_items`: optional timely market context
- `patterns`: reusable named patterns
- `context_sections`: modular Leadpoet context and style guide sections
- `web_auth_users`: one local web login with salted password hash metadata

The most important linkage:

```text
generated_drafts.id -> posts.source_draft_id -> post_influences -> post_metrics
content_queue.draft_id -> generated_drafts.id -> posts.source_draft_id
content_queue.id -> queued_post_versions.queue_id
```

That lets later metrics be analyzed against the context and trends that shaped the draft.

## CLI Reference

Top-level commands:

```text
init
set-web-login
ingest-x
ingest-x-batch
sync-x-user
sync-x-metrics
update-post-notes
add-context
add-pattern
generate-x
plan-week-x
list-content-queue
update-queued-post
approve-queued-post
approve-week-x
publish-due-x
post-now-x
add-trend
ingest-trends-batch
list-trends
mark-trend-used
publish-draft-x
add-metrics
list-posts
rebuild-style-guide
analytics
weekly-report-x
prompt-x
```

Use built-in help for details:

```bash
python3 -m leadpoet_social_brain.cli --help
python3 -m leadpoet_social_brain.cli set-web-login --help
python3 -m leadpoet_social_brain.cli generate-x --help
python3 -m leadpoet_social_brain.cli sync-x-user --help
python3 -m leadpoet_social_brain.cli sync-x-metrics --help
python3 -m leadpoet_social_brain.cli update-post-notes --help
python3 -m leadpoet_social_brain.cli plan-week-x --help
python3 -m leadpoet_social_brain.cli publish-due-x --help
python3 -m leadpoet_social_brain.cli ingest-x-batch --help
python3 -m leadpoet_social_brain.cli add-trend --help
python3 -m leadpoet_social_brain.web.app --help
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
- provider-backed X import categorization
- X API duplicate prevention and pagination
- `sync-x-user --dry-run`
- missing X API metrics
- `sync-x-metrics` snapshot appends, skipped unresolved posts, and latest-snapshot analytics
- `update-post-notes` field preservation and `imitate=true` workflow
- style guide rebuild quality
- provider-required generation behavior
- OpenRouter provider configuration, request payloads, and response parsing
- storage-backed web login setup and signed session cookies
- login-required UI mutation endpoints and token-protected hosted task endpoints
- weekly X planning into a local review queue
- local UI API queue listing, editing, approval, AI revise, revision acceptance, and safety guards
- trend-required planning guard and evergreen opt-in
- queued post editing, approval, and approval gate before posting
- X write credential failure messaging
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
