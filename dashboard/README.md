# Streamlit dashboard

Read-view + editor UI on top of the scanner's Turso DB.

```
dashboard/
├── app.py                       # main page — KPIs + charts
├── db.py                        # cached Turso connection & DataFrame loaders
├── pages/
│   ├── 1_📋_Listings.py         # filterable table of matched listings
│   └── 2_⚙️_Chat_config.py     # per-chat override editor
└── README.md                    # you are here
```

## Local run

```bash
make install
cp ../.env.example ../.env   # fill in TURSO_URL + TURSO_AUTH_TOKEN
make dashboard
```

Opens on `http://localhost:8501`.
`make` auto-selects Python 3.10+ for the venv if your system `python3` is too old.

Turso is required — there is no local-database fallback. Without credentials
the app renders a "Not configured" screen with the fix, rather than quietly
showing an empty market.

## Streamlit Community Cloud (free hosting)

1. Push this repo to a **public** GitHub repo.
2. <https://share.streamlit.io> → **New app** → pick the repo.
3. **Main file path**: `dashboard/app.py`.
4. **Advanced → Secrets** — paste:
   ```toml
   TURSO_URL = "libsql://…turso.io"
   TURSO_AUTH_TOKEN = "eyJhbGciOi…"
   ```
5. Deploy.

Before deploying, verify the dependency list is complete:

```bash
make check-dashboard-deps
```

That builds a venv from `requirements.txt` alone and imports every dashboard
module — reproducing Streamlit Cloud, where a missing package shows up as a
`ModuleNotFoundError` on a live page rather than at build time.

Dependencies come from the repo-root [`requirements.txt`](../requirements.txt),
which Streamlit Cloud installs from. It is deliberately
**pure-Python** — no compiled extensions — so the build cannot break on
whatever CPython version the platform happens to run.

> That mattered: the first deploy failed because `libsql-experimental` (the
> native Turso driver) ships no cp314 wheel, Streamlit Cloud runs CPython
> 3.14, pip attempted a source build and the Rust step died inside
> `libsql-ffi`. The dashboard and scanner now reach Turso over its HTTP API
> (see [`scanner/turso_http.py`](../scanner/turso_http.py)) and the native
> driver is gone. For a remote database it was the same network round trip
> anyway.

Free tier: unlimited public apps. An app with no visitors sleeps after
~7 days; the next visit wakes it in ~30 s.

## Pages

* **Main (app.py)** — KPIs, source-mix bar chart, reject-reason bar chart,
  new-matches-per-day line, price/m² histogram, per-chat delivery counts.
* **📋 Listings** — sortable, filterable table of every matched listing
  (source, price, area, zł/m², URL link, seen-at).
* **⚙️ Chat config** — per-chat override editor. Save writes to the
  `chat_configs` table in Turso; the scanner picks up changes on the next
  scan, or immediately if you trigger `/scan` from Telegram.

## Adding a new page

Drop a new file into `dashboard/pages/N_🌟_Name.py`. Streamlit picks it up
automatically on next page reload. Follow the emoji + `_underscore_`
naming — it becomes the sidebar entry.

## When the deploy hangs

Streamlit Cloud's boot log ends with its own infrastructure steps:

```
Provisioning machine...
Preparing system...
Spinning up manager process...
Updated app!          <- our code has started by here
```

If it stalls **at "Spinning up manager process"**, our code has not run yet —
that step is entirely platform-side. Don't go hunting through the app.

Confirm the app itself is healthy first:

```bash
make check-dashboard-deps   # requirements.txt is complete
./scripts/boot_check.sh     # boots app.py in a pristine venv, hits every page
```

If both pass, the app is fine and the stall is Streamlit's. In order:

1. **Reboot app** (Manage app → ⋮ → Reboot).
2. If it stalls again, **delete the app and redeploy it**. This clears a stuck
   container and is the reliable fix; the URL slug is rebuilt from the repo
   name, so it usually comes back the same.
3. Check <https://status.streamlit.io>.

Not a cause, despite looking like one:

* **Memory.** Measured 162 MB RSS for pandas + streamlit + plotly against a
  ~1 GB limit. Don't rip out plotly on suspicion — measure first.
* **"WARN: More than one requirements file detected."** Streamlit sees both
  `requirements.txt` and `requirements-dev.txt`. The deploy uses
  `requirements.txt`; the `-dev` file is only for local tooling.
