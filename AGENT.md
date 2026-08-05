# Agent instructions

You are working on **poland-real-estate-scanner** — a small, self-contained
Python 3.11 project that scans 5 real-estate sources for apartments in
Kraków and pushes matches to a Telegram group. It runs every 15 minutes on
GitHub Actions.

Read [`README.md`](README.md) first for the product-level picture. This file
covers *how* to work on the codebase.

## The product bar

Every listing sent to Telegram must be:

- **Actually buyable**: full ownership on the open market, not a share.
  Search rejects `TBS`, `udział`, `współwłasność`, `suterena`, `półpiwnica`,
  and building-year < 1990.
- **Within budget**: `price ≤ 610 000 PLN`, `area ≥ 39 m²`. Never let a
  refactor accidentally invert these thresholds.
- **De-duplicated**: same listing must not appear twice across runs. Three
  layers of dedup, in this order:
  1. **Same-source strict** — `<source>:<id>` in `seen` table (SQLite).
  2. **Cross-source fuzzy** — `Listing.fuzzy_key = price|int(area)|street/district`.
     Otodom and Morizon versions of the same flat collapse to one Telegram
     message. Persisted across runs via `seen.fuzzy_key`.
  3. **Per-source aggregation** — 30 similar developer units at one street
     roll into a single message via :mod:`scanner.aggregator`.

If a change breaks any of these, the run is broken even if the code passes
type-checks.

## Repo layout

```
main.py                                CLI + dependency wiring; delegates to pipeline.
scanner/
  models.py         Listing + DealScore dataclasses.
  filters.py        ListingFilter — accept/reject rules from config.
  parsing.py        parse_price / parse_area / parse_rooms — shared regex helpers.
  scoring.py        DealScorer + ScoringWeights + ScoringContext.
  aggregator.py     group_listings — folds near-duplicates into ListingGroup.
  format.py         plain + HTML rendering for console and Telegram.
  storage.py        SeenStore — SQLite / libSQL dedup, keyed by "<source>:<id>".
  telegram.py       Bot API client + 429 retry + chat auto-discover + greet.
  chat_config.py    ChatOverride + EffectiveConfig — per-chat overrides.
  chat_repo.py      ChatConfigRepo — CRUD for chat_configs + chat_emissions.
  commands.py       CommandRouter — /help /status /max_price /source /kw etc.
  pipeline.py       MultiChatPipeline — the scan loop, one context per chat.
  sources/
    base.py         BaseSource: page-loop, fetch, session. Subclass this.
    otodom.py       __NEXT_DATA__ (Next.js SSR data).
    olx.py          [data-cy="l-card"] card scraping.
    morizon.py      .card + [data-cy=…] scraping.
    komornik.py     __NUXT_DATA__ (Devalue index-ref format).
    bzp.py          REST API client (overrides scan()).
dashboard/
  app.py            Streamlit main page — KPIs + charts.
  db.py             Cached Turso connection + DataFrame loaders.
  pages/            Streamlit multi-page: listings table, chat editor.
```

## Running & verifying

Common commands (`make help` lists all):

```bash
make install        # poetry install
make dry            # --dry-run: scan every source, print, don't touch DB, don't send
make run            # real run
make reset-db       # rm data/seen.db (safe — the workflow will recreate on next run)
make chats          # print chats the bot has recently seen (getUpdates)
make prune          # archive+delete rejected rows > 90d (monthly cron does this too)
make greet          # announce chat_id in freshly-joined chats (auto-runs before each scan)
```

The scheduled prune (`.github/workflows/prune.yml`) runs on the 1st of every
month, archives to `datasets/YYYY-MM-DD_pruned_rejected.csv`, and commits.
**Only `rejected` rows are pruned** — `matched` / `duplicate` rows are the
ML dataset and must never be silently dropped.

**Every change you make must survive `make dry`.** That's the minimum bar.
Watch the `done: {…}` line: `matched > 0` and no `crashed` in the logs.

**Sanity to check when you change a source parser:**

```bash
poetry run python -c "
import sys; sys.path.insert(0, '.')
import logging; logging.basicConfig(level=logging.INFO)
from scanner.sources.<name> import <Class>
src = <Class>(url='<same url as config>', pages=1, user_agent='Mozilla/5.0', delay=0)
for l in src.scan():
    print(l)
"
```

Look at the printed rows: price/area/location should be non-None for the
common case. If most listings come out with `price=None`, your selector
regressed silently.

## What's in-scope for changes

**In scope:**

- Bug fixes in parsers when a source ships a schema drift.
- Adding a new working source (see the "Adding a new source" section of the
  README).
- Tightening filters — but keep the rejection reasons in `reject_keywords`
  in `config.example.yml` synced with `config.yml` in a way the user can
  understand.
- New Makefile / workflow steps if they simplify verification.

**Out of scope unless asked:**

- Async / concurrency — the whole scan takes ~40 s serially, and per-source
  delays (2 s) exist to be polite to the sites. Speedups aren't worth the
  complexity.
- Alternative dedup stores (Postgres, Redis). SQLite committed via the
  workflow is deliberately dumb and it works.
- Web UI / admin panel. This is a cron job.
- **Scraping KRZ / any Incapsula-protected site**. We tried Playwright +
  stealth; `edet=12` (headless detected). Do not spend cycles on this
  again without paid stealth proxies.

## Code standards

**KISS / DRY.** Concretely, in this codebase:

- **New parser?** Subclass `BaseSource`, implement `_parse(html)`. Do NOT
  reimplement the page loop or `_page_url` — that lives in `BaseSource.scan`.
- **Need to parse "598 955 zł"?** Use `scanner.parsing.parse_price`. Do NOT
  copy-paste regex into a source module. Same for `parse_area` / `parse_rooms`.
- **Add a config option to a source?** Just accept it as a constructor kwarg.
  `build_sources()` already passes every key under `sources.<name>` as
  kwargs — no plumbing needed anywhere else.
- **Persist something new about a listing?** Don't grow the `seen` table
  schema unless it's genuinely needed for the *scanner*'s decision-making.
  For ad-hoc analytics, use ad-hoc SQL over the existing columns.
- **No noise comments.** Docstrings state *why* a module exists and any
  non-obvious design choice. Don't add comments that repeat the code
  (`# increment counter`) or the task history (`# added for issue #42`).

**Never commit:**

- `config.yml` (contains the bot token — gitignored, keep it that way).
- Your local `data/seen.db` (gitignored — the workflow force-adds its own).
- Bot tokens, chat ids, or personal data anywhere in tracked files.

## Scoring

`scanner/scoring.py` computes a 0–100 `DealScore` for every matched listing.
Three signals; **every knob is in the YAML config** — no code constants
worth touching for tuning:

1. **Price / m² vs run median** — ±`weights.price_per_m2` pts, linear over
   `± weights.price_per_m2_full_at` deviation. Median is built from
   persisted matches (via `SeenStore.matched_price_per_m2`) plus the
   current run, so quiet runs still get a stable baseline. Requires at
   least `weights.min_median_sample` samples, else disabled with a log
   line ("keyword-only signals").
2. **Area sweet-spot** — `weights.area_sweet_bonus` pts when
   `area ∈ [area_sweet_min .. area_sweet_max]`.
3. **Keyword bonuses / penalties** — from `positive_keywords` and
   `negative_keywords`. Each entry is either a plain string (uses
   `weights.keyword` as its weight) or `{name: "…", weight: N}` for a
   custom per-keyword override. Same word-prefix matching convention as
   `filters.reject_keywords`.

The scorer emits `reasons` alongside the number ("area sweet-spot",
"+balkon", "-do remontu", "-15% vs median"). Don't hide reasons — if the
number doesn't add up from the reasons list, that's a bug.

Sort keys use `score → price` order so `format_group_html` puts the best
deals at the top of an aggregated group message.

**When adding scoring signals** — first try to express the new signal as
a keyword (or a pair of keywords with custom weights). Only touch
`DealScorer.score()` in Python if the new signal genuinely can't be
expressed as a keyword hit (e.g. it needs a numeric range, a per-source
override, a lookup table). Every code-side addition also needs a matching
entry in `weights` + a comment in `config.example.yml` explaining what it
does and what changing it accomplishes.

## Multi-tenancy

Every row in `chat_configs` (persisted, `enabled=1`, not `paused=True`)
produces one :class:`ChatContext` at scan start and gets its own
end-to-end pass through the pipeline: filter → cross-source dedup →
score → aggregate → emit + `chat_emissions.record_emission`.

The single most common gotcha: **a listing already in `seen`** (from
another chat's earlier run) still needs its filter to be applied for the
current chat — different chats can have different filter overrides, and
`chat_emissions` (not `seen`) is what gates delivery.

Two paths mutate `chat_configs`:

* `CommandRouter.process_pending()` — polls `getUpdates`, dispatches
  `/…` commands, calls `repo.upsert(chat_id, …)`. Idempotent via
  `command_updates` (each `update_id` is stored on first sighting).
* Streamlit form → `upsert_chat_override()`. Same repo method; writes
  the same JSON blob. Both UIs are strictly interchangeable.

## Auto-announce chat_id

Before every scan the pipeline polls `getUpdates` for `my_chat_member`
events with a `left → member` (or admin) transition — that's the signal a
user just added the bot to a chat. For each such chat not already in
`greeted_chats`, `send_greeting()` posts the chat's id back to the chat and
we record the id in the store. The record makes it strictly one greeting
per chat, ever.

The `--greet-chats` flag runs this step alone (no scan). Bakes into `make
greet`. Runs every 15 min anyway as part of the normal scan.

## The three dedup layers — where each is enforced

* **Same-source** (`<source>:<id>`): `store.has(listing.dedup_key)` at the top
  of every source's yield loop in `main.py`. Rejected & matched both persist,
  so once a listing is seen we never re-evaluate it.
* **Cross-source** (`fuzzy_key`): phase 2 of the pipeline in `main.py`. Loads
  `store.emitted_fuzzy_keys()` once, then set-lookup per candidate. Skipped
  when a listing's `fuzzy_key` is `None` (missing price/area/location).
* **Per-source aggregation** (near-duplicates at same street): phase 3 in
  `main.py`, via `scanner.aggregator.group_listings`. Trigger threshold is
  `notifications.min_group_size` (default 3).

If you touch dedup, keep these three separate — mixing them (e.g. fuzzy
matching within one source) would collapse legitimate different flats on the
same street.

## Sources — what to know before touching them

- **Otodom** — schema drift happens (`props.pageProps.data.searchAds.items`
  vs `.data.listing.items`). The extractor already tries several paths;
  if a run comes back empty, add the new path to `_ITEMS_PATHS` in
  `scanner/sources/otodom.py` instead of rewriting.
- **OLX** — cards that link to Otodom are deliberately skipped to avoid
  cross-source duplicates.
- **Morizon** — produces lots of same-street developer bulk listings. That's
  fine — the aggregator collapses them.
- **Komornik** — `openingValue` is the auction *starting bid*, not market
  value. That's what a buyer commits at, so it's the right price field.
  Area is best-effort from the title text and capped at 15–500 m² to avoid
  reading `"0,2200 ha"` as an area.
- **BZP** — mostly public procurement, not sales. If the user asks why
  there are few hits, that's the answer, not a bug.

## The Telegram side

- Bot must be in the target chat as an admin (channels) or a member (groups)
  before `sendMessage` will work.
- `TelegramNotifier._post` handles `429 Too Many Requests` transparently —
  sleeps `retry_after + 1 s` and retries. Don't add your own retry layer.
- Group aggregation sends `disable_web_page_preview=true` deliberately —
  20 URL previews stacked would blow the message up.
- Personal DMs need the user to have `/start`ed the bot at least once,
  otherwise the API returns `Forbidden: bot can't initiate conversation`.

## Config discipline

`config.example.yml` is authoritative. When you add a new source or knob:

1. Add it to `config.example.yml` with a comment explaining what it does.
2. If the user is running locally, they need to re-merge into their own
   `config.yml` — call this out in your response.
3. The GitHub Actions workflow renders `config.yml` from
   `config.example.yml` on every run, substituting only `TG_BOT_TOKEN` and
   `TG_CHAT_ID`. Any new config change lands there automatically.

## Deploy loop

1. User pushes to `main`.
2. `.github/workflows/scan.yml` runs on `*/15 * * * *` and on manual dispatch.
3. Workflow: checkout → Python 3.11 → Poetry install → render config from
   secrets → `poetry run python main.py` → commit `data/seen.db` back.
4. Look at Actions logs: the last line should be
   `done: {'seen': …, 'matched': N, 'sent': N, …}`. If `sent < matched`,
   Telegram delivery failed — check the log lines above for the reason.

## Self-check before saying "done"

Run through this list — genuinely, not as decoration:

- [ ] `make dry` completes with `matched > 0`, no `Traceback`, no `crashed` line.
- [ ] For a source you touched: eyeball the printed listings. Prices and
      areas parsed? URLs valid?
- [ ] `git status` — did I accidentally stage `config.yml` or a stale
      `data/seen.db`? (`.gitignore` should prevent this, but double-check.)
- [ ] `grep -R -l --include='*.py' <name-of-thing-I-removed> .` — no
      dead references left after a rename/removal.
- [ ] If I added a config knob: `config.example.yml` documents it, and the
      user knows they need to update their `config.yml`.
- [ ] If I touched filters: is the "what we ignore" table in the README
      still accurate?
- [ ] I did not introduce a new dependency without a compelling reason.
      New deps show up in `poetry.lock` and the workflow install step —
      each one is a maintenance cost.

## When you're stuck

- **A source stopped returning listings.** Almost always a schema drift.
  Fetch the page manually (`curl -A "Mozilla/5.0" <url> -o /tmp/x.html`),
  open it, find the new selector or JSON path, patch it in one place.
- **Telegram silently doesn't deliver.** Check `chat_id`. Groups start
  with `-100…` (supergroups) or `-…` (basic). Personal DMs need `/start`.
  Run `make chats` to see what `getUpdates` reports.
- **Actions run shows `sent: 0` but plenty matched.** The bot isn't in the
  chat, or the chat_id is wrong, or the token was revoked. Check secrets
  in Settings → Secrets and variables.
- **Actions run shows `already_seen: 219, matched: 0`.** With Turso the DB
  lives cloud-side; run `turso db shell krakow-real-estate 'DELETE FROM seen'`
  to reset. If falling back to local SQLite: delete `data/seen.db`, commit,
  push — the next run recreates it.
- **Local dev is hitting the cloud DB by accident.** Check your shell env —
  `TURSO_URL` / `TURSO_AUTH_TOKEN` route to Turso. Unset both to use local
  `data/seen.db`.

If you're about to write "I *think* this works" without running `make dry`
— go back and run it.
