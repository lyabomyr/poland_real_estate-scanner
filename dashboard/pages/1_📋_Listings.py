"""Matched listings, scoped per chat, ranked by deal score.

Each chat has its own filters, so "what matched" and "what *this chat* was
notified about" are different sets. The target picker switches between them;
the default is the union across every chat.
"""

from __future__ import annotations

import streamlit as st

from ui import render_connection_status
from db import load_chats, load_listings

st.set_page_config(page_title="Listings — Kraków flats", page_icon="📋", layout="wide")
st.title("📋 Matched listings")

if not render_connection_status():
    st.stop()

# ── Target (chat) picker ──────────────────────────────────────────────

chats = load_chats()
ALL = "__all__"
options = [ALL] + chats["chat_id"].astype(str).tolist()


def _chat_label(chat_id: str) -> str:
    if chat_id == ALL:
        return "🌍 All chats (everything matched)"
    row = chats[chats["chat_id"].astype(str) == chat_id]
    if row.empty:
        return chat_id
    title = row.iloc[0]["title"] or "(no title)"
    suffix = "" if bool(row.iloc[0]["enabled"]) else "  ⏸ disabled"
    return f"{title} — {chat_id}{suffix}"


picked = st.selectbox(
    "Target",
    options=options,
    format_func=_chat_label,
    help=(
        "Pick a chat to see only what was delivered there. Chats with different "
        "price/area/keyword overrides legitimately receive different listings."
    ),
)
chat_id = None if picked == ALL else picked

df = load_listings(chat_id)
if df.empty:
    st.info(
        "Nothing delivered to this chat yet. A newly registered chat starts "
        "with a clean slate — the historical backlog is suppressed on purpose "
        "so it isn't flooded on its first scan."
        if chat_id else
        "No matched listings yet — trigger the **scan** workflow and refresh."
    )
    st.stop()

# ── Sidebar filters ───────────────────────────────────────────────────

with st.sidebar:
    st.subheader("Filter")

    sources = sorted(df["source"].dropna().unique().tolist())
    picked_src = st.multiselect("Source", sources, default=sources)

    score_range = None
    if df["score"].notna().any():
        smin, smax = int(df["score"].min()), int(df["score"].max())
        score_range = st.slider(
            "Deal score",
            min_value=smin, max_value=max(smax, smin + 1), value=(smin, smax),
            help="Rule-based 0-100. Ask the bot /decision_tree for the formula.",
        )
    else:
        st.caption("⏳ No scores stored yet — the next scan writes them.")

    price_range = None
    if df["price"].notna().any():
        pmin, pmax = int(df["price"].dropna().min()), int(df["price"].dropna().max())
        price_range = st.slider(
            "Price (PLN)",
            min_value=pmin, max_value=max(pmax, pmin + 1), value=(pmin, pmax), step=10_000,
        )

    area_range = None
    if df["area"].notna().any():
        amin, amax = float(df["area"].dropna().min()), float(df["area"].dropna().max())
        area_range = st.slider(
            "Area (m²)",
            min_value=amin, max_value=max(amax, amin + 1.0), value=(amin, amax), step=1.0,
        )

    search = st.text_input("Search in title", "")

# ── Apply filters ─────────────────────────────────────────────────────
# Rows with missing values are kept: an unpublished area is not evidence
# against a listing (same convention as scanner/filters.py).

mask = df["source"].isin(picked_src)
if score_range is not None:
    mask &= df["score"].isna() | df["score"].between(*score_range)
if price_range is not None:
    mask &= df["price"].isna() | df["price"].between(*price_range)
if area_range is not None:
    mask &= df["area"].isna() | df["area"].between(*area_range)
if search:
    mask &= df["title"].str.contains(search, case=False, na=False)

view = df[mask].copy()
view["ppm2"] = view.apply(
    lambda r: int(r["price"] / r["area"]) if r["price"] and r["area"] and r["area"] > 0 else None,
    axis=1,
)

# ── Summary + table ───────────────────────────────────────────────────

left, mid, right = st.columns(3)
left.metric("Shown", f"{len(view)} / {len(df)}")
if view["score"].notna().any():
    mid.metric("Best score", int(view["score"].max()))
if view["ppm2"].notna().any():
    right.metric("Median zł/m²", f"{int(view['ppm2'].median()):,}".replace(",", " "))

st.caption("Sorted by deal score (high → low), then price (low → high).")

columns = ["score", "score_reasons", "source", "title", "price", "area", "ppm2", "first_seen_at"]
if chat_id:
    columns.append("sent_at")
columns.append("url")

st.dataframe(
    view[columns],
    hide_index=True,
    height=620,
    column_config={
        "score": st.column_config.ProgressColumn(
            "★ Score", min_value=0, max_value=100, format="%d",
            help="Rule-based deal quality, 0-100",
        ),
        "score_reasons": st.column_config.TextColumn("Why", width="medium"),
        "source": st.column_config.TextColumn("Source", width="small"),
        "title": st.column_config.TextColumn("Title", width="large"),
        "price": st.column_config.NumberColumn("Price (zł)", format="%d"),
        "area": st.column_config.NumberColumn("m²", format="%.1f"),
        "ppm2": st.column_config.NumberColumn("zł/m²", format="%d"),
        "first_seen_at": st.column_config.DatetimeColumn("First seen"),
        "sent_at": st.column_config.DatetimeColumn("Delivered"),
        "url": st.column_config.LinkColumn("Open", display_text="↗"),
    },
)

if not view["score"].notna().any():
    st.info(
        "Scores are written during a scan. Listings stored before score "
        "persistence existed show blank here — new matches get a score "
        "immediately."
    )
