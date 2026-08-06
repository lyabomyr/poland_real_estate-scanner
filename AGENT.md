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
- command replies are honest about latency. They arrive on the next
  scheduled run, and that is **not** 15 minutes: GitHub throttles free
  scheduled workflows and drops most of them (measured: 14 runs in 24 h,
  average gap 115 min, worst 226). `/help` and the greeting must say so and
  point at "Run workflow" — a bot that looks broken for two hours is worse
  than one that admits it is slow.

## Current architecture

Two runtime paths matter:

1. **GitHub Actions scanner** (cron asks for 15 min, GitHub gives ~2 h) —
   one run does both jobs:
   `main.py -> getUpdates (CommandRouter) -> pipeline -> sources -> filters -> dedup -> scoring -> Telegram`
2. **Streamlit dashboard** — reads Turso and edits `chat_configs`

There is deliberately **no always-on server**. Commands are polled once per
run, which is why replies are slow. That trade-off is the whole reason the
project costs nothing to operate — do not "fix" it by adding a hosted webhook
without being asked.

Do not re-tighten the cron either. `*/15` was already tried and GitHub simply
dropped 87% of the runs; asking more often does not get more. The minutes are
offset off `:00` on purpose (GitHub's busiest moment) — keep them offset.

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
make dry            # full scan against live portals, sends nothing
make integration    # 27 live cases: portals, database, bot commands (~3 min)
```

### The test plan

[`TEST_PLAN.md`](TEST_PLAN.md) holds 51 cases across nine risk areas (R1
discovery … R9 observability). Read it before changing delivery, filtering or
dedup — it records *why* each case exists, and most of them exist because the
thing they guard actually broke.

Add a case whenever you fix a bug where **the system reported success while
dropping data**. That is the failure mode this project keeps producing, and
the only one that cannot be noticed in normal use:

- an oversized group message Telegram rejected → 220 listings unreachable
- a run killed mid-delivery → 647 listings stranded, re-stranded every run
- a keyword deleted from the config → the stored row still said `rejected`
- listings with no price → bypassed `max_price` entirely
- the delivery queue ignored the chat's own config → a tightened budget
  didn't apply to what was already queued
- a bot handler raising → the router catches it and the user gets silence

A crash is fine; it gets noticed and fixed. Silence is what costs the user a
flat they wanted.

### A failed workflow run is usually not a bug

Before investigating a red run, check *where* it failed. Two GitHub-side
modes look alarming and mean nothing about this repo — the job never got as
far as running Python:

```
Set up job:  Failed to resolve action download info. Error: Service Unavailable
             -> GitHub could not serve actions/checkout. Nothing ran.

Annotations: The job was not acquired by Runner of type hosted
             -> no runner was ever assigned. The duration shown (e.g. 15m 3s)
                is GitHub retrying, not our timeout-minutes.
```

Both happened on 2026-08-06 (#26, #27) and cost nothing: deliveries continued
on the next scheduled run and the queue kept draining (508 -> 108 across the
day). That is the delivery backlog doing its job — a skipped run is free,
because what to send is read from the database, not from what a scan saw.

So: do **not** raise `timeout-minutes`, add retries, or restructure the
workflow in response to these. A run that genuinely overruns looks different
— it reaches the "Run scanner" step and its logs show the scan working.
Healthy runs take 2-3 minutes.

## Config discipline

`config.yml` is authoritative, tracked in git, and the **only** config file.
`main.py --config` defaults to it, both workflows pass it explicitly, and
`dashboard/db.py` reads it for the "default from …" captions.

Do not reintroduce a second, gitignored config file. That layout existed
(`config.yml` local + `config.example.yml` deployed) and silently drifted:
the local copy hard-rejected `z lat 60`, pinned every source URL so `city:`
did nothing, and enabled a source that had been deleted — so the scanner on
the developer's machine filtered differently from the one in production, and
the dashboard's "defaults" described neither.

Secrets never go in this file. They come from the environment
(`TG_BOT_TOKEN`, `TURSO_*`, `DASHBOARD_URL`) via `scanner/runtime_config.py`
— which is what makes a single tracked config safe.

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
