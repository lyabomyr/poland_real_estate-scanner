# Poland real-estate scanner

Scanner for buying apartments in Kraków under a fixed budget. Two runtime
pieces:

1. **GitHub Actions scanner** — runs every 15 minutes. Each run sends fresh
   matches *and* answers any Telegram commands received since the last run.
2. **Streamlit dashboard** — read-only analytics plus per-chat override
   editing.

State lives in **Turso/libSQL**, reached over its HTTP API (no compiled
driver — see [`scanner/turso_http.py`](scanner/turso_http.py)).
`TURSO_URL` + `TURSO_AUTH_TOKEN` are **required everywhere** — there is no
local-database fallback, so the scanner, the bot and the dashboard always
read the same rows.

> ⏱ **Bot replies take up to 15 minutes.** Commands are read by the scheduled
> scan, not by an always-on server, so a message sent at 12:01 is answered by
> the run that starts at 12:15. Nothing is lost — it just isn't instant. Hit
> **Actions → scan → Run workflow** when you want an immediate answer.

## Current architecture

### Scanner path

`GitHub Actions cron/manual dispatch -> main.py -> sources -> filters -> per-chat dedup -> scoring -> Telegram`

- Live sources: **Otodom**, **OLX**, **Morizon**, **licytacje.komornik.pl**
- Persistence: `seen`, `chat_configs`, `chat_emissions`, `greeted_chats`,
  processed Telegram `update_id`s
- Delivery model: one effective config per enabled chat

### Telegram command path

`GitHub Actions cron -> main.py -> getUpdates -> Turso-backed command handling -> Telegram reply`

Commands ride the same 15-minute run as the scan:

- Read-only: `/help`, `/status`, `/config`, `/urls`, `/decision_tree`,
  `/dashboard`, `/stats`
- Mutating: `/max_price`, `/min_area`, `/max_area`, `/min_year`, `/source`,
  `/kw`, `/pause`, `/resume`, `/reset`

Each `update_id` is claimed in `command_updates` before dispatch, so a command
is never executed twice. Mutations land in `chat_configs` in the same run that
answers them — so the scan you're waiting on already applies the new setting.

## Price changes

Portals edit a price in place, keeping the same listing id — so a naive
`seen`-based dedup would swallow every price cut, which is the single most
useful signal a buyer gets.

`chat_emissions.emitted_price` records what each chat was told. When a
re-seen listing's price differs from that, it is notified again, led by:

```
🔔 PRICE CHANGE
⬇️ price cut 40 000 zł (7%) — was 590 000 zł
```

Every move is also appended to `price_history`, which the dashboard renders
as a "recent price changes" table.

## Cities and generated URLs

Source URLs are **built**, not hardcoded, from `search.city` +
`search.max_price` + `search.min_area` via each source's `URL_TEMPLATE`.
That's what makes `city:` meaningful — a hardcoded Kraków URL would render
the setting inert.

Supported cities live in [`scanner/cities.py`](scanner/cities.py) (krakow,
katowice, warszawa, wroclaw, poznan, gdansk, gdynia, lodz, szczecin, lublin,
bydgoszcz, rzeszow). Each entry carries the per-portal spelling: Otodom wants
an ascii voivodeship slug in the path, komornik.pl wants the Polish
voivodeship name as a query param.

Because `city` is a per-chat override, one group can watch Kraków while
another watches Katowice off the same deployment. Verified live for both:
Kraków 96 listings, Katowice 81.

URL precedence, most specific first:

1. per-chat `source_urls[name]` (dashboard → Chat config → advanced)
2. explicit `url` in the YAML source block
3. generated from city + thresholds

## Defaults and filters

Defaults live in [`config.example.yml`](config.example.yml):

```yaml
search:
  min_area: 39
  max_price: 610000
```

Hard reject rules live in [`scanner/filters.py`](scanner/filters.py) and the
effective keyword lists come from the baseline config plus per-chat overrides.

Highlights:

- reject if `price > max_price` when price is known
- reject if `area < min_area` when area is known
- reject if `build_year < min_build_year` when build year is known
- reject if `title + description + location` matches any reject keyword
- missing values do **not** reject by themselves

## Dedup and scoring

Three dedup layers:

1. Same-source strict key: `<source>:<listing_id>`
2. Cross-source fuzzy key: `<price>|<round(area)>|<first non-city location token>`
3. Per-source aggregation for bulk developer listings

Scoring is rule-based and configured in YAML:

- price/m² vs median
- area sweet spot
- positive keywords
- negative keywords

`/decision_tree` is generated from the same effective config + rule model the
scanner uses.

## Telegram commands

Current command surface:

- `/help`
- `/status`
- `/config`
- `/urls`
- `/decision_tree`
- `/dashboard`
- `/stats [N]`
- `/max_price N`
- `/min_area N`
- `/max_area N`
- `/min_year Y`
- `/source NAME on|off`
- `/source NAME url URL`
- `/kw + NAME [WEIGHT]`
- `/kw - NAME [WEIGHT]`
- `/kw reject NAME`
- `/kw del NAME`
- `/kw list`
- `/reset FIELD`
- `/reset all`
- `/pause`
- `/resume`

Persistent reply keyboard:

- `/status`, `/help`, `/dashboard`
- `/config`, `/decision_tree`, `/urls`
- `/stats`, `/kw list`
- `/pause`, `/resume`

## `TG_CHAT_ID` is optional

`TG_CHAT_ID` is no longer required for normal operation.

Behavior:

- New groups auto-register when the bot is added
- Greeting includes the chat ID for copy/paste convenience
- If there are active chats in `chat_configs`, they are the source of truth
- If there are **no** active chats and `TG_CHAT_ID` is set, it is used as a
  fallback bootstrap/safety net
- Empty or missing `TG_CHAT_ID` must not break workflows or runtime config

## The dashboard link in Telegram

Every enabled chat gets a message with a deep link to **its own** config page
(`<dashboard>/Chat_config?chat_id=…`), pinned so it stays reachable from the
chat header.

Two things have to be true:

1. **`DASHBOARD_URL` is set** as a GitHub Actions *variable* (Settings →
   Secrets and variables → Actions → Variables). Without it the step is
   skipped silently — there's nothing to link to. For this project:

   ```
   DASHBOARD_URL = https://poland-realestate-scanner.streamlit.app
   ```
2. **The bot is a chat admin**, otherwise Telegram refuses `pinChatMessage`.
   The message is still delivered; only the pin is skipped, and the log says
   which happened.

Delivery is self-healing: every scan checks each chat and posts the link if
that chat doesn't have the current URL recorded in `pinned_dashboards`. So a
chat registered before `DASHBOARD_URL` existed picks it up on the next run,
and changing the URL re-pins everywhere. To force it now:

```bash
make pin-dashboard
```

**Make the app public**, or the link is useless to anyone who isn't signed
into your Streamlit account: Manage app → Settings → Sharing → "This app is
public and searchable".

## Streamlit dashboard

The dashboard URL is public and should be stored as a **GitHub Actions
Variable**, not a secret.

Config path:

- YAML: `notifications.dashboard_url`
- env override: `DASHBOARD_URL`

The bot surfaces the dashboard URL in:

- greeting
- `/help`
- `/status`
- `/dashboard`
- `/config`
- `/urls`

Deploy the Streamlit app, then put its final URL into `DASHBOARD_URL`.

See [`dashboard/README.md`](dashboard/README.md).

## Local development

Only Python 3.10+ is needed — dependencies come from
[`requirements.txt`](requirements.txt), the same file Streamlit Cloud
installs. `make install` auto-selects Python 3.12/3.11/3.10 when your
system `python3` is older.

```bash
make install
cp .env.example .env        # fill in TURSO_URL + TURSO_AUTH_TOKEN
cp config.example.yml config.yml
# fill telegram.bot_token if you want local polling / manual sends

make dry
make run
make prune
make chats
make greet
```

Note: `make chats` and `make greet` read `getUpdates`, the same channel the
scanner drains. Running them locally consumes updates the next scheduled scan
would otherwise have processed.

## Deployment

### GitHub Actions

[`scan.yml`](.github/workflows/scan.yml) runs:

- every 15 minutes on cron (GitHub may delay by 5-15 min at peak)
- manually from the Actions UI — use this for an immediate scan/reply

[`prune.yml`](.github/workflows/prune.yml) runs monthly and archives old
rejected rows.

### GitHub Secrets

Repository secrets required by GitHub Actions:

| Secret | Required | Purpose |
|---|---|---|
| `TG_BOT_TOKEN` | yes | Telegram BotFather token |
| `TURSO_URL` | yes | libSQL/Turso database URL |
| `TURSO_AUTH_TOKEN` | yes | Turso auth token |
| `TG_CHAT_ID` | optional | fallback bootstrap chat |

### GitHub Variables

Repository variables recommended for GitHub Actions:

| Variable | Required | Purpose |
|---|---|---|
| `DASHBOARD_URL` | optional | public Streamlit URL surfaced by the bot |

## Streamlit deployment

Deploy the dashboard from [`dashboard/app.py`](dashboard/app.py) on Streamlit
Community Cloud and set:

- `TURSO_URL`
- `TURSO_AUTH_TOKEN`

After the app is live, copy its final public URL into the GitHub Actions
variable `DASHBOARD_URL`.

## Verification checklist

Minimum checks before calling a change done:

```bash
make lint                  # pyflakes
make test                  # unit tests
make check-dashboard-deps  # requirements.txt covers dashboard imports
make boot-check            # dashboard boots in a clean venv
```

For parser or pipeline changes, also run:

```bash
make dry
```

## Files at a glance

```text
main.py                        CLI + wiring
.github/workflows/scan.yml     cron: scan + drain commands (every 15 min)
.github/workflows/prune.yml    cron: archive old rejected rows (monthly)
dashboard/                     Streamlit app
scanner/
  chat_config.py               per-chat overrides + effective config merge
  chat_repo.py                 chat_configs / chat_emissions CRUD
  commands.py                  Telegram command routing (polled)
  introspection.py             /config, /urls, /decision_tree reports
  pipeline.py                  MultiChatPipeline
  runtime_config.py            YAML load + env overrides
  telegram.py                  Bot API client, keyboard, greeting
  turso_http.py                Turso HTTP client (sqlite3-shaped interface)
  sources/
    otodom.py  olx.py  morizon.py  komornik.py
```
