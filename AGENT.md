# Agent instructions

Read [`README.md`](README.md) first. This file is the engineering delta: what
must stay true when you touch the repo.

## Product bar

The scanner is correct only if all of this stays true:

- listings are actually buyable apartments, not `TBS`, ownership shares,
  basement units, or similar junk
- budget and area thresholds are not inverted or bypassed
- dedup still works across same-source, cross-source, and grouped bulk listings
- Telegram commands reflect the **same effective runtime config** the scanner
  uses
- webhook mode and polling fallback never conflict (`getUpdates` must stay off
  in production when `TG_WEBHOOK_ENABLED=true`)

## Current architecture

Three runtime paths matter:

1. **GitHub Actions scanner**
   `main.py -> pipeline -> sources -> filters -> dedup -> scoring -> Telegram`
2. **Vercel webhook endpoint**
   `api/telegram_webhook.py -> scanner/webhook.py -> CommandRouter`
3. **Streamlit dashboard**
   reads Turso and edits `chat_configs`

Shared state lives in Turso/SQLite:

- `seen`
- `chat_configs`
- `chat_emissions`
- `greeted_chats`
- processed Telegram `update_id`s

## Repo layout

```text
main.py
api/telegram_webhook.py
scripts/manage_telegram_webhook.py
scripts/send_scan_summary.py
scanner/
  chat_config.py
  chat_repo.py
  commands.py
  introspection.py
  pipeline.py
  runtime_config.py
  storage.py
  telegram.py
  webhook.py
  sources/
    otodom.py
    olx.py
    morizon.py
    komornik.py
dashboard/
  app.py
  db.py
  pages/
```

## Important invariants

### Dedup

Keep the three layers separate:

1. same-source strict key: `<source>:<listing_id>`
2. cross-source fuzzy key: `<price>|<round(area)>|<first non-city location token>`
3. per-source aggregation of bulk developer listings

### Multi-tenancy

The scanner is per-chat, not per-run-global:

- every enabled row in `chat_configs` becomes one `ChatContext`
- `seen` is global dedup state
- `chat_emissions` is per-chat delivery state
- a listing already in `seen` still has to be re-filtered for the current chat

### Telegram commands

`CommandRouter` is shared by:

- local polling fallback via `process_pending()`
- production webhook via `process_update()`

Do not fork the command logic into separate implementations.

### `/config`, `/urls`, `/decision_tree`

These commands must stay generated from the same effective config and rule
model as the scanner. Do not hardcode a second copy of thresholds, keywords,
source URLs, or scoring weights in command handlers.

### `/scan`

`/scan` must:

- acknowledge immediately
- dispatch GitHub Actions via `workflow_dispatch`
- stay protected against arbitrary chats/users
- send a completion summary back to Telegram

## Running and verifying

Minimum verification:

```bash
poetry run pyflakes scanner/ main.py api scripts tests
poetry run python -m unittest discover -s tests -v
```

If you touched parsers, filters, scoring, or delivery flow, also run:

```bash
make dry
```

## Config discipline

`config.example.yml` is authoritative.

When you add a knob:

1. document it there
2. make `/config` surface it if it affects search/scoring/delivery
3. ensure env overrides are wired through `scanner/runtime_config.py`

`TG_CHAT_ID` is optional now. Never reintroduce a hard requirement for it in
workflow rendering, startup, or bot replies.

## Deployment discipline

Production scanner:

- GitHub Actions cron/manual dispatch

Production command path:

- Vercel webhook endpoint

Dashboard:

- Streamlit Community Cloud

Do not silently reintroduce:

- a paid always-on server
- production `getUpdates` polling alongside webhook mode
- BZP as an active source

## Safety checks before saying done

- no BZP references left in active code/docs/config unless they are clearly
  historical
- webhook secret validation still works
- duplicate `update_id` handling still works
- reply keyboard still contains `/dashboard`, `/config`, `/decision_tree`,
  `/urls`
- long Telegram replies still stay under the 4096-char limit via chunking
- no secrets leak in `/config` or `/urls`
