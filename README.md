# Poland real-estate scanner

Scanner for buying apartments in Kraków under a fixed budget. The project is
split into three runtime pieces:

1. **GitHub Actions scanner** runs every 15 minutes and sends fresh matches.
2. **Vercel webhook endpoint** handles Telegram commands immediately.
3. **Streamlit dashboard** provides a read-only analytics view plus per-chat
   override editing.

State lives in **Turso/libSQL**. Local development falls back to SQLite.

## Current architecture

### Scanner path

`GitHub Actions cron/manual dispatch -> main.py -> sources -> filters -> per-chat dedup -> scoring -> Telegram`

- Live sources: **Otodom**, **OLX**, **Morizon**, **licytacje.komornik.pl**
- Persistence: `seen`, `chat_configs`, `chat_emissions`, `greeted_chats`,
  processed Telegram `update_id`s
- Delivery model: one effective config per enabled chat

### Telegram command path

`Telegram webhook -> Vercel Function -> webhook secret validation -> Turso-backed command handling -> immediate Telegram reply`

- Read-only commands reply immediately: `/help`, `/status`, `/config`,
  `/urls`, `/decision_tree`, `/dashboard`, `/stats`
- Mutating commands update `chat_configs` immediately: `/max_price`,
  `/min_area`, `/max_area`, `/min_year`, `/source`, `/kw`, `/pause`,
  `/resume`, `/reset`
- `/scan` replies immediately, triggers `scan.yml` via GitHub Actions
  `workflow_dispatch`, then sends a completion summary back to Telegram

### Why Vercel, not Cloudflare Workers

Cloudflare Workers Free was the original preference, but the repo’s hard
requirement is that `/config` and `/decision_tree` reflect the **same Python
effective-config and rule logic** as the scanner. Reusing the Python code in a
Vercel Function is lower-risk than maintaining a second JS implementation of
the config merge, filter tree, scoring model, redaction, and command parsing.

Expected latency:

- Warm webhook responses: usually sub-second
- Cold starts: low single-digit seconds
- `/scan` acknowledgement: immediate; the actual scan still depends on GitHub
  Actions queue + runtime

Official references used for this setup:

- [Telegram Bot API](https://core.telegram.org/bots/api?source=post_page)
- [GitHub workflow dispatch REST API](https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28+)
- [Vercel Python Functions](https://vercel.com/docs/functions/runtimes/python)
- [Vercel Hobby plan](https://vercel.com/docs/plans/hobby)

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
- `/scan`
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

Requires [Poetry](https://python-poetry.org/).

```bash
make install
cp config.example.yml config.yml
# fill telegram.bot_token if you want local polling / manual sends

make dry
make run
make prune
make chats
make greet
```

Notes:

- `make chats` and `make greet` use `getUpdates`, so they only work when no
  webhook is configured on the bot
- local polling mode is for development; production should set
  `TG_WEBHOOK_ENABLED=true`

## Deployment

### GitHub Actions

[`scan.yml`](.github/workflows/scan.yml) runs:

- every 15 minutes on cron
- manually from the Actions UI
- from Telegram `/scan` via `workflow_dispatch`

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
| `TG_WEBHOOK_ENABLED` | yes in production | tells the scanner to skip `getUpdates` polling |

### Vercel environment variables

Set these on the Vercel project that serves `api/telegram_webhook.py`:

| Variable | Required | Purpose |
|---|---|---|
| `TG_BOT_TOKEN` | yes | same bot token |
| `TG_WEBHOOK_SECRET` | yes | Telegram webhook secret token |
| `TG_WEBHOOK_ENABLED` | yes | set to `true` |
| `TURSO_URL` | yes | same Turso URL |
| `TURSO_AUTH_TOKEN` | yes | same Turso token |
| `DASHBOARD_URL` | optional | public Streamlit URL |
| `TG_CHAT_ID` | optional | fallback bootstrap chat |
| `TG_WORKFLOW_ALLOWED_CHAT_IDS` | yes for `/scan` | comma-separated chat IDs allowed to dispatch scans |
| `TG_WORKFLOW_ALLOWED_USER_IDS` | optional | comma-separated Telegram user IDs additionally allowed to dispatch `/scan` |
| `GITHUB_REPOSITORY_OWNER` | yes for `/scan` | repo owner |
| `GITHUB_REPOSITORY_NAME` | yes for `/scan` | repo name |
| `GITHUB_SCAN_WORKFLOW_FILE` | yes for `/scan` | usually `scan.yml` |
| `GITHUB_SCAN_WORKFLOW_REF` | yes for `/scan` | usually `main` |
| `GITHUB_WORKFLOW_TOKEN` | yes for `/scan` | token that can call workflow dispatch |

### GitHub token for `/scan`

Minimum required permission for the token used by the webhook endpoint:

- **Actions: write**

Per GitHub’s current REST docs, a fine-grained token with `Actions: write`
is sufficient for creating a workflow dispatch. Keep the token scoped to this
repository only.

### Vercel deploy steps

1. Import the repository into Vercel.
2. Keep the repo root as the project root.
3. Set all Vercel env vars listed above.
4. Deploy.
5. Note the production URL, e.g. `https://your-project.vercel.app`.

#### How the function gets its dependencies

Vercel's Python runtime installs from **`requirements.txt`** at the repo
root — it does *not* read `pyproject.toml` or `poetry.lock`. That file is
deliberately minimal (`requests`, `PyYAML`, `libsql-experimental`) because
the webhook only needs to talk to Telegram, read `config.example.yml`, and
reach Turso.

`beautifulsoup4` (scanner-only) and `streamlit` / `pandas` / `plotly`
(dashboard-only) are intentionally excluded — shipping them would add
~120 MB to the function bundle and slow every cold start.

**If you add a dependency that the webhook path needs, add it to both
`pyproject.toml` and `requirements.txt`.** The scanner on GitHub Actions
and the dashboard on Streamlit Cloud keep using Poetry / `pyproject.toml`.

### Telegram webhook setup

Use the helper script:

```bash
export TG_BOT_TOKEN="123456:ABC"
export TG_WEBHOOK_SECRET="your-secret-token"

poetry run python scripts/manage_telegram_webhook.py set \
  --url "https://your-project.vercel.app/api/telegram_webhook" \
  --secret "$TG_WEBHOOK_SECRET" \
  --drop-pending-updates
```

Inspect status:

```bash
poetry run python scripts/manage_telegram_webhook.py info
```

Delete webhook and go back to polling:

```bash
poetry run python scripts/manage_telegram_webhook.py delete --drop-pending-updates
```

## Streamlit deployment

Deploy the dashboard from [`dashboard/app.py`](dashboard/app.py) on Streamlit
Community Cloud and set:

- `TURSO_URL`
- `TURSO_AUTH_TOKEN`

After the app is live, copy its final public URL into:

- GitHub Actions variable `DASHBOARD_URL`
- Vercel env var `DASHBOARD_URL`

## Verification checklist

Minimum checks before calling a change done:

```bash
poetry run pyflakes scanner/ main.py api scripts tests
poetry run python -m unittest discover -s tests -v
```

For parser or pipeline changes, also run:

```bash
make dry
```

## Files at a glance

```text
main.py
api/telegram_webhook.py
scripts/manage_telegram_webhook.py
scripts/send_scan_summary.py
.github/workflows/scan.yml
.github/workflows/prune.yml
dashboard/
scanner/
  chat_config.py
  chat_repo.py
  commands.py
  introspection.py
  pipeline.py
  runtime_config.py
  telegram.py
  webhook.py
  sources/
    otodom.py
    olx.py
    morizon.py
    komornik.py
```
