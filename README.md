# Poland real-estate scanner

Cron-driven scanner for buying apartments in Kraków under a fixed budget.
Runs every 15 min on GitHub Actions, checks 5 real-estate sources,
deduplicates, filters, scores, and pushes fresh matches to Telegram —
**one message per apartment, per interested chat**.

- **Live sources**: Otodom, OLX, Morizon, licytacje.komornik.pl (bailiff
  auctions), BZP / eZamówienia (public-procurement API)
- **Multi-tenant**: many chats, each with its own filter/keyword/source
  overrides. Add the bot to a new group → it self-registers → tune the
  chat's config via `/set` commands in the group or the Streamlit UI.
- **UI**: Streamlit dashboard on Streamlit Community Cloud — read-only
  KPIs + charts + per-chat override editor.
- **Cost**: $0 — Turso free tier, Streamlit Cloud free tier, GitHub
  Actions unlimited minutes on public repos.
- **Persistence**: hosted libSQL (Turso) with local-SQLite fallback for dev.

## What we look for

Defaults in [`config.example.yml`](config.example.yml):

```yaml
search:
  min_area: 39         # m²
  max_price: 610000    # PLN
  min_build_year: 1990 # skip only if the source actually reports a build year
```

## What we ignore (reject filter)

If any of these words hits `title + description + location`, the listing is
dropped — recorded as rejected in the seen-store so we don't re-evaluate it
every 15 min forever:

| category | keywords | why |
|---|---|---|
| social housing | `TBS`, `T.B.S` | not sold on the open market |
| below-ground | `suterena`, `półpiwnica`, `polpiwnica`, `poziom -1`, `piwnica mieszkalna` | basement units |
| **fractional ownership** | `udział`, `udzial`, `współwłasność`, `wspolwlasnos` | bailiff auctions frequently sell 1/2 / 1/6 shares — we want a whole flat, not a share of one |

Additionally, listings are rejected when:

- `price > max_price` (only if the source published a price)
- `area < min_area` (only if the source published an area — komornik often
  doesn't, so their listings pass the area gate on `None`)
- `build_year < min_build_year` (only when known)

Filters live in [`scanner/filters.py`](scanner/filters.py); the "only when
known" rule is deliberate — if we required every field to be present, we'd
throw away every komornik listing.

## Sources — coverage & mechanics

| source | how | notes |
|---|---|---|
| **Otodom** | Parses the Next.js `__NEXT_DATA__` blob. Structured, stable. | primary source, ~70 hits/scan |
| **OLX** | Parses `[data-cy="l-card"]` cards. OLX cards that link to `otodom.pl` are skipped (dedup against the Otodom scan). | ~15 hits/scan |
| **Morizon** | Parses SSR HTML — `.card` elements with `[data-cy=…]` attrs. | ~35–70 hits/scan (developer bulk listings — see aggregation below) |
| **Komornik** (`licytacje.komornik.pl`) | Parses `__NUXT_DATA__` (Devalue index-ref format). Bailiff-forced sales — apartments below market value. | ~20 hits/scan, prices are auction *starting bids* |
| **BZP** (`ezamowienia.gov.pl/mo-board/api/v1/Notice`) | Official REST API per *Załącznik 3 – Instrukcja integracji z API BZP*. Mostly public procurement, so few genuine apartment hits. | 0–2 hits/scan; useful net for public-body sales |

### What we tried and dropped

- **KRZ (`krz.ms.gov.pl`)** — mandatory since Dec 2021 for all
  bankruptcy-trustee (syndyk) notices. Blocked by Incapsula bot-shield;
  even Playwright + `playwright-stealth` returns `edet=12` (headless
  detected). Would need a paid stealth proxy — not compatible with free CI.
- **listaprzetargow.pl** — the monitoring page is fully JS-rendered; SSR HTML
  is empty. Removed.
- **aukcjesyndyka.pl / syndyk.pl / syndyk.com.pl / e-syndyk.pl / …** — all
  parked or dead. The private-aggregator syndyk niche collapsed after KRZ
  centralised bankruptcy notices.

## Deal-quality scoring

Every listing gets a rule-based 0–100 score with a human-readable breakdown:

```
[morizon] 2-pokojowe mieszkanie z balkonem po remoncie
598 955 zł • 41 m² • 14 610 zł/m²
★ 73/100 (-12% vs median, area sweet-spot, +balkon, +po remoncie)
📍 Centralna, Czyżyny, Kraków, małopolskie
```

Signals (see [`scanner/scoring.py`](scanner/scoring.py)):

| signal | weight | notes |
|---|---|---|
| **price / m² vs run median** | ±25 pts | Median is computed from all persisted matches + this run; needs ≥ 10 samples, else falls back to keyword-only |
| **area sweet-spot** (40–60 m²) | +5 pts | Most liquid segment |
| **positive keywords** | +3 each | `balkon`, `taras`, `garaż`, `wind`, `klimatyzac`, `przynależn`, `po remoncie`… (tunable in config) |
| **negative keywords** | -3 each | `do remontu`, `parter`, `ostatnie piętr`, `bez windy`… |

Scores drive **sort order**: within an aggregated group, best-scoring listings
come first, so the top link in a "28 similar" message is the best deal. Score
and its reasons are included in both console and Telegram output.

Keywords are matched as **prefixes on a word boundary** (Polish is heavily
inflected — `balkon` catches `balkonem`, `balkony`, `balkonu`). When the
inflection changes the root vowel (`winda → windą`), list the shorter stem
(`wind`) — the config comments call this out for each keyword.

## Cross-source deduplication

The same apartment often shows up on Otodom, OLX AND Morizon at once — with
different listing ids but the same price, area and street. To avoid three
notifications for one flat, every :class:`Listing` computes a **fuzzy key**:

```
fuzzy_key = "<price>|<int(area)>|<first-location-part>"
```

Sources are checked in registry order (`otodom → olx → morizon → komornik → bzp`)
— the first source to emit for a given fuzzy key wins; subsequent matches are
persisted with `status='duplicate'` and never notified. Key is `NULL` (so
dedup is skipped) when price / area / location is missing, so komornik
listings with unpublished areas still come through fine.

Cross-source dedup is **persistent** — matched fuzzy keys survive in
`data/seen.db` across runs. If Otodom emits an apartment today and Morizon
picks up the same listing tomorrow, tomorrow's Morizon match is skipped
silently.

Real-world numbers from one live run: 179 raw hits → 16 rejected by filters
→ **9 cross-source duplicates skipped** → 154 emitted (further collapsed by
per-source aggregation below).

## Aggregation of similar listings

When a developer lists every unit in a new building as a separate ad (28
Prokocim units at 39–42 m², 531k–597k PLN), we roll them all into one
Telegram message:

```
[morizon] 28 similar listings — Prokocim, Bieżanów-Prokocim
1. 531 742 zł · 39 m² · otwórz
2. 553 420 zł · 39 m² · otwórz
…
28. 596 930 zł · 42 m² · otwórz
```

Group key: first two comma-separated parts of the location (`street, district`).
Threshold: `notifications.min_group_size` (default `3`). Below the threshold,
listings go out as individual messages. Aggregation is **per source** — an
Otodom match and a Morizon match at the same address stay separate on purpose
(the same apartment on two platforms is useful signal, not noise).

## Multi-tenancy — many chats, each with its own config

Each chat that has the bot as a member gets a row in the `chat_configs`
table. The row's `config` column is a JSON blob (:class:`ChatOverride`)
listing the fields that chat **overrides** on top of the YAML baseline —
`max_price`, `min_area`, disabled sources, custom source URLs, extra
positive/negative/reject keywords, scoring weights, `paused` flag.

The scanner builds one effective config per chat at the start of every
scan (see `scanner/chat_config.py :: EffectiveConfig`) and runs the full
pipeline per chat: filter → cross-source dedup → score → aggregate →
emit. Per-chat emission is tracked in `chat_emissions` so a listing that
was sent to chat A won't be resent when chat B goes live later.

Two UIs to tune a chat's overrides:

* **Telegram commands** — send `/help` in the chat, all commands are
  listed. Highlights: `/max_price 700000`, `/min_area 45`,
  `/source olx off`, `/source otodom url https://…`, `/kw + balkon`,
  `/kw - do remontu 6`, `/pause`, `/resume`, `/status`, `/reset all`.
  Commands are processed on the next scan (≤ 15 min cron latency);
  every reply is delivered by the bot.
* **Streamlit dashboard** — [`dashboard/`](dashboard/README.md) has a
  form for every override, plus a raw-JSON preview of what actually gets
  persisted. Also shows KPIs, source mix, reject-reason breakdown,
  price/m² distribution, per-chat delivery counts, and a sortable
  listings table.

## Streamlit dashboard

See [`dashboard/README.md`](dashboard/README.md) for local run + deploy
instructions. In short:

```bash
TURSO_URL=… TURSO_AUTH_TOKEN=… poetry run streamlit run dashboard/app.py
```

For Streamlit Community Cloud (free) — set `dashboard/app.py` as entry,
add the two `TURSO_*` values as secrets in the app settings, deploy.

## Auto-announce chat_id on join

When you add `@KrakowFlatsBot` to a new group, the next scheduled scan (or
`make greet`) posts a message into that group with its `chat_id`:

```
👋 Kraków flats scanner is here.

Chat ID: -5406344287

Save this as TG_CHAT_ID in the repo's Settings → Secrets and
variables → Actions to route apartment matches to this chat.
```

Announced chats are recorded in the `greeted_chats` table so the message
fires **at most once per chat** — running `make greet` again is a no-op.
Beats installing a third-party bot just to read the id.

## Cleanup / archival

Rejected rows accumulate quickly (Otodom returns ~180 items every 15 min,
most already past the filter). After a few weeks they add nothing — the same
listings won't come back, and even if they did the filter would reject them
again. To keep the hot DB lean:

```bash
make prune                   # archive-then-delete rejected rows > 90 days old
```

Config knobs (defaults in `config.example.yml`):

```yaml
storage:
  prune_rejected_days: 90    # threshold
  archive_dir: ./datasets    # where the CSV dump goes
```

**Only `status='rejected'` rows are pruned**. `matched` and `duplicate` rows
are kept **forever** — they're the ML dataset. Before deletion the doomed rows
are appended to `datasets/<YYYY-MM-DD>_pruned_rejected.csv` (full column dump,
recoverable), then `VACUUM` reclaims disk space on SQLite.

There's a scheduled workflow at
[`.github/workflows/prune.yml`](.github/workflows/prune.yml) that runs on the
**1st of every month at 03:00 UTC** and commits the archive back to the repo.
Manual dispatch via **Actions → prune → Run workflow** any time.

## Storage — local SQLite or Turso cloud

[`scanner/storage.py`](scanner/storage.py) picks the backend at runtime from
env vars:

| Env vars present            | Backend                                       |
|-----------------------------|-----------------------------------------------|
| `TURSO_URL` + `TURSO_AUTH_TOKEN` | Hosted libSQL (Turso). No local file.    |
| *(neither set)*             | Local `sqlite3` file at `storage.db_path`.    |

Local dev normally leaves both unset — you don't want to burn cloud
reads/writes while iterating on parsers. The GitHub Actions workflows set them
via secrets so the cron scan writes to Turso and no commit-back is needed.

### Turso quick-start

```bash
# 1. Install the Turso CLI (macOS)
brew install tursodatabase/tap/turso

# 2. Auth + create the database
turso auth signup
turso db create krakow-real-estate

# 3. Get the two values you need
turso db show krakow-real-estate --url                    # → TURSO_URL
turso db tokens create krakow-real-estate --expiration none  # → TURSO_AUTH_TOKEN
```

Save both as **repository secrets** (Settings → Secrets and variables →
Actions → New repository secret): `TURSO_URL` and `TURSO_AUTH_TOKEN`. Both
scan and prune workflows already pass them through — no workflow edit
needed.

Free tier: 9 GB storage / 1 B row reads per month — years of runway for
this workload.

### Inspecting the cloud DB from your laptop

```bash
turso db shell krakow-real-estate     # sqlite3-style REPL against the cloud DB
turso db shell krakow-real-estate 'SELECT source, COUNT(*) FROM seen GROUP BY 1'
turso db dump krakow-real-estate      # full SQL dump for offline analysis
```

## Dedup

Everything the scanner has ever *seen* is stored in SQLite at
`storage.db_path` (default `./data/seen.db`), keyed as
`<source>:<listing_id>`. Table `seen` also records `url`, `title`, `price`,
`area`, `status` (`matched` / `rejected` / `duplicate`), `reject_reason`,
`fuzzy_key`, and `first_seen_at` — useful for ad-hoc audits:

```bash
sqlite3 data/seen.db 'SELECT reject_reason, COUNT(*) FROM seen WHERE status="rejected" GROUP BY 1 ORDER BY 2 DESC;'
sqlite3 data/seen.db 'SELECT source, COUNT(*) FROM seen GROUP BY 1;'
```

Delete the file (`make reset-db`) to force a fresh scan.

## Local development

Requires [Poetry](https://python-poetry.org/) (`brew install poetry` or `pipx install poetry`).

```bash
make install                 # poetry install
# edit config.yml — fill telegram.bot_token / telegram.chat_id
make run                     # scan → console + Telegram
make dry                     # --dry-run: print, don't touch DB, don't send
make reset-db                # wipe data/seen.db
make chats                   # list chats the bot has recently seen (for chat_id)
```

Local cron / launchd:

```
*/15 * * * * cd /path/to/poland_real_estate-scanner && $(poetry env info --path)/bin/python main.py >> data/scanner.log 2>&1
```

## Deployment on GitHub Actions

The workflow at
[`.github/workflows/scan.yml`](.github/workflows/scan.yml) runs every 15 min
(`cron: "*/15 * * * *"`) and can be triggered manually via
**Actions → scan → Run workflow**.

### Required secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `TG_BOT_TOKEN` | Bot token from `@BotFather` (`123…:AAE…`) |
| `TG_CHAT_ID` | Chat identifier: `@channel_name`, personal id (`582409029`), or supergroup id (`-100…`, `-541…`) |
| `TURSO_URL` | `libsql://<db>-<user>.turso.io` — from `turso db show <db> --url` |
| `TURSO_AUTH_TOKEN` | From `turso db tokens create <db> --expiration none` |

The workflow renders `config.yml` from `config.example.yml` on every run by
substituting `TG_*` into it, and passes `TURSO_*` through as env vars so the
scanner writes to cloud libSQL instead of a local file.

### What the workflow does

1. Checkout, set up Python 3.11, install Poetry, cache the virtualenv
2. `poetry install --no-root`
3. Render `config.yml` from `config.example.yml` + `TG_*` secrets (via
   inline Python — safe against special characters in the token)
4. `poetry run python main.py` with `TURSO_URL` + `TURSO_AUTH_TOKEN` in env,
   so writes go to the cloud DB. Cross-run continuity is provided by Turso —
   no `git commit` of `seen.db` needed.

### Costs

Public repo → **unlimited free minutes**. Private repo → 2000 min/month
free tier, and `*/15` uses ~2880 min/month — either make the repo public,
switch to `*/30`, or accept that scanning pauses near end of month. GitHub
never charges without a payment method + `spending limit > 0`.

## What is / isn't committed

Tracked in git:

- source code, config template, workflow, poetry lockfile
- `data/.gitkeep` (empty placeholder for the data directory)

Ignored (`.gitignore`):

- `config.yml` — contains the bot token, must never be committed
- `data/seen.db` and everything else in `data/` — the workflow re-adds
  `seen.db` with `git add -f` so cross-run continuity is preserved, but
  contributors never push their local dedup state

## Files at a glance

```
poland_real_estate-scanner/
├── main.py                  # CLI entrypoint — pipeline: fetch → filter → dedup → aggregate → notify
├── Makefile                 # run, dry, reset-db, chats, install
├── pyproject.toml
├── poetry.lock
├── config.example.yml       # template — copy to config.yml locally
├── AGENT.md                 # instructions for AI agents editing this repo
├── README.md                # this file
├── .github/workflows/scan.yml
├── data/
│   └── seen.db              # (created at runtime; committed only from the workflow)
└── scanner/
    ├── models.py            # Listing dataclass
    ├── filters.py           # ListingFilter — price/area/build_year/keywords
    ├── parsing.py           # parse_price / parse_area / parse_rooms
    ├── format.py            # plain + HTML formatters
    ├── aggregator.py        # group_listings — collapses developer bulk lots
    ├── storage.py           # SeenStore — SQLite-backed dedup
    ├── telegram.py          # TelegramNotifier + discover_chats
    └── sources/
        ├── base.py          # BaseSource: page-loop, fetch, session
        ├── otodom.py        # __NEXT_DATA__ parser
        ├── olx.py           # [data-cy="l-card"] parser
        ├── morizon.py       # .card + [data-cy=…] parser
        ├── komornik.py      # __NUXT_DATA__ (Devalue) parser
        └── bzp.py           # REST API client
```

## Adding a new source

1. Drop `scanner/sources/<name>.py` with a class that subclasses
   `BaseSource` and implements `_parse(html) -> Iterable[Listing]`. For a
   non-HTML source (JSON API etc.), override `scan()` instead and skip
   `_parse`. See [`scanner/sources/bzp.py`](scanner/sources/bzp.py) for the
   pattern.
2. Register it in `SOURCE_REGISTRY` in [`main.py`](main.py).
3. Add a block under `sources:` in `config.example.yml`.
4. `make dry` to confirm.

## For contributors / AI agents

Read [`AGENT.md`](AGENT.md) before making changes — it covers the design
decisions and the "check your work" checklist. Do not commit `config.yml`
or your local `data/seen.db`.
