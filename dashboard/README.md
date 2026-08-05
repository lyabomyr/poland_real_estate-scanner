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
poetry install
TURSO_URL=libsql://…turso.io \
TURSO_AUTH_TOKEN=eyJhbGciOi… \
  poetry run streamlit run dashboard/app.py
```

Opens on `http://localhost:8501`.

Without the two `TURSO_*` vars set, `db.py` falls back to a local
`data/seen.db` — same as the scanner.

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

Dependencies come from the repo-root [`requirements.txt`](../requirements.txt),
which Streamlit Cloud prefers over `pyproject.toml`. It is deliberately
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
