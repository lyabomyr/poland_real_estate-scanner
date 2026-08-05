"""Per-chat config editor — the UI counterpart to the Telegram commands.

Everything you edit here writes to the ``chat_configs`` row for the
selected chat. The scanner picks up changes on its next run, or immediately
if you trigger ``/scan`` from Telegram.
"""

from __future__ import annotations

import streamlit as st

from ui import render_connection_status
from db import get_repo, load_chats, set_chat_enabled, upsert_chat_override
from scanner.chat_config import ChatOverride

st.set_page_config(page_title="Chat config — Kraków flats", page_icon="⚙️", layout="wide")
st.title("⚙️ Chat configuration")

if not render_connection_status():
    st.stop()

st.markdown(
    """
    Each chat inherits every field from `config.yml` (the *baseline*).
    Rows below live in the **`chat_configs`** table — each field you set
    here **overrides** the baseline **for that chat only**.

    Leave a numeric field at 0 (or empty) to fall back to baseline.
    """
)

chats = load_chats()
if chats.empty:
    st.info(
        "No chats registered yet. Add `@KrakowFlatsBot` to a group — the next scan will "
        "register it here automatically. Or seed one via `config.yml → telegram.chat_id`."
    )
    st.stop()

# ── Chat picker ───────────────────────────────────────────────────────

options = {
    row["chat_id"]: f"{row['title'] or '(no title)'}  —  {row['chat_id']}"
    for _, row in chats.iterrows()
}
picked = st.selectbox("Chat", options=list(options.keys()), format_func=lambda k: options[k])

repo = get_repo()
row = repo.get(picked)
if row is None:
    st.error("Chat vanished — refresh the page.")
    st.stop()

override = row.override

# ── Row 1: numeric knobs ──────────────────────────────────────────────

st.subheader("Numeric overrides")
c1, c2, c3, c4 = st.columns(4)
new_max_price = c1.number_input(
    "max_price (PLN)",
    min_value=0, max_value=10_000_000, step=10_000,
    value=int(override.max_price or 0),
    help="0 = inherit baseline",
)
new_min_area = c2.number_input(
    "min_area (m²)",
    min_value=0.0, max_value=500.0, step=1.0,
    value=float(override.min_area or 0),
    help="0 = inherit baseline",
)
new_max_area = c3.number_input(
    "max_area (m²)",
    min_value=0.0, max_value=1000.0, step=1.0,
    value=float(override.max_area or 0),
    help="0 = no upper limit",
)
new_min_year = c4.number_input(
    "min_build_year",
    min_value=0, max_value=2100, step=1,
    value=int(override.min_build_year or 0),
    help="0 = inherit baseline (probably off)",
)

# ── Row 2: sources ────────────────────────────────────────────────────

st.subheader("Sources")
KNOWN = ("otodom", "olx", "morizon", "komornik")
enabled_now = [s for s in KNOWN if s not in override.disabled_sources]
picked_sources = st.multiselect("Enabled sources", KNOWN, default=enabled_now)
new_disabled = [s for s in KNOWN if s not in picked_sources]

with st.expander("Per-source URL overrides (advanced)"):
    st.caption("Leave blank to use the baseline URL. Custom URL only applies to this chat.")
    new_source_urls: dict = {}
    for s in KNOWN:
        current = override.source_urls.get(s, "")
        v = st.text_input(f"{s}.url", value=current, key=f"src_url_{s}")
        if v.strip():
            new_source_urls[s] = v.strip()

# ── Row 3: keywords ───────────────────────────────────────────────────

st.subheader("Extra keywords for this chat")


def _kw_lines(entries) -> str:
    lines = []
    for e in entries:
        if isinstance(e, dict):
            n, w = e.get("name"), e.get("weight")
            lines.append(f"{n} @{w}" if w is not None else str(n))
        else:
            lines.append(str(e))
    return "\n".join(lines)


def _parse_kw(text: str) -> list:
    """Parse ``one keyword per line`` — plain string OR ``name @weight``."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line:
            name, _, w = line.rpartition("@")
            name = name.strip()
            try:
                weight = int(w.strip())
            except ValueError:
                out.append(line)
                continue
            out.append({"name": name, "weight": weight})
        else:
            out.append(line)
    return out


c1, c2, c3 = st.columns(3)
new_pos_text = c1.text_area(
    "Positive (+): one per line, optional `name @weight`",
    value=_kw_lines(override.extra_positive),
    height=140,
    help="Adds to baseline scoring.positive_keywords. Prefix-match on word boundary.",
)
new_neg_text = c2.text_area(
    "Negative (−): one per line, optional `name @weight`",
    value=_kw_lines(override.extra_negative),
    height=140,
    help="Adds to baseline scoring.negative_keywords. Subtracted from the score.",
)
new_rej_text = c3.text_area(
    "Reject: one per line",
    value="\n".join(override.extra_reject),
    height=140,
    help="Adds to baseline filters.reject_keywords. Any hit → listing dropped entirely.",
)

# ── Save / actions ────────────────────────────────────────────────────

st.divider()
paused = st.checkbox("⏸ Pause — stop sending matches to this chat", value=override.paused)

col_save, col_reset, col_disable = st.columns([1, 1, 1])
if col_save.button("💾 Save overrides", type="primary"):
    new_override = ChatOverride(
        max_price=new_max_price or None,
        min_area=new_min_area or None,
        max_area=new_max_area or None,
        min_build_year=new_min_year or None,
        disabled_sources=new_disabled,
        source_urls=new_source_urls,
        extra_positive=_parse_kw(new_pos_text),
        extra_negative=_parse_kw(new_neg_text),
        extra_reject=[l.strip() for l in new_rej_text.splitlines() if l.strip()],
        weights=override.weights,  # weight editing is command-line only for now
        min_group_size=override.min_group_size,
        paused=paused,
    )
    upsert_chat_override(picked, row.title, new_override)
    st.success("Saved. The next scan will pick this up, or run /scan in Telegram.")
    st.rerun()

if col_reset.button("↩ Reset all overrides"):
    upsert_chat_override(picked, row.title, ChatOverride(paused=paused))
    st.success("All overrides cleared; only pause state kept.")
    st.rerun()

if col_disable.button("🗑 Unregister chat"):
    set_chat_enabled(picked, False)
    st.warning("Chat disabled (row kept for history — set `enabled=1` in DB to restore).")
    st.rerun()

# ── Live preview of the JSON blob ─────────────────────────────────────

with st.expander("Raw override JSON (what gets persisted)"):
    st.code(override.to_json(), language="json")
