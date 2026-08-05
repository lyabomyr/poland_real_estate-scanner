"""Shared UI chrome for every dashboard page.

Keeps the connection banner identical everywhere so a misconfigured deploy
looks the same on the overview, the listings table and the config editor —
rather than each page inventing its own empty state.
"""

from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from db import backend_info

ALL_TARGETS = "__all__"


def chat_label(chats: pd.DataFrame, chat_id: str) -> str:
    """Human label for a chat id: real title + id, flagged if disabled."""
    row = chats[chats["chat_id"].astype(str) == str(chat_id)]
    if row.empty:
        return str(chat_id)
    title = row.iloc[0]["title"] or "(no title)"
    suffix = "" if bool(row.iloc[0]["enabled"]) else "  ⏸ disabled"
    return f"{title} — {chat_id}{suffix}"


def preselected_chat_id(chats: pd.DataFrame) -> Optional[str]:
    """Chat id from the ``?chat_id=`` query param, if it exists in the store.

    Lets the bot hand out a link that opens straight on one chat's data —
    the greeting pins exactly such a URL, so a group can jump from Telegram
    to its own config without hunting through a dropdown.
    """
    try:
        raw = st.query_params.get("chat_id")
    except Exception:
        return None
    if not raw:
        return None
    wanted = str(raw).strip()
    known = set(chats["chat_id"].astype(str)) if not chats.empty else set()
    return wanted if wanted in known else None


def target_picker(chats: pd.DataFrame) -> Tuple[Optional[str], str]:
    """Render the "which chat" selector. Returns ``(chat_id | None, label)``.

    ``None`` means "all chats". Honours ``?chat_id=`` for the initial value.
    """
    if chats.empty:
        return None, "All chats"

    options = [ALL_TARGETS] + chats["chat_id"].astype(str).tolist()
    preset = preselected_chat_id(chats)
    index = options.index(preset) if preset in options else 0

    picked = st.selectbox(
        "Target",
        options=options,
        index=index,
        format_func=lambda cid: (
            "🌍 All chats (everything matched)" if cid == ALL_TARGETS
            else chat_label(chats, cid)
        ),
        help=(
            "Chats can have different price/area/city overrides, so they "
            "legitimately receive different listings. Pick one to see exactly "
            "what it was notified about."
        ),
    )
    if picked == ALL_TARGETS:
        return None, "All chats"
    return picked, chat_label(chats, picked)


def render_connection_status() -> bool:
    """Draw the sidebar backend badge. Returns True if connected to Turso.

    Pages call this first: when it returns False there is nothing useful to
    render, because the store is a local (empty) SQLite file rather than the
    shared cloud database the scanner writes to.
    """
    backend = backend_info()

    with st.sidebar:
        st.divider()
        if backend.error:
            st.error("⚠️ Turso connection failed")
            st.caption(backend.detail)
        elif backend.is_turso:
            st.success("🟢 Connected to Turso")
            st.caption(backend.detail)
        else:
            st.warning("🟡 Local SQLite (not connected)")
            st.caption(backend.detail)

    if backend.error:
        st.error(
            "**Could not reach Turso.** The credentials are set but the "
            "connection failed — the token may be revoked or the database "
            "renamed.\n\n"
            f"```\n{backend.error[:400]}\n```"
        )
        _render_secrets_help()
        return False

    if not backend.is_turso:
        st.warning(
            "**Not connected to the shared database.** This dashboard is "
            "reading an empty local SQLite file, so it can't show the "
            "scanner's data or edit chat settings."
        )
        _render_secrets_help()
        return False

    return True


def _render_secrets_help() -> None:
    """Actionable, copy-pasteable fix for the most common deploy mistake."""
    st.markdown(
        "#### How to connect\n"
        "In Streamlit Cloud open **Manage app → Settings → Secrets** and paste "
        "the two values below, then **Reboot app**."
    )
    st.code(
        'TURSO_URL = "libsql://<your-db>-<user>.turso.io"\n'
        'TURSO_AUTH_TOKEN = "<token>"',
        language="toml",
    )
    st.caption(
        "Get them with `turso db show <db> --url` and "
        "`turso db tokens create <db> --expiration none`. "
        "Use the same values as the repo's GitHub secrets so the dashboard and "
        "the scanner share one database."
    )
