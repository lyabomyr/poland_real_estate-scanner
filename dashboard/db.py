"""Shared Turso / local-SQLite connection + query helpers for the Streamlit UI.

Reads credentials from ``TURSO_URL`` + ``TURSO_AUTH_TOKEN`` — Streamlit
Cloud injects them from *App settings → Secrets*. Locally, ``st.secrets``
also picks them up if you drop a ``.streamlit/secrets.toml``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# Let `import scanner.*` resolve when Streamlit Cloud runs us from the
# repo root — dashboard/ lives inside the project package layout.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scanner.chat_config import ChatOverride  # noqa: E402
from scanner.chat_repo import ChatConfigRepo  # noqa: E402
from scanner.storage import SeenStore  # noqa: E402


def _resolve_secrets() -> None:
    """Copy Streamlit secrets into env so `SeenStore` picks Turso automatically."""
    try:
        secrets = st.secrets
    except FileNotFoundError:
        return
    except Exception:
        return
    for key in ("TURSO_URL", "TURSO_AUTH_TOKEN"):
        if key in secrets and key not in os.environ:
            os.environ[key] = str(secrets[key])


@st.cache_resource
def get_store() -> SeenStore:
    """Long-lived store handle. Cached across reruns; Streamlit keeps one per session."""
    _resolve_secrets()
    return SeenStore("./data/seen.db")


def get_repo() -> ChatConfigRepo:
    return ChatConfigRepo(get_store())


@st.cache_data(ttl=60)
def load_seen(status: Optional[str] = None) -> pd.DataFrame:
    """Full ``seen`` table as a DataFrame. Cached 60s to avoid hammering Turso on every rerun."""
    q = "SELECT * FROM seen"
    params: tuple = ()
    if status:
        q += " WHERE status = ?"
        params = (status,)
    q += " ORDER BY first_seen_at DESC"
    return _query_df(q, params)


def _query_df(query: str, params: tuple = ()) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame.

    libSQL's ``Cursor`` isn't iterable, so we can't hand it to
    ``pd.read_sql_query``. Two ``execute()`` calls: one to get rows, one to
    get the column names (``.description``). Both are cheap.
    """
    store = get_store()
    cur = store.conn.execute(query, params)
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=60)
def load_emissions_joined() -> pd.DataFrame:
    """Chat emissions joined with the listing they refer to. One row per (chat, listing)."""
    return _query_df(
        """
        SELECT
            e.chat_id, e.sent_at,
            s.source, s.title, s.url, s.price, s.area, s.fuzzy_key, s.first_seen_at
        FROM chat_emissions e
        JOIN seen s ON s.key = e.listing_key
        ORDER BY e.sent_at DESC
        """
    )


@st.cache_data(ttl=30)
def load_chats() -> pd.DataFrame:
    """All chat_configs rows as a DataFrame."""
    return _query_df(
        "SELECT chat_id, title, enabled, config, updated_at FROM chat_configs ORDER BY chat_id"
    )


def upsert_chat_override(chat_id: str, title: Optional[str], override: ChatOverride) -> None:
    """Save a chat override + bust the cache so the UI shows fresh data."""
    get_repo().upsert(chat_id, title, override, enabled=True)
    load_chats.clear()  # invalidate cached DataFrame


def set_chat_enabled(chat_id: str, enabled: bool) -> None:
    get_repo().set_enabled(chat_id, enabled)
    load_chats.clear()
