# Kraków real-estate scanner

Scans **Otodom**, **OLX**, and **listaprzetargow.pl** for apartment listings in
Kraków that match your parameters (min area, max price, min build year, keyword
filters), deduplicates them, and sends fresh matches to a Telegram channel.

## Setup

Requires [Poetry](https://python-poetry.org/) (`pipx install poetry` or `brew install poetry`).

```bash
make install
# then edit config.yml — fill in telegram.bot_token / telegram.chat_id
# (or leave the defaults to just print matches to console)
```

## Run

```bash
make run          # scan → print + Telegram (if configured)
make dry          # dry-run — print, don't touch DB, don't send
make reset-db     # wipe the SQLite dedup store
make help         # list all targets
```

Under the hood `make run` is just `poetry run python main.py`, so custom flags still work:

```bash
poetry run python main.py --config /path/to/other.yml
```

Schedule with cron / launchd, e.g. every 10 min:

```
*/10 * * * * cd /path/to/poland_real_estate-scanner && $(poetry env info --path)/bin/python main.py >> data/scanner.log 2>&1
```

## How filters work

Configured in `config.yml`:

* `search.min_area` — reject if `area < min_area`
* `search.max_price` — reject if `price > max_price`
* `search.min_build_year` — reject only if a build year is known and older
* `filters.reject_keywords` — case-insensitive word-boundary match against
  title + description + location. Default list already blocks TBS, `suterena`,
  `półpiwnica`, etc.

## Dedup

Seen listings are stored in a SQLite database at `storage.db_path`
(default `./data/seen.db`), keyed as `<source>:<listing_id>`. The `seen` table
also records `url`, `title`, `price`, `area`, `status` (`matched` /
`rejected`), `reject_reason`, and `first_seen_at` — handy for ad-hoc queries:

```bash
sqlite3 data/seen.db 'SELECT reject_reason, COUNT(*) FROM seen WHERE status="rejected" GROUP BY 1 ORDER BY 2 DESC;'
```

Delete the file to reset.

## Notes on sources

* **Otodom** — parses the Next.js `__NEXT_DATA__` JSON (stable, structured).
* **OLX** — parses the `[data-cy="l-card"]` cards from HTML. OLX cards that
  point to `otodom.pl` are skipped to avoid duplicates.
* **Morizon** — parses SSR HTML (`.card` elements with predictable `data-cy`
  attributes). Reliable, no headless browser needed.
* **Komornik (licytacje.komornik.pl)** — bailiff-forced-sale auctions.
  Parses `__NUXT_DATA__` (Devalue index-ref format) from the SSR HTML.
* **BZP / eZamówienia** — public-procurement REST API. Mostly public tenders,
  so genuine apartment sales are rare — a few per month at best (bankruptcy /
  communal). Tune `sources.bzp.order_object` (e.g. `sprzedaż lokalu`) or
  `cpv_code` (`70123100` = residential sale) to narrow.
* **listaprzetargow.pl** — disabled by default. Fully JS-rendered; SSR HTML
  is empty. Would need Playwright / headless Chromium to work.
* **KRZ (`krz.ms.gov.pl`)** — the *authoritative* source for Polish
  bankruptcy-trustee (syndyk) auctions since Dec 2021, **not supported here**.
  The portal is protected by Incapsula bot-shield that rejects even
  stealthed headless Chromium (`edet=12` → "headless browser detected").
  Bypassing needs paid stealth proxies or a residential IP, which isn't
  compatible with a free CI setup.

## Extend

Add a new source in `scanner/sources/<name>.py`, subclass `BaseSource`,
implement `scan() → Iterable[Listing]`, register it in
`SOURCE_REGISTRY` in `main.py`, and add a `sources.<name>` block in the
config.
