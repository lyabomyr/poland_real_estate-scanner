"""Streamlit dashboard — main page: overview KPIs + score histogram.

Streamlit's file-based routing automatically picks up any ``pages/*.py``
so navigation shows this page + everything under ``dashboard/pages/``.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from db import load_chats, load_emissions_joined, load_seen

st.set_page_config(
    page_title="Kraków flats — scanner",
    page_icon="🏢",
    layout="wide",
)

st.title("🏢 Kraków flats — scanner")
st.caption(
    "Live read from Turso. Numbers update on every scan (\\*/15 min); "
    "cached in the UI for 60 s. Refresh the page to re-fetch."
)

seen = load_seen()
if seen.empty:
    st.info(
        "No data yet — run `make run` (locally) or trigger the "
        "**scan** workflow on GitHub Actions to populate the store."
    )
    st.stop()

matched = seen[seen["status"] == "matched"].copy()
rejected = seen[seen["status"] == "rejected"]
dupes = seen[seen["status"] == "duplicate"]

# ── KPIs ──────────────────────────────────────────────────────────────

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Matched", len(matched))
col2.metric("Rejected", len(rejected))
col3.metric("Cross-source dupes", len(dupes))
if not matched.empty:
    matched["ppm2"] = matched.apply(
        lambda r: (r["price"] / r["area"]) if r["price"] and r["area"] and r["area"] > 0 else None,
        axis=1,
    )
    col4.metric("Median zł/m²", f"{int(matched['ppm2'].median()):,}".replace(",", " "))
col5.metric("Chats registered", len(load_chats()))

st.divider()

# ── Source breakdown ──────────────────────────────────────────────────

left, right = st.columns([1, 1])

with left:
    st.subheader("Matches by source")
    by_src = matched.groupby("source").size().reset_index(name="count").sort_values("count", ascending=False)
    fig = px.bar(by_src, x="source", y="count", text="count")
    fig.update_layout(height=320, showlegend=False, xaxis_title=None, yaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Reject reasons")
    if rejected.empty:
        st.write("_(nothing rejected yet)_")
    else:
        rr = rejected["reject_reason"].fillna("(unspecified)").value_counts().head(15)
        rr_df = pd.DataFrame({"reason": rr.index, "count": rr.values})
        fig = px.bar(rr_df, x="count", y="reason", orientation="h")
        fig.update_layout(height=320, yaxis={"categoryorder": "total ascending"}, xaxis_title=None, yaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)

# ── Time trend + price/m² distribution ────────────────────────────────

st.divider()
left2, right2 = st.columns([1, 1])

with left2:
    st.subheader("New matches per day")
    ts = matched.copy()
    ts["day"] = pd.to_datetime(ts["first_seen_at"]).dt.date
    per_day = ts.groupby("day").size().reset_index(name="count")
    fig = px.line(per_day, x="day", y="count", markers=True)
    fig.update_layout(height=300, xaxis_title=None, yaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

with right2:
    st.subheader("Price / m² distribution")
    if "ppm2" in matched.columns and matched["ppm2"].notna().any():
        fig = px.histogram(matched.dropna(subset=["ppm2"]), x="ppm2", nbins=30)
        fig.update_layout(height=300, xaxis_title="zł / m²", yaxis_title=None, bargap=0.02)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.write("_(not enough listings with area yet)_")

# ── Emissions by chat ─────────────────────────────────────────────────

st.divider()
st.subheader("Delivered per chat (last 30 days)")
em = load_emissions_joined()
if em.empty:
    st.write("_(no messages delivered yet)_")
else:
    recent = em[pd.to_datetime(em["sent_at"]) >= (pd.Timestamp.utcnow() - pd.Timedelta(days=30))]
    by_chat = recent.groupby("chat_id").size().reset_index(name="delivered")
    chats = load_chats()[["chat_id", "title"]]
    merged = by_chat.merge(chats, on="chat_id", how="left")
    merged = merged.rename(columns={"title": "chat_title"})[["chat_id", "chat_title", "delivered"]]
    st.dataframe(merged, use_container_width=True, hide_index=True)
