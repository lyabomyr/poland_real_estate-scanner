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
- command replies are honest about latency: they arrive within one scan
  interval (15 min), and `/help` + the greeting say so

## Current architecture

Two runtime paths matter:

1. **GitHub Actions scanner** (every 15 min) — one run does both jobs:
   `main.py -> getUpdates (CommandRouter) -> pipeline -> sources -> filters -> dedup -> scoring -> Telegram`
2. **Streamlit dashboard** — reads Turso and edits `chat_configs`

There is deliberately **no always-on server**. Commands are polled once per
run, which is why replies take up to 15 minutes. That trade-off is the whole
reason the project costs nothing to operate — do not "fix" it by adding a
hosted webhook without being asked.

Shared state lives in Turso/SQLite:

- `seen`
- `chat_configs`
- `chat_emissions`
- `greeted_chats`
- processed Telegram `update_id`s

## Repo layout

```text
main.py
scanner/
  chat_config.py
  chat_repo.py
  commands.py
  introspection.py
  pipeline.py
  runtime_config.py
  storage.py
  telegram.py
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

`CommandRouter.process_pending()` drains `getUpdates` once per scan run and
delegates each update to `process_update()`. Keep that split — `process_update`
takes a single update dict, which is what makes the router testable without
network access.

Each `update_id` is claimed via `repo.claim_update()` *before* dispatch, so a
command can never run twice even if a run is retried.

### `/config`, `/urls`, `/decision_tree`

These commands must stay generated from the same effective config and rule
model as the scanner. Do not hardcode a second copy of thresholds, keywords,
source URLs, or scoring weights in command handlers.

## Running and verifying

Minimum verification:

```bash
make lint
make test
```

If you touched parsers, filters, scoring, or delivery flow, also run:

```bash
make dry
```

## Config discipline

`config.example.yml` is authoritative — and it is the **only** config file.
`main.py --config` defaults to it, GitHub Actions passes it explicitly, and
`dashboard/db.py` reads it for the "default from …" captions. Do not
reintroduce a local `config.yml`: that layout existed, silently drifted, and
produced a local scanner that filtered differently from the deployed one.

When you add a knob:

1. document it there
2. make `/config` surface it if it affects search/scoring/delivery
3. ensure env overrides are wired through `scanner/runtime_config.py`

`TG_CHAT_ID` is optional now. Never reintroduce a hard requirement for it in
workflow rendering, startup, or bot replies.

## Deployment discipline

Everything runs on free tiers:

- scanner + command polling: GitHub Actions cron (or manual dispatch)
- state: Turso free tier
- dashboard: Streamlit Community Cloud

### Turso access

The store talks to Turso over its **HTTP API** (`scanner/turso_http.py`),
not the native `libsql-experimental` driver. That driver is a compiled Rust
extension with wheels for a fixed CPython range (cp38-cp313 at 0.0.55); on
anything newer pip attempts a source build that needs Rust + cmake, and it
fails on hosts like Streamlit Cloud (CPython 3.14). For a remote database
the native driver is the same network round trip, so it bought us nothing.

Consequences to respect:

- `TursoConnection` mimics only what we use: `execute`/`fetchone`/`fetchall`/
  `description`/`rowcount`/`commit`/`close`. Extend it rather than reaching
  for a driver.
- Each `execute()` is its own HTTP request, so connection-scoped SQL like
  `changes()` is unreliable — use `cursor.rowcount` (see `claim_update`).
- Keep `requirements.txt` pure-Python; it is what Streamlit Cloud installs.

Do not silently reintroduce:

- a paid or always-on server (Vercel/Cloudflare/VPS) — this was tried and
  removed on purpose; the 15-minute reply latency is accepted
- BZP as an active source — it is a public-procurement board, not a sales
  listing site, and produced ~1 irrelevant hit per month
- `libsql-experimental` or any other compiled dependency — it broke the
  hosted dashboard build once already

## Safety checks before saying done

- no BZP references left in active code/docs/config unless they are clearly
  historical
- duplicate `update_id` handling still works
- `/help` and the greeting still state the up-to-15-minute reply latency
- reply keyboard still contains `/dashboard`, `/config`, `/decision_tree`,
  `/urls`
- long Telegram replies still stay under the 4096-char limit via chunking
- no secrets leak in `/config` or `/urls`
