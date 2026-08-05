"""Sortable / filterable table of every matched listing."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from db import load_seen

st.set_page_config(page_title="Listings — Kraków flats", page_icon="📋", layout="wide")
st.title("📋 Matched listings")

df = load_seen(status="matched")
if df.empty:
    st.info("No matched listings yet.")
    st.stop()

# ── Filters ───────────────────────────────────────────────────────────

with st.sidebar:
    st.subheader("Filter")
    sources = sorted(df["source"].unique().tolist())
    picked_src = st.multiselect("Source", sources, default=sources)

    price_min = int(df["price"].dropna().min()) if df["price"].notna().any() else 0
    price_max = int(df["price"].dropna().max()) if df["price"].notna().any() else 1_000_000
    price_range = st.slider(
        "Price (PLN)",
        min_value=price_min,
        max_value=price_max,
        value=(price_min, price_max),
        step=10_000,
    )

    area_min = float(df["area"].dropna().min()) if df["area"].notna().any() else 0.0
    area_max = float(df["area"].dropna().max()) if df["area"].notna().any() else 200.0
    area_range = st.slider(
        "Area (m²)",
        min_value=float(area_min),
        max_value=float(area_max),
        value=(float(area_min), float(area_max)),
        step=1.0,
    )

    search = st.text_input("Search in title", "")

# Apply filters
mask = df["source"].isin(picked_src)
if df["price"].notna().any():
    mask &= (df["price"].isna()) | df["price"].between(price_range[0], price_range[1])
if df["area"].notna().any():
    mask &= (df["area"].isna()) | df["area"].between(area_range[0], area_range[1])
if search:
    mask &= df["title"].str.contains(search, case=False, na=False)
filtered = df[mask].copy()

# Derived columns for a friendlier view
filtered["ppm2"] = filtered.apply(
    lambda r: int(r["price"] / r["area"]) if r["price"] and r["area"] and r["area"] > 0 else None,
    axis=1,
)
filtered["price"] = filtered["price"].apply(lambda v: f"{int(v):,}".replace(",", " ") if pd.notna(v) else "—")

st.caption(f"Showing **{len(filtered)}** of {len(df)} matched listings.")

st.dataframe(
    filtered[["source", "title", "price", "area", "ppm2", "fuzzy_key", "first_seen_at", "url"]],
    hide_index=True,
    use_container_width=True,
    column_config={
        "url": st.column_config.LinkColumn("URL", display_text="↗"),
        "price": st.column_config.TextColumn("Price (zł)"),
        "area":  st.column_config.NumberColumn("m²", format="%.1f"),
        "ppm2":  st.column_config.NumberColumn("zł/m²", format="%d"),
        "first_seen_at": st.column_config.DatetimeColumn("Seen at"),
    },
)
