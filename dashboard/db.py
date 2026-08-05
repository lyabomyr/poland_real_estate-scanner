"""Turso connection + query helpers for the Streamlit UI.

Credentials come from ``TURSO_URL`` + ``TURSO_AUTH_TOKEN``, resolved in this
order:

1. real environment variables (Streamlit Cloud injects its Secrets this way)
2. ``.streamlit/secrets.toml`` — only read when the file actually exists
3. a project-root ``.env`` — the local development path

There is no local-SQLite fallback. It used to make an unconfigured dashboard
render as an empty market, which read as "the scanner found nothing" instead
of "you are not connected".
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
from scanner.env import load_dotenv  # noqa: E402
from scanner.runtime_config import load_runtime_config  # noqa: E402
from scanner.storage import MissingCredentialsError, SeenStore  # noqa: E402

_WANTED_SECRETS = ("TURSO_URL", "TURSO_AUTH_TOKEN")


@st.cache_data(ttl=300)
def load_baseline_config() -> dict:
    """The YAML baseline every chat inherits from.

    Always the repo's tracked ``config.yml`` — the one file the scanner runs
    with, here and on GitHub Actions. There used to be a second, gitignored
    copy that this preferred when present, which meant a developer's stale
    file was shown as "the defaults" while the scanner applied different ones.
    """
    path = _ROOT / "config.yml"
    return load_runtime_config(path) if path.exists() else {}


def effective_config(override: ChatOverride) -> EffectiveConfig:
    """Baseline + this chat's override, i.e. what the scanner will actually use."""
    return EffectiveConfig(baseline=load_baseline_config(), override=override)


def _secrets_file_exists() -> bool:
    """Whether Streamlit has a secrets.toml to read.

    Checked *before* touching ``st.secrets`` on purpose: reading it with no
    file present raises, and Streamlit surfaces that as a red error box in the
    UI even when the caller catches it. Probing the paths ourselves keeps a
    normal local run clean.
    """
    candidates = (
        Path.home() / ".streamlit" / "secrets.toml",
        _ROOT / ".streamlit" / "secrets.toml",
    )
    return any(p.exists() for p in candidates)


def _resolve_secrets() -> None:
    """Populate TURSO_* in the environment from whichever source has them."""
    if all(os.environ.get(key) for key in _WANTED_SECRETS):
        return

    if _secrets_file_exists():
        try:
            for key in _WANTED_SECRETS:
                if not os.environ.get(key) and key in st.secrets:
                    os.environ[key] = str(st.secrets[key])
        except Exception:
            pass

    if not all(os.environ.get(key) for key in _WANTED_SECRETS):
        load_dotenv()


@dataclass
class Backend:
    """Which database the UI actually ended up talking to.

    Surfaced in the sidebar because the failure mode is otherwise silent and
    confusing: with no secrets configured the store quietly opens an empty
    local SQLite file, and the app looks like "the scanner never ran" instead
    of "the dashboard isn't connected".
    """
    kind: str                      # "turso" | "unconfigured"
    detail: str                    # host, or a short reason
    error: Optional[str] = None    # set when creds exist but the connect fails
    schema_missing: bool = False   # connected, but the scanner never ran

    @property
    def is_turso(self) -> bool:
        return self.kind == "turso"


# Interactive timeout: well below the scanner's 30 s so a slow database shows
# up as an error banner rather than a spinner that never resolves.
_HTTP_TIMEOUT_SECONDS = 12


@st.cache_resource
def get_store() -> SeenStore:
    """Long-lived store handle. Cached across reruns; one per session.

    ``ensure_schema=False`` on purpose: the DDL costs 15 sequential Turso
    round-trips, and the scanner already owns schema creation. Skipping it
    turns a multi-second cold start into a single connect.
    """
    _resolve_secrets()
    return SeenStore(ensure_schema=False, timeout=_HTTP_TIMEOUT_SECONDS)


@st.cache_data(ttl=30)
def backend_info() -> Backend:
    """Report the live backend, probing it so bad credentials surface as errors."""
    _resolve_secrets()
    url = os.environ.get("TURSO_URL")
    if not (url and os.environ.get("TURSO_AUTH_TOKEN")):
        missing = [k for k in _WANTED_SECRETS if not os.environ.get(k)]
        return Backend(kind="unconfigured", detail=f"missing {', '.join(missing)}")

    host = url.split("://", 1)[-1]
    try:
        store = get_store()
        store.conn.execute("SELECT 1").fetchone()
    except MissingCredentialsError as exc:
        return Backend(kind="unconfigured", detail=str(exc).splitlines()[0])
    except Exception as exc:  # wrong token, revoked DB, network…
        return Backend(kind="turso", detail=host, error=str(exc))
    # We skip the DDL, so an empty database is a state we have to name
    # explicitly rather than crash on the first SELECT.
    return Backend(kind="turso", detail=host, schema_missing=not store.schema_ready())


def get_repo() -> ChatConfigRepo:
    return ChatConfigRepo(get_store())


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
def load_listings_full(chat_id: Optional[str] = None) -> pd.DataFrame:
    """Like :func:`load_listings` but with the card fields (image, blurb, location).

    Kept separate so the big table view doesn't drag description blobs over
    the wire on every rerun.
    """
    cols = (
        "key, source, title, url, price, area, score, score_reasons, "
        "location, description, image_url, fuzzy_key, first_seen_at"
    )
    if chat_id is None:
        return _query_df(
            f"SELECT {cols}, NULL AS sent_at FROM seen WHERE status = 'matched' "
            "ORDER BY score DESC NULLS LAST, price ASC"
        )
    prefixed = ", ".join(f"s.{c.strip()}" for c in cols.split(","))
    return _query_df(
        f"SELECT {prefixed}, e.sent_at "
        "FROM chat_emissions e JOIN seen s ON s.key = e.listing_key "
        "WHERE e.chat_id = ? AND s.status = 'matched' "
        "ORDER BY s.score DESC NULLS LAST, s.price ASC",
        (str(chat_id),),
    )


@st.cache_data(ttl=60)
def load_price_history() -> pd.DataFrame:
    """Every recorded price move, newest first."""
    return _query_df(
        "SELECT h.listing_key, h.old_price, h.new_price, h.changed_at, "
        "       s.source, s.title, s.url, s.area "
        "FROM price_history h LEFT JOIN seen s ON s.key = h.listing_key "
        "ORDER BY h.changed_at DESC"
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
