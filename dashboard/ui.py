"""Shared UI chrome for every dashboard page.

Keeps the connection banner identical everywhere so a misconfigured deploy
looks the same on the overview, the listings table and the config editor —
rather than each page inventing its own empty state.
"""

from __future__ import annotations

import streamlit as st

from db import backend_info


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
