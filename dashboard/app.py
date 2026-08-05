"""Overview page: what the scanner is finding, and the current best offers.

Everything is scoped by the Target picker — "All chats" for the whole store,
or one chat for exactly what that chat was notified about. Chats with
different price/area/city overrides legitimately see different markets, so a
global-only view would be misleading.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ui import render_connection_status, target_picker
from db import load_chats, load_listings_full, load_price_history

st.set_page_config(page_title="Kraków flats — scanner", page_icon="🏢", layout="wide")

st.title("🏢 Flat scanner — overview")
st.caption(
    "Live read from Turso — the same database the scanner writes to. Updated "
    "on every scan (\\*/15 min), cached in the UI for 60 s."
)

if not render_connection_status():
    st.stop()

chat_id, chat_label = target_picker(load_chats())
df = load_listings_full(chat_id)

if df.empty:
    st.info(
        f"Nothing delivered to **{chat_label}** yet."
        if chat_id else
        "No matched listings yet — trigger the **scan** workflow and refresh."
    )
    st.stop()

# Derived once, reused by every panel below.
df["ppm2"] = df.apply(
    lambda r: (r["price"] / r["area"]) if r["price"] and r["area"] and r["area"] > 0 else None,
    axis=1,
)
df["day"] = pd.to_datetime(df["first_seen_at"], errors="coerce").dt.date

# ── KPI row ───────────────────────────────────────────────────────────

ppm2 = df["ppm2"].dropna()
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Listings", len(df))
if not ppm2.empty:
    k2.metric("Median zł/m²", f"{int(ppm2.median()):,}".replace(",", " "))
    k3.metric("Cheapest zł/m²", f"{int(ppm2.min()):,}".replace(",", " "))
    k4.metric("Priciest zł/m²", f"{int(ppm2.max()):,}".replace(",", " "))
if df["score"].notna().any():
    k5.metric("Best score", int(df["score"].max()))

st.divider()

# ── Top 3 offers ──────────────────────────────────────────────────────

st.subheader("🏆 Top 3 right now")
st.caption("Ranked by deal score, then by price. Images come straight from the portal.")

top = df[df["score"].notna()].head(3) if df["score"].notna().any() else df.head(3)
if top.empty:
    st.info("No listings to rank yet.")
else:
    for col, (_, row) in zip(st.columns(len(top)), top.iterrows()):
        with col:
            if row.get("image_url"):
                st.image(row["image_url"], use_container_width=True)
            else:
                st.markdown(
                    "<div style='height:150px;display:flex;align-items:center;"
                    "justify-content:center;background:#26292f;border-radius:8px;"
                    "color:#888'>no photo</div>",
                    unsafe_allow_html=True,
                )
            score = int(row["score"]) if pd.notna(row.get("score")) else None
            st.markdown(f"### {'★ ' + str(score) if score is not None else '—'}")
            st.markdown(f"**{(row['title'] or '(no title)')[:90]}**")

            bits = []
            if pd.notna(row["price"]):
                bits.append(f"**{int(row['price']):,}".replace(",", " ") + " zł**")
            if pd.notna(row["area"]):
                bits.append(f"{row['area']:g} m²")
            if pd.notna(row["ppm2"]):
                bits.append(f"{int(row['ppm2']):,}".replace(",", " ") + " zł/m²")
            st.markdown(" · ".join(bits))

            if row.get("location"):
                st.caption(f"📍 {row['location']}")
            if row.get("score_reasons"):
                st.caption(f"why: {row['score_reasons']}")
            if row.get("description"):
                st.caption(str(row["description"])[:160])
            st.markdown(f"[Open on {row['source']} ↗]({row['url']})")

st.divider()

# ── Time + location ───────────────────────────────────────────────────

left, right = st.columns(2)

with left:
    st.subheader("New listings per day")
    per_day = df.dropna(subset=["day"]).groupby("day").size().reset_index(name="count")
    if per_day.empty:
        st.write("_(no dated listings)_")
    else:
        fig = px.bar(per_day, x="day", y="count", text="count")
        fig.update_layout(height=330, xaxis_title=None, yaxis_title=None, bargap=0.25)
        st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Where they are")
    st.caption("Grouped by district — the second part of the listing's address.")
    # "Sołtysowska, Czyżyny, Kraków, małopolskie" -> "Czyżyny". Falls back to
    # the first token when a listing only carries one.
    def _district(loc: str | None) -> str | None:
        if not loc:
            return None
        parts = [p.strip() for p in str(loc).split(",") if p.strip()]
        return (parts[1] if len(parts) > 1 else parts[0]) if parts else None

    districts = df["location"].map(_district).dropna()
    if districts.empty:
        st.info("No location data stored yet — it's written by the next scan.")
    else:
        counts = districts.value_counts().head(12).reset_index()
        counts.columns = ["district", "count"]
        fig = px.bar(counts, x="count", y="district", orientation="h", text="count")
        fig.update_layout(
            height=330, yaxis={"categoryorder": "total ascending"},
            xaxis_title=None, yaxis_title=None,
        )
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# ── Price per m² ──────────────────────────────────────────────────────

st.subheader("Price per m²")
if ppm2.empty:
    st.info("Not enough listings with both price and area yet.")
else:
    c1, c2 = st.columns([3, 2])
    with c1:
        fig = px.histogram(df.dropna(subset=["ppm2"]), x="ppm2", nbins=30)
        fig.add_vline(
            x=ppm2.median(), line_dash="dash",
            annotation_text="median", annotation_position="top",
        )
        fig.update_layout(height=330, xaxis_title="zł / m²", yaxis_title=None, bargap=0.02)
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.markdown("**Distribution**")
        st.dataframe(
            pd.DataFrame({
                "metric": ["count", "min", "25%", "median", "75%", "max", "mean"],
                "zł/m²": [
                    int(ppm2.count()), int(ppm2.min()), int(ppm2.quantile(.25)),
                    int(ppm2.median()), int(ppm2.quantile(.75)), int(ppm2.max()),
                    int(ppm2.mean()),
                ],
            }),
            hide_index=True, use_container_width=True,
        )

    st.markdown("**By source** — median zł/m² and spread")
    by_src = (
        df.dropna(subset=["ppm2"]).groupby("source")["ppm2"]
        .agg(count="count", min="min", median="median", max="max")
        .round(0).astype(int).reset_index()
    )
    st.dataframe(by_src, hide_index=True, use_container_width=True)

# ── Price changes ─────────────────────────────────────────────────────

st.divider()
st.subheader("💸 Recent price changes")
st.caption(
    "Portals edit prices in place, keeping the same listing id. The scanner "
    "notices and re-notifies — these are those moves."
)
history = load_price_history()
if history.empty:
    st.info(
        "No price changes recorded yet. This fills in once a listing the "
        "scanner already knows about changes price."
    )
else:
    history["delta"] = history["new_price"] - history["old_price"]
    history["pct"] = (history["delta"] / history["old_price"] * 100).round(1)
    st.dataframe(
        history[["changed_at", "source", "title", "old_price", "new_price", "delta", "pct", "url"]].head(50),
        hide_index=True,
        column_config={
            "changed_at": st.column_config.DatetimeColumn("When"),
            "source": st.column_config.TextColumn("Source", width="small"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "old_price": st.column_config.NumberColumn("Was", format="%d"),
            "new_price": st.column_config.NumberColumn("Now", format="%d"),
            "delta": st.column_config.NumberColumn("Δ zł", format="%d"),
            "pct": st.column_config.NumberColumn("Δ %", format="%.1f"),
            "url": st.column_config.LinkColumn("Open", display_text="↗"),
        },
    )

# ── Source mix ────────────────────────────────────────────────────────

st.divider()
st.subheader("Where listings come from")
by_source = df.groupby("source").size().reset_index(name="count").sort_values("count", ascending=False)
fig = px.bar(by_source, x="source", y="count", text="count")
fig.update_layout(height=300, showlegend=False, xaxis_title=None, yaxis_title=None)
st.plotly_chart(fig, use_container_width=True)
