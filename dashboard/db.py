"""Shared Turso / local-SQLite connection + query helpers for the Streamlit UI.

Reads credentials from ``TURSO_URL`` + ``TURSO_AUTH_TOKEN`` — Streamlit
Cloud injects them from *App settings → Secrets*. Locally, ``st.secrets``
also picks them up if you drop a ``.streamlit/secrets.toml``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# Let `import scanner.*` resolve when Streamlit Cloud runs us from the
# repo root — dashboard/ lives inside the project package layout.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scanner.chat_config import ChatOverride, EffectiveConfig  # noqa: E402
from scanner.chat_repo import ChatConfigRepo  # noqa: E402
from scanner.runtime_config import load_runtime_config  # noqa: E402
from scanner.storage import SeenStore  # noqa: E402


@st.cache_data(ttl=300)
def load_baseline_config() -> dict:
    """The YAML baseline every chat inherits from.

    Prefers a local ``config.yml`` (developer machine) and falls back to the
    tracked ``config.example.yml``, which is what the deployed scanner runs
    with. The dashboard needs this to show *effective* values — a chat with
    no overrides should display the real defaults, not zeros.
    """
    for candidate in ("config.yml", "config.example.yml"):
        path = _ROOT / candidate
        if path.exists():
            return load_runtime_config(path)
    return {}


def effective_config(override: ChatOverride) -> EffectiveConfig:
    """Baseline + this chat's override, i.e. what the scanner will actually use."""
    return EffectiveConfig(baseline=load_baseline_config(), override=override)


def _resolve_secrets() -> None:
    """Copy Streamlit secrets into env so `SeenStore` picks Turso automatically.

    ``st.secrets`` is lazy: the attribute access is cheap, but *reading* from
    it raises ``StreamlitSecretNotFoundError`` when no secrets.toml exists.
    So the whole lookup has to sit inside the try, and we skip it entirely
    when the env already carries both values (local dev with exported vars,
    or a plain SQLite run).
    """
    wanted = ("TURSO_URL", "TURSO_AUTH_TOKEN")
    if all(os.environ.get(key) for key in wanted):
        return
    try:
        for key in wanted:
            if key not in os.environ and key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except Exception:
        # No secrets configured — fall back to the local SQLite file.
        return


@dataclass
class Backend:
    """Which database the UI actually ended up talking to.

    Surfaced in the sidebar because the failure mode is otherwise silent and
    confusing: with no secrets configured the store quietly opens an empty
    local SQLite file, and the app looks like "the scanner never ran" instead
    of "the dashboard isn't connected".
    """
    kind: str                      # "turso" | "sqlite"
    detail: str                    # host, or local file path
    error: Optional[str] = None    # set when Turso creds exist but fail

    @property
    def is_turso(self) -> bool:
        return self.kind == "turso"


@st.cache_resource
def get_store() -> SeenStore:
    """Long-lived store handle. Cached across reruns; Streamlit keeps one per session."""
    _resolve_secrets()
    return SeenStore("./data/seen.db")


@st.cache_data(ttl=30)
def backend_info() -> Backend:
    """Report the live backend, probing it so bad credentials surface as errors."""
    _resolve_secrets()
    url = os.environ.get("TURSO_URL")
    if not (url and os.environ.get("TURSO_AUTH_TOKEN")):
        return Backend(kind="sqlite", detail="./data/seen.db")

    host = url.split("://", 1)[-1]
    try:
        get_store().conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # wrong token, revoked DB, network…
        return Backend(kind="turso", detail=host, error=str(exc))
    return Backend(kind="turso", detail=host)


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
def load_listings(chat_id: Optional[str] = None) -> pd.DataFrame:
    """Matched listings, optionally scoped to what one chat was notified about.

    ``chat_id=None`` returns everything the scanner ever matched. Passing a
    chat id joins ``chat_emissions``, which is the per-chat delivery log —
    two chats with different filters legitimately see different listings, so
    "what did *this* chat get" is a different question from "what matched".
    """
    if chat_id is None:
        return _query_df(
            "SELECT key, source, title, url, price, area, score, score_reasons, "
            "       fuzzy_key, first_seen_at, NULL AS sent_at "
            "FROM seen WHERE status = 'matched' "
            "ORDER BY score DESC NULLS LAST, price ASC"
        )
    return _query_df(
        "SELECT s.key, s.source, s.title, s.url, s.price, s.area, s.score, "
        "       s.score_reasons, s.fuzzy_key, s.first_seen_at, e.sent_at "
        "FROM chat_emissions e JOIN seen s ON s.key = e.listing_key "
        "WHERE e.chat_id = ? AND s.status = 'matched' "
        "ORDER BY s.score DESC NULLS LAST, s.price ASC",
        (str(chat_id),),
    )


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
