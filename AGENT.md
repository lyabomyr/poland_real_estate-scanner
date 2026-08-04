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
- **De-duplicated**: same listing must not appear twice across runs. When
  30 near-duplicate developer units appear in one scan, they roll up into a
  single Telegram message — see :mod:`scanner.aggregator`.

If a change breaks any of these, the run is broken even if the code passes
type-checks.

## Repo layout

```
main.py                                CLI entrypoint. Owns the run loop.
scanner/
  models.py         Listing dataclass — the single shape passed between stages.
  filters.py        ListingFilter — accept/reject rules from config.
  parsing.py        parse_price / parse_area / parse_rooms — shared regex helpers.
  aggregator.py     group_listings — folds near-duplicates into ListingGroup.
  format.py         plain + HTML rendering for console and Telegram.
  storage.py        SeenStore — SQLite dedup, keyed by "<source>:<id>".
  telegram.py       TelegramNotifier — Bot API + 429 retry, chat auto-discover.
  sources/
    base.py         BaseSource: page-loop, fetch, session. Subclass this.
    otodom.py       __NEXT_DATA__ (Next.js SSR data).
    olx.py          [data-cy="l-card"] card scraping.
    morizon.py      .card + [data-cy=…] scraping.
    komornik.py     __NUXT_DATA__ (Devalue index-ref format).
    bzp.py          REST API client (overrides scan()).
```

## Running & verifying

Common commands (`make help` lists all):

```bash
make install        # poetry install
make dry            # --dry-run: scan every source, print, don't touch DB, don't send
make run            # real run
make reset-db       # rm data/seen.db (safe — the workflow will recreate on next run)
make chats          # print chats the bot has recently seen (getUpdates)
```

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
- **Actions run shows `already_seen: 219, matched: 0`.** The committed
  `data/seen.db` in the repo has everything. Delete it locally, commit the
  deletion, push — the workflow will recreate from scratch.

If you're about to write "I *think* this works" without running `make dry`
— go back and run it.
