# Koinoniko

Koinoniko is the local-first Social Brain for Leadpoet.

It stores Gavin's X posts, learns from the ones worth imitating, keeps current company and market context close at hand, and helps generate founder-led X posts that sound personal, specific, and useful.

The product is intentionally simple:

- SQLite storage
- Python standard library runtime
- X first, with platform logic separated for future LinkedIn support
- Login-gated local or hosted web UI
- Provider-backed LLM generation only
- Editable markdown skill files for voice and post-type instructions
- Transparent analytics from stored post performance
- Manual review and approval before anything is posted
- No agent framework, background worker, scraping crawler, or complex orchestration

## Current Capabilities

Koinoniko currently supports:

- importing X examples manually, from CSV, or from the X API
- syncing posts created directly on X back into SQLite
- syncing public X metrics into append-only metric snapshots
- marking strong posts as Voice Examples with "Why this worked" notes
- suggesting "Why this worked" notes for Voice Examples through the configured provider
- updating Koinoniko Voice from selected Voice Examples
- tracking compact Voice Coverage and Active Writing Rules
- generating X drafts through a configured LLM provider
- using OpenRouter as the built-in provider
- routing Standard and Best Version generation to configurable models
- planning a weekly queue of X posts for review
- editing, revising, approving, scheduling, and posting approved queue items
- storing draft-to-post linkage and generation influences
- refreshing AI, sales, and lead generation trends through ScrapingDog
- showing compact trend cards with a short "what happened" summary and deeper source details in the modal
- storing optional Pattern Scout examples for reusable post mechanics
- selecting a trend or saved Pattern Scout mechanic when creating a post
- asking Koinoniko questions about posts, Voice, trends, patterns, queue quality, and what is working
- showing Account connection status without exposing secret values
- running protected Railway Cron tasks for publishing due posts, post sync, metrics sync, trend refresh, and pattern refresh

Generation requires a provider. There is no demo or template fallback.

If `SOCIAL_BRAIN_PROVIDER` is missing, generation fails with:

```text
No LLM provider configured. Set SOCIAL_BRAIN_PROVIDER=module.path:create_provider to generate drafts.
```

## Project Layout

```text
leadpoet_social_brain/
  core/models.py              Platform-neutral data models
  platforms/base.py           Platform adapter protocol
  platforms/x.py              X adapter, URL validation, character limits
  platforms/x_api.py          X API import and public metrics sync
  storage/sqlite_store.py     SQLite schema, migrations, repository functions
  ingestion.py                X import, trend import, publish workflow
  content_queue.py            Weekly planning, queue, approval, posting helpers
  context.py                  Context retrieval and Voice rebuilds
  generation.py               Prompt assembly, provider call, draft validation
  skills.py                   Markdown skill loader
  providers.py                Provider protocol and env loader
  openrouter_provider.py      Built-in OpenRouter provider
  analytics.py                Transparent performance summaries
  trend_scout.py              Web trend refresh and ScrapingDog integration
  pattern_scout.py            Optional Pattern Scout intake and refresh
  cli.py                      Command-line interface
  web/app.py                  Login-gated HTTP API and static UI server
  web/static/                 Single-page web UI
  web/task_client.py          Small client for protected hosted task endpoints

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
  test_trend_scout_scrapingdog.py
```

Core Social Brain logic is platform-neutral. X-specific behavior lives in `platforms/x.py` and `platforms/x_api.py`. LinkedIn should be additive: add a platform adapter, platform skill file, and commands without rewriting storage, context, generation, or analytics.

## Local Setup

Run commands from the repo root:

```bash
cd /Users/gavin/Code/koinonikos
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

Use a separate database for demos or tests:

```bash
python3 -m leadpoet_social_brain.cli --db /private/tmp/social_brain_demo.sqlite3 init
```

Use a shared configured database for CLI and UI:

```bash
export SOCIAL_BRAIN_DB="/app/data/social_brain.sqlite3"
```

Local secrets can live in `.env`:

```bash
cp .env.example .env
set -a; source .env; set +a
```

`.env` is ignored by git. Keep real API keys in `.env` locally and Railway Variables in production.

Initialize the database:

```bash
python3 -m leadpoet_social_brain.cli init
```

## Web UI

Start the web app:

```bash
python3 -m leadpoet_social_brain.web.app --port 8787
```

Open:

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

The UI loads `.env` by default if present. It reads secrets into the process but never displays token values.

### Login

The web UI requires a single SQLite-backed login. Username and salted password hash are stored in SQLite. Passwords are not stored in source code or environment variables.

Create or reset the login:

```bash
python3 -m leadpoet_social_brain.cli set-web-login --username gavinz444
```

The command prompts for the password and stores only a salted PBKDF2 hash. Resetting the password invalidates existing sessions. If no login exists, the app fails closed and shows a configuration-required login page.

### Screens

The UI has four main areas:

- `Dashboard`: daily/weekly trends, daily/weekly patterns, and the full X posts table
- `Queue`: review, edit, AI revise, approve, schedule, and post approved content
- `Voice`: compact status chips, current Koinoniko Voice, selected Voice Examples, and collapsed suggested examples
- `Account`: connection status, data sync actions, model routing, and account/security controls

The Dashboard keeps trend and pattern cards compact. Trend cards show the topic/source, title, and a short summary of what happened. Clicking a trend opens the full detail modal with `What happened`, `Why it matters`, `Leadpoet angle`, suggested hooks, source link, and source details.

Account uses polished status rows for:

- LLM Provider
- OpenRouter
- X Read
- X Write
- ScrapingDog
- Daily Sync

The Account page shows `Connected`, `Not connected`, or `Not configured`. It does not show API keys, tokens, or secret values.

## Ask Koinoniko

Ask Koinoniko is a global assistant opened from the icon launcher in the lower-right corner.

It can answer questions about:

- which posts are strongest as Voice Examples
- which topics, hooks, structures, and formats are working
- what Gavin should post about next
- which queued or scheduled posts feel weak
- recent trends and Pattern Scout mechanics

The drawer starts with a few starter prompts. After a prompt is used, the starter list disappears so the chat stays focused. The composer stays pinned to the bottom, responses render formatted headings/lists/quotes instead of raw markdown, and the expand/narrow control uses the same chevron button style as the sidebar.

Ask Koinoniko is provider-backed and read-only by default. Suggested actions are labeled as confirmation-required; it should not silently mutate posts, Voice Examples, queue items, or settings.

## Voice Workflow

Voice is how Koinoniko learns what Gavin-written posts sound like.

Use it like this:

1. Sync X posts.
2. Open Dashboard.
3. Click a strong post.
4. Check `Use this as a Voice Example`.
5. Add `Why this worked`.
6. Save.
7. Open `Voice`.
8. Click `Update Voice`.

The Voice page shows:

- current Koinoniko Voice summary in clean sections
- compact Voice Coverage by topic and post type
- editable Active Writing Rules grouped by positioning, Voice, review filters, and Writing Guardrails
- selected Voice Examples with one simple performance signal
- suggested Voice Examples behind `Show suggested examples`

Positioning rules include the core Leadpoet framing: Leadpoet is the intent layer for proprietary buyer context, timing signals, and account-level intent. Other tools, agents, or case studies should be treated as market proof that intent matters, then brought back to Leadpoet's wedge. Editing these rules in the Voice modal updates the active generation/review prompts; positioning edits also update the reusable Leadpoet context section.

Voice UI rule: each section should have one visible title only. Do not add eyebrow labels, mini titles, or descriptive subtitles above/below section titles unless the user explicitly asks for them.

Each Voice Example can use `Suggest why this worked` to draft a concise note from the post text, metrics, topic, post type, and current Voice. The suggestion is only placed into the field for review; it is not saved until you click Save.

The Dashboard recent posts table can filter to `Voice Examples`. Clicking a post opens a clean post detail drawer with:

- full post text and an `Open on X` button
- compact metadata: date, topic, post type, and engagement rate
- compact metrics: likes, replies, reposts, quotes, bookmarks, and impressions
- Voice Example checkbox
- `Why this worked` note
- collapsed advanced fields for hook, structure, tone, CTA, formatting, and pattern

Topic and Post type are editable inline inside the post drawer as compact chips. They use existing stored values plus defaults, and new typed values can be saved without clearing the Voice note.

Internally this reuses existing `post_notes` and context storage. The product-facing UI uses Voice language; the older CLI names remain available for compatibility.

## Environment Variables

Common variables:

```bash
SOCIAL_BRAIN_DB="/app/data/social_brain.sqlite3"
SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
OPENROUTER_API_KEY="<openrouter key>"
X_BEARER_TOKEN="<x bearer token for read/import/metrics>"
TASK_SECRET="<long random secret for /tasks/* endpoints>"
SCRAPINGDOG_API_KEY="<scrapingdog key>"
```

ScrapingDog status is connected if either of these is set:

```bash
SCRAPINGDOG_API_KEY="..."
SCRAPING_DOG_API_KEY="..."
```

Optional model env fallbacks:

```bash
SOCIAL_BRAIN_DEFAULT_MODEL="..."
SOCIAL_BRAIN_STRATEGIST_MODEL="..."
SOCIAL_BRAIN_NATURAL_WRITER_MODEL="..."
SOCIAL_BRAIN_EVALUATOR_MODEL="..."
OPENROUTER_MODEL="..."  # legacy default-model fallback
```

Optional OpenRouter tuning:

```bash
OPENROUTER_TEMPERATURE="0.8"
OPENROUTER_MAX_TOKENS="1200"
OPENROUTER_SITE_URL="https://leadpoet.com"
OPENROUTER_APP_NAME="Koinoniko"
```

X write credentials are only needed for posting from the app:

```bash
X_OAUTH2_ACCESS_TOKEN="<user access token with tweet.write>"
```

or OAuth 1.0a:

```bash
X_API_KEY="<consumer key>"
X_API_SECRET="<consumer secret>"
X_ACCESS_TOKEN="<access token>"
X_ACCESS_TOKEN_SECRET="<access token secret>"
```

Secrets stay in environment variables. They are not stored in SQLite.

## OpenRouter And Model Routing

OpenRouter support is built in.

Set:

```bash
export OPENROUTER_API_KEY="<your OpenRouter key>"
export SOCIAL_BRAIN_PROVIDER="leadpoet_social_brain.openrouter_provider:create_provider"
```

Then open `Account -> Model Routing` and set:

- Default model
- Strategist model
- Natural writer model
- Evaluator model

Standard generation uses the default model in one provider call.

Best Version mode uses a simple strategist -> natural writer -> evaluator flow. Role models are optional. If a role is blank, it falls back to the default model. If no Account model is saved, env fallbacks such as `SOCIAL_BRAIN_DEFAULT_MODEL` or legacy `OPENROUTER_MODEL` are used.

Example OpenRouter model IDs:

```text
anthropic/claude-sonnet-4.5
openai/gpt-4.1-mini
google/gemini-2.5-pro
```

The Account page can save model settings. Model names are not secrets and may be stored in SQLite app settings.

### Custom Provider

Any provider can be plugged in with:

```python
class MyProvider:
    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        ...

def create_provider():
    return MyProvider()
```

Then set:

```bash
export SOCIAL_BRAIN_PROVIDER="my_provider_module:create_provider"
```

Providers that only implement `generate(prompt, *, variants)` still work. Koinoniko calls them without the optional model argument.

## Account Sync

Koinoniko can stay synced with posts created directly on X.

In the web UI, open `Account` and use:

- `Sync X Posts`: imports authored posts from `gavinzaentz`
- `Sync X Metrics`: appends fresh public metric snapshots for stored posts
- `Sync All X Data`: runs post sync first, then metric sync
- `Refresh Trends`: updates live trend context when ScrapingDog is connected
- `Sync Patterns`: refreshes Pattern Scout examples when ScrapingDog and the provider are connected

Default X post sync behavior:

- username: `gavinzaentz`
- default topic: `Leadpoet`
- default post type: `founder_insight`
- exclude replies: `true`
- exclude retweets: `true`
- imported posts are not Voice Examples by default

Post sync does not guess why a post worked, hook, structure, tone, CTA, formatting, pattern, or Voice Example status. Add those manually only for posts you want the system to learn from.

Sync metadata is stored under simple keys:

- `x_posts_sync`
- `x_metrics_sync`
- `x_daily_sync`
- `trends_sync`
- `patterns_sync`

Only timestamps, status, JSON summaries, and errors are stored.

In the Account detail modal, the Sync button opens a small action menu with Posts, Metrics, Trends, Patterns, and All. The menu opens upward inside the modal so the options remain visible on smaller screens.

## Railway Deployment

This repo includes `railway.toml`:

```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python -m leadpoet_social_brain.web.railway_entrypoint"
```

Railway provides `PORT` automatically. The app listens on that port in production.
The checked-in `railway.toml` now points at `leadpoet_social_brain.web.railway_entrypoint`, which starts the web UI for the main service and runs one-shot task clients for Railway services named `publish-due-x`, `sync-x-posts`, `sync-x-metrics`, `refresh-trends`, or `refresh-patterns`. If a cron service has a different name, set `SOCIAL_BRAIN_TASK` to one of the supported task names.

All product data lives in SQLite. On Railway, that means X posts, metric snapshots, Voice Examples, Koinoniko Voice, trends, Pattern Scout examples, drafts, queue items, sync metadata, model routing, and the web login persist only when the SQLite file is on the mounted volume.

Required Railway setup:

1. Deploy from the `main` branch of `gzaentz/koinoniko`.
2. Add a Railway Volume.
3. Mount the volume at `/app/data`.
4. Set `SOCIAL_BRAIN_DB=/app/data/social_brain.sqlite3`, or rely on `RAILWAY_VOLUME_MOUNT_PATH=/app/data`.
5. Keep service replicas at `1` while using SQLite.
6. Set `OPENROUTER_API_KEY`.
7. Set `SOCIAL_BRAIN_PROVIDER=leadpoet_social_brain.openrouter_provider:create_provider`.
8. Set `X_BEARER_TOKEN` for X import and public metrics.
9. Set `SCRAPINGDOG_API_KEY` or `SCRAPING_DOG_API_KEY` for live trend refresh.
10. Set `TASK_SECRET` for protected Railway Cron tasks.
11. Set X write credentials only if you want approved posts to post from Railway.
12. Optional: set `SOCIAL_BRAIN_TASK_TOKEN` only if an external scheduler will call older `/api/tasks/*` endpoints.

SQLite notes:

- The app creates the DB parent directory before opening SQLite.
- SQLite opens with WAL mode, a `5000ms` busy timeout, and foreign keys enabled.
- The deploy logs print the active DB path as `Social Brain DB path: ...`.
- Do not write or initialize the DB during build or pre-deploy steps; Railway volumes are mounted at runtime.
- Opening the app safely runs schema creation and migrations at runtime.
- Do not add Postgres for this version.
- Do not store API tokens in SQLite.

After first deploy, run against the mounted DB:

```bash
python -m leadpoet_social_brain.cli init
python -m leadpoet_social_brain.cli set-web-login --username gavinz444
```

If Railway shows `Unexposed service`, open the service settings and generate a public domain after deployment succeeds.

## Railway Cron

Koinoniko does not run a scheduler or background worker. Use Railway Cron or another external scheduler to call protected task endpoints.

Protected endpoints:

```text
POST /tasks/sync-x-posts
POST /tasks/sync-x-metrics
POST /tasks/sync-x-all
POST /tasks/sync-x-daily
POST /tasks/publish-due-x
POST /tasks/refresh-trends
POST /tasks/refresh-patterns
POST /tasks/refresh-patterns-weekly
```

They require:

```text
Authorization: Bearer <TASK_SECRET>
```

Recommended cron setup:

Railway cron expressions are UTC.

```text
X post sync:       every 30 minutes       */30 * * * *
X metrics sync:    every 2 hours           15 */2 * * *
Publish due posts: every 5 minutes        */5 * * * *
Trend refresh:     daily, 06:25 UTC        25 6 * * *
Pattern refresh:   weekly, Mon 06:40 UTC   40 6 * * 1
```

Create Railway Cron services that run and exit:

```bash
python -m leadpoet_social_brain.web.task_client sync-x-posts
python -m leadpoet_social_brain.web.task_client sync-x-metrics
python -m leadpoet_social_brain.web.task_client publish-due-x
python -m leadpoet_social_brain.web.task_client refresh-trends
python -m leadpoet_social_brain.web.task_client refresh-patterns-weekly
```

`publish-due-x` checks approved queue items and posts anything whose scheduled time has passed. Railway Cron has a 5-minute minimum interval, so approved posts can go out up to a few minutes after their scheduled time. `sync-x-posts` is incremental by default: after the first import, it asks X only for posts newer than the latest stored X post. The `refresh-patterns-weekly` task refreshes both weekly and monthly Pattern Scout windows, 5 examples each by default. The default cron payloads also keep post and metric limits modest to avoid repeated backfills.

Set these variables on each Cron service:

```bash
SOCIAL_BRAIN_URL="https://<app-domain>"
TASK_SECRET="<same TASK_SECRET as the web app>"
```

For one simpler hourly job:

```bash
python -m leadpoet_social_brain.web.task_client sync-x-all
```

Daily sync remains available:

```bash
python -m leadpoet_social_brain.web.task_client sync-x-daily
```

Approved scheduled posts still need a trigger to publish. Use the Railway Cron `publish-due-x` task above, run it locally, or call the protected hosted endpoint directly:

```bash
curl -X POST "$SOCIAL_BRAIN_URL/tasks/publish-due-x" \
  -H "Authorization: Bearer $TASK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"limit":20}'
```

The legacy `/api/tasks/publish-due-x` route remains available for external schedulers that still use `SOCIAL_BRAIN_TASK_TOKEN`.

## Hosted Security

The hosted UI is intentionally simple but should stay locked down:

- the app fails closed until a SQLite-backed web login exists
- session cookies are signed, `HttpOnly`, `SameSite=Strict`, and `Secure` on Railway
- UI mutation endpoints require a logged-in session
- protected `/tasks/*` endpoints require `TASK_SECRET`
- older `/api/tasks/*` endpoints require `SOCIAL_BRAIN_TASK_TOKEN`
- API keys and tokens are read from environment variables only
- Account status returns connected/missing booleans and polished labels, never secret values

## First-Run Checklist

For a new hosted database:

1. Add Railway Volume at `/app/data`.
2. Set `SOCIAL_BRAIN_DB=/app/data/social_brain.sqlite3`.
3. Deploy and confirm logs show the expected DB path.
4. Run `init`.
5. Run `set-web-login --username gavinz444`.
6. Set `OPENROUTER_API_KEY`.
7. Set `SOCIAL_BRAIN_PROVIDER`.
8. Set Account model routing.
9. Set `X_BEARER_TOKEN`.
10. Open Account and run `Sync All X Data`.
11. Open Dashboard and mark the strongest posts as Voice Examples.
12. Open Voice and click `Update Voice`.
13. Set `SCRAPINGDOG_API_KEY` or `SCRAPING_DOG_API_KEY`.
14. Run `Refresh Trends`.
15. Set `TASK_SECRET`.
16. Configure Railway Cron tasks.
17. Add X write auth only when you are ready to post from the app.

## Real Workflow

The normal web-first loop:

1. Open Account.
2. Run `Sync All X Data`.
3. Review recent posts on Dashboard.
4. Click strong posts and mark them as Voice Examples.
5. Open Voice and click `Update Voice`.
6. Refresh Trends.
7. Optionally add or refresh Patterns if there are post mechanics worth adapting.
8. Create an individual post from a selected Trend/Pattern or create a weekly plan.
9. Use Ask Koinoniko when you want a quick read on what is working or what feels weak.
10. Review and edit queued posts.
11. Use AI revisions only when useful.
12. Approve posts you are ready to publish.
13. Publish approved items manually or with `publish-due-x`.
14. Sync metrics after posting.
15. Review analytics and repeat.

CLI equivalent:

```bash
python3 -m leadpoet_social_brain.cli init

python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 3200 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight

python3 -m leadpoet_social_brain.cli list-posts --platform x --limit 30

python3 -m leadpoet_social_brain.cli update-post-notes \
  --post-id "<post id>" \
  --worked-reason "Strong because it makes a sharp contrast in plain language." \
  --imitate true \
  --topic "sales LLMs" \
  --post-type founder_insight

python3 -m leadpoet_social_brain.cli rebuild-style-guide

python3 -m leadpoet_social_brain.cli analytics
```

The CLI still uses the original internal names: `--imitate true` means "use this as a Voice Example," and `--worked-reason` means "Why this worked."

## X Import

Use Account `Sync X Posts` for the normal workflow.

Use CLI `sync-x-user` for dry runs, date windows, or optional LLM categorization.

```bash
export X_BEARER_TOKEN="<your bearer token>"

python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --exclude-replies \
  --exclude-retweets \
  --dry-run
```

Import all available authored posts:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 3200 \
  --exclude-replies \
  --exclude-retweets \
  --default-topic "Leadpoet" \
  --default-post-type founder_insight
```

Optional LLM categorization classifies only `topic` and `post_type`:

```bash
python3 -m leadpoet_social_brain.cli sync-x-user \
  --username gavinzaentz \
  --limit 100 \
  --exclude-replies \
  --exclude-retweets \
  --auto-categorize
```

The importer:

- resolves username to X user ID
- calls the X API v2 user posts endpoint
- supports pagination and `--limit`
- avoids duplicates by X post ID or canonical URL
- imports available public metrics
- does not store `X_BEARER_TOKEN`
- does not infer why a post worked
- does not mark imported posts as Voice Examples by default

Private analytics such as URL clicks and profile clicks are not included.

CSV import remains available:

```bash
python3 -m leadpoet_social_brain.cli ingest-x-batch --csv data/x_posts.csv
```

CSV columns:

```text
text,url,date,author,topic,post_type,impressions,likes,replies,reposts,quotes,bookmarks,worked_reason,hook,structure,tone,cta,formatting,pattern,imitate
```

Long X examples are accepted during ingestion. Character limits apply during generation and posting, not storage.

## X Metrics

Refresh public metrics for stored X posts:

```bash
python3 -m leadpoet_social_brain.cli sync-x-metrics --since-days 90
python3 -m leadpoet_social_brain.cli sync-x-metrics --post-id "<post id>"
python3 -m leadpoet_social_brain.cli sync-x-metrics --dry-run
```

Metrics sync:

- finds stored X posts with `x_post_id` or parseable X URLs
- fetches latest public metrics
- appends a new metric snapshot
- does not overwrite old snapshots
- preserves `metrics_collected_at`
- skips unresolved posts cleanly
- reports top movers when enough history exists

Manual correction is still available:

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

Analytics uses the latest metric snapshot by default.

## Voice Examples From CLI

The web UI is the preferred way to teach Voice. The CLI remains available for batch or scripted updates.

Mark a strong post as a Voice Example:

```bash
python3 -m leadpoet_social_brain.cli update-post-notes \
  --post-id "<post id>" \
  --worked-reason "Strong because it says the product truth plainly." \
  --imitate true \
  --hook specific_observation \
  --structure enemy_then_better_model \
  --tone plainspoken \
  --cta none \
  --formatting single_paragraph \
  --pattern enemy_then_better_model
```

Missing fields preserve existing values.

Update Koinoniko Voice from selected examples:

```bash
python3 -m leadpoet_social_brain.cli rebuild-style-guide --limit 12
```

Koinoniko Voice is meant to improve writing taste, not turn the workflow into a tagging chore.

## Generation

Print the prompt without generating:

```bash
python3 -m leadpoet_social_brain.cli prompt-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "AI SDR context beats volume" \
  --use-trends
```

Generate drafts:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer context beats AI SDR volume" \
  --use-trends \
  --variants 3
```

Generate one higher-quality final draft:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer context beats AI SDR volume" \
  --use-trends \
  --best-version
```

Use `--max-chars` only when you want to override the default X length behavior:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "Bittensor" \
  --post-type bittensor_update \
  --angle "subnets need measurable quality signals" \
  --max-chars 280
```

Generation uses:

- exact high-performing examples
- human notes on why posts worked
- Koinoniko Voice
- modular Leadpoet context
- optional trends
- optional Pattern Scout mechanics
- analytics hints when enough data exists
- markdown skills
- review filter

Drafts store structured influence metadata instead of relying on one giant prompt as the source of truth.

In the web UI, `Create Post` includes:

- topic and post type combo inputs
- optional schedule time
- `Trend to build from`
- `Pattern to use`
- selected source chips that can be removed before generation

Create Post uses the Best Version pipeline by default. A hidden strategist first turns the
user's idea and selected context into a structured Post Intent, including the thesis,
concrete detail, post shape, and evidence boundary. The natural writer drafts from that
intent, and the evaluator rewrites once if the result is generic, unsupported, or too
polished. The user only reviews the final queued post, not the internal brief.

`Use in Post` on a trend or `Use Pattern` on a pattern opens Create Post with that source preselected. Generated output still enters the review queue; it does not publish automatically.

## Weekly Content Queue

Create a reviewable weekly queue:

```bash
python3 -m leadpoet_social_brain.cli plan-week-x \
  --week-start 2026-06-29 \
  --posts 7 \
  --use-trends \
  --trend-window-days 14 \
  --topic-mix "sales LLMs:3,Bittensor:2,product:2" \
  --cadence daily_mornings \
  --default-post-type founder_insight \
  --best-version
```

If `--use-trends` is passed and there are no recent unused trends, planning fails clearly instead of inventing news. Use `--allow-evergreen` when evergreen posts are acceptable.

Review, edit, revise, and approve:

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

Nothing posts unless the queue item is approved.

Post approved content:

```bash
python3 -m leadpoet_social_brain.cli post-now-x --queue-id "<queue id>"
python3 -m leadpoet_social_brain.cli publish-due-x
```

## Trends

Trend Scout stores timely context for generation and planning.

The Dashboard trend cards are intentionally compact:

- topic/source metadata
- title
- short "what happened" summary clamped to two lines
- `Use in Post`

Clicking a trend opens the full detail modal. The modal keeps the richer context:

- What happened
- Why it matters
- Leadpoet angle
- Suggested hook
- Source link
- Source details and enrichment status

Trend refresh summaries can include:

- `daily`: daily trends stored
- `weekly`: weekly trends stored
- `enriched`: trends with full LLM-enriched context
- `partial`: trends saved with basic/source context but not full enrichment
- `failed`: trends that could not be saved or enriched

Refresh live trends through ScrapingDog:

```bash
python -m leadpoet_social_brain.web.task_client refresh-trends
```

or from Account with `Refresh Trends`.

Manual trend import is also available:

```bash
python3 -m leadpoet_social_brain.cli add-trend \
  --title "AI SDR tools shift from volume to buyer context" \
  --source-url "https://example.com/ai-sdr-context" \
  --source-type news \
  --topic "sales LLMs" \
  --summary "More teams are questioning high-volume AI SDR workflows." \
  --why-it-matters "Buyers are tuning out generic automation and rewarding timely context." \
  --leadpoet-angle "Leadpoet can argue that sales AI needs buyer timing, not just generated copy." \
  --suggested-hooks "The AI SDR debate is really a context debate." \
  --urgency high
```

Batch import:

```bash
python3 -m leadpoet_social_brain.cli ingest-trends-batch --csv data/trends.csv
```

List and mark trends:

```bash
python3 -m leadpoet_social_brain.cli list-trends --status unused --limit 10
python3 -m leadpoet_social_brain.cli mark-trend-used --trend-id "<trend id>"
```

When a generated draft uses trends and is later published, trend influences are preserved for later analytics.

## Pattern Scout

Pattern Scout stores optional source examples for reusable post mechanics. It is not a separate content system and it does not replace Voice. Use it when you see a post structure worth adapting.

The rule is:

```text
Use the structure, not the wording.
```

The Dashboard shows weekly and monthly Pattern cards next to Trends. The scheduled Pattern Scout refresh stores separate weekly and monthly examples so the two views can diverge. Pattern cards stay compact and use sensible placeholders when a field is missing. Clicking a Pattern opens its source text, structure, why it worked, adaptation rule, source link, and Archive/Ignore actions. `Use Pattern` opens Create Post with that pattern selected; generated output still goes to Queue as `needs_review`.

Manual add:

```bash
python3 -m leadpoet_social_brain.cli add-pattern-example \
  --source-url "https://x.com/example/status/123" \
  --source-author "Example" \
  --source-text "Source post text..." \
  --topic "sales LLMs" \
  --category sales \
  --pattern-name "Weak default teardown" \
  --hook-style "names the weak default" \
  --structure "weak default, concrete counterexample, practical replacement" \
  --why-it-worked "It turns a vague market claim into an obvious product point." \
  --adaptation-rule "Use the structure, not the wording." \
  --window weekly
```

Batch import:

```bash
python3 -m leadpoet_social_brain.cli ingest-patterns-batch --csv data/patterns.csv
```

Useful CSV columns:

```text
source_url,source_author,source_text,source_summary,topic,category,pattern_name,hook_style,structure,pacing,tone,formatting,why_it_worked,adaptation_rule,engagement_notes,likes,replies,reposts,quotes,bookmarks,impressions,collected_at,window,status
```

List or refresh:

```bash
python3 -m leadpoet_social_brain.cli list-patterns --window weekly
python3 -m leadpoet_social_brain.cli refresh-patterns --window weekly --limit 5
```

`refresh-patterns` requires both ScrapingDog and an LLM provider. If ScrapingDog is missing, it fails clearly and manual add/import still work.

Use patterns during generation only when helpful:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why account context beats generated email volume" \
  --use-patterns
```

You can pin a specific pattern:

```bash
python3 -m leadpoet_social_brain.cli generate-x \
  --topic "sales LLMs" \
  --post-type founder_insight \
  --angle "why buyer timing matters" \
  --pattern-id "<pattern id>"
```

When a generated draft uses Pattern Scout and is later published, the pattern name, hook style, and structure are preserved as influences for later analytics.

## Publishing And Feedback Loop

Generated drafts are stored in `generated_drafts`.

Each draft stores:

- request fields
- text
- explanation
- selected examples
- selected patterns
- selected context sections
- analytics hints
- selected trends
- skill file paths and hashes
- generation mode/provider

Publishing a draft creates a published post with `source_draft_id`:

```bash
python3 -m leadpoet_social_brain.cli publish-draft-x \
  --draft-id "<draft id>" \
  --url "https://x.com/gavin/status/<id>" \
  --date "2026-06-25" \
  --author "Gavin"
```

List stored posts:

```bash
python3 -m leadpoet_social_brain.cli list-posts --platform x --limit 20
```

Weekly report:

```bash
python3 -m leadpoet_social_brain.cli weekly-report-x --since-days 7
```

## Analytics

Run:

```bash
python3 -m leadpoet_social_brain.cli analytics
```

Analytics reports:

- human-readable hints
- post type performance
- topic performance
- pattern performance
- hook performance
- structure performance
- tone performance
- CTA performance
- formatting performance
- trend topic/source/urgency performance
- top posts by metric

Rows include count, average engagement rate, average impressions, average bookmarks, average replies, average reposts, average total engagements, and top post ID.

Analytics should guide future writing without making the system feel like a tagging/admin tool.

## Skills

Reusable writing instructions live in markdown:

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

1. global voice
2. platform skill
3. post-type skill when present
4. review filter
5. examples, context, analytics hints, trends, and optional Pattern Scout mechanics from SQLite

Edit these markdown files when the voice or post-type behavior needs to change.

## Data Model

Important SQLite tables:

- `posts`: platform, text, URL, author, topic, post type, status, optional `source_draft_id`
- `post_metrics`: append-only metric snapshots
- `post_notes`: Voice Example flag, Why this worked notes, and optional labels
- `post_influences`: links published posts to draft influences
- `generated_drafts`: generated text and structured context snapshot
- `content_queue`: review/approval/posting queue
- `queued_post_versions`: manual and AI edit history
- `trend_items`: timely market context
- `patterns`: reusable named patterns
- `pattern_examples`: Pattern Scout source examples and reusable mechanics
- `context_sections`: Leadpoet context and Koinoniko Voice sections
- `web_auth_users`: single local web login
- `sync_metadata`: X and trend sync status summaries
- `app_settings`: saved model routing

Key linkage:

```text
generated_drafts.id -> posts.source_draft_id -> post_influences -> post_metrics
content_queue.draft_id -> generated_drafts.id -> posts.source_draft_id
content_queue.id -> queued_post_versions.queue_id
pattern_examples.id -> generated_drafts.context_snapshot -> post_influences
```

This lets later metrics be analyzed against the examples, context, trends, and patterns that shaped the post.

## Web API Notes

The web UI uses small JSON endpoints for the current workflow:

```text
GET /api/taxonomy/topics
GET /api/taxonomy/post-types
PATCH /api/posts/:id
POST /api/posts/:id/voice
GET /api/account/status
POST /api/account/sync-x-posts
POST /api/account/sync-x-metrics
POST /api/account/sync-x-all
GET /api/assistant/starter-prompts
POST /api/assistant/chat
GET /api/voice/coverage
GET /api/voice/avoid-phrases
POST /api/voice/avoid-phrases
POST /api/voice/examples/:id/suggest-note
GET /api/trends
POST /api/trends
GET /api/trends/:id
POST /api/trends/refresh
GET /api/patterns
POST /api/patterns
GET /api/patterns/:id
POST /api/patterns/:id/archive
POST /api/patterns/:id/ignore
POST /api/patterns/refresh
```

The topic and post type option endpoints return distinct values from stored posts and queue items plus sensible defaults. They do not create a separate management system.

`PATCH /api/posts/:id` can update topic and post type without changing the Voice Example flag or `Why this worked` note.

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
add-pattern-example
ingest-patterns-batch
list-patterns
refresh-patterns
generate-x
plan-week-x
list-content-queue
update-queued-post
revise-queued-post
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

Use built-in help:

```bash
python3 -m leadpoet_social_brain.cli --help
python3 -m leadpoet_social_brain.cli sync-x-user --help
python3 -m leadpoet_social_brain.cli generate-x --help
python3 -m leadpoet_social_brain.cli plan-week-x --help
python3 -m leadpoet_social_brain.web.app --help
python3 -m leadpoet_social_brain.web.task_client --help
```

## Tests

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Compile check:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/social_brain_pycache python3 -m compileall leadpoet_social_brain tests
```

Current tests cover:

- ingestion and batch import
- long X examples
- X API normalization, pagination, duplicate prevention, and dry runs
- metrics sync snapshots and latest-snapshot analytics
- Voice Example note workflows
- Voice updates from selected examples
- Voice suggested notes, coverage, and Active Writing Rules
- Voice page and post detail drawer UI expectations
- Ask Koinoniko context, starter prompts, read-only chat behavior, source display, and drawer UI expectations
- topic and post type option endpoints
- Create Post source selectors for trends and patterns
- skill loading and prompt assembly
- provider-required generation
- OpenRouter provider behavior
- Best Version model routing
- trend storage, refresh, and ScrapingDog behavior
- web login and signed sessions
- Account status and sync endpoints
- Account sync menu placement inside the detail modal
- protected task endpoints
- weekly planning and content queue workflow
- X write credential safety
- analytics dimensions

## Design Principles

- Examples teach taste.
- Context teaches what matters now.
- Trends add timeliness without inventing news.
- Analytics should be useful and optional.
- Labels support learning, but should not dominate writing.
- Markdown skill files should replace hardcoded voice rules.
- Provider-backed generation is the real quality path.
- Manual review comes before publishing.
- LinkedIn expansion should be additive, not a rewrite.
