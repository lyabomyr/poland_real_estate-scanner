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
2. Go to <https://share.streamlit.io> → **New app** → choose the repo.
3. **Main file path**: `dashboard/app.py`.
4. **Python version**: `3.11`.
5. **Advanced → Secrets** — paste:
   ```toml
   TURSO_URL = "libsql://…turso.io"
   TURSO_AUTH_TOKEN = "eyJhbGciOi…"
   ```
6. Deploy. First build takes ~2 min (pip installs `libsql-experimental` +
   `streamlit` + `pandas` + `plotly`).

The app URL will be `https://<slug>.streamlit.app`. Free tier: unlimited
public apps, no sleep for actively-used ones.

## Pages

* **Main (app.py)** — KPIs, source-mix bar chart, reject-reason bar chart,
  new-matches-per-day line, price/m² histogram, per-chat delivery counts.
* **📋 Listings** — sortable, filterable table of every matched listing
  (source, price, area, zł/m², URL link, seen-at).
* **⚙️ Chat config** — per-chat override editor. Save writes to the
  `chat_configs` table in Turso; the scanner picks up changes on its next
  15-minute cron tick.

## Adding a new page

Drop a new file into `dashboard/pages/N_🌟_Name.py`. Streamlit picks it up
automatically on next page reload. Follow the emoji + `_underscore_`
naming — it becomes the sidebar entry.
