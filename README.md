# Poland real-estate scanner

Cron-driven scanner for buying apartments in Kraków under a fixed budget.
Runs every 15 min on GitHub Actions, checks 5 real-estate sources,
deduplicates, filters, and pushes fresh matches to a Telegram group.

- **Live sources**: Otodom, OLX, Morizon, licytacje.komornik.pl (bailiff
  auctions), BZP / eZamówienia (public-procurement API)
- **Cost**: $0 — public GitHub Actions minutes are unlimited on public repos
- **Persistence**: SQLite (`data/seen.db`) committed back to the repo after
  each run

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

## Dedup

Everything the scanner has ever *seen* is stored in SQLite at
`storage.db_path` (default `./data/seen.db`), keyed as
`<source>:<listing_id>`. Table `seen` also records `url`, `title`, `price`,
`area`, `status` (`matched` / `rejected`), `reject_reason`, `first_seen_at`
— useful for ad-hoc audits:

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

The workflow renders `config.yml` from `config.example.yml` on every run by
substituting those two secrets — nothing else is needed.

### What the workflow does

1. Checkout, set up Python 3.11, install Poetry, cache the virtualenv
2. `poetry install --no-root`
3. Render `config.yml` from `config.example.yml` + secrets (via inline Python,
   safe against special characters in the token)
4. `poetry run python main.py`
5. Force-add `data/seen.db` (bypasses gitignore), commit, push — so the next
   run inherits the dedup state

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
