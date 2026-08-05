"""Per-chat config editor.

Every field shows its **effective** value — the chat's override if it has
one, otherwise the baseline default from `config.yml`. A caption under each
field says which of the two you're looking at, so an untouched chat displays
the real defaults instead of blanks.

Saving stores only genuine deviations: set a field back to its default value
and the override disappears from the JSON blob. That keeps
`chat_configs.config` readable and makes "reset" mean exactly one thing.
"""

from __future__ import annotations

import streamlit as st

from ui import chat_label, preselected_chat_id, render_connection_status
from db import (
    effective_config,
    get_repo,
    load_baseline_config,
    load_chats,
    set_chat_enabled,
    upsert_chat_override,
)
from scanner.chat_config import ChatOverride
from scanner.cities import city_keys, city_label
from scanner.introspection import format_decision_tree

st.set_page_config(page_title="Chat config — Kraków flats", page_icon="⚙️", layout="wide")
st.title("⚙️ Chat configuration")

if not render_connection_status():
    st.stop()

st.markdown(
    "Values below are what the scanner **will actually use** for the selected "
    "chat. Anything not overridden here comes from `config.yml` — the shared "
    "baseline every chat inherits. Changes apply on the next scan (≤ 15 min)."
)

chats = load_chats()
if chats.empty:
    st.info(
        "No chats registered yet. Add **@KrakowFlatsBot** to a group — it "
        "registers itself on the next scan and will appear here."
    )
    st.stop()

# ── Chat picker ───────────────────────────────────────────────────────

# Honour ?chat_id=… so the link the bot pins opens straight on this chat.
_options = chats["chat_id"].astype(str).tolist()
_preset = preselected_chat_id(chats)
picked = st.selectbox(
    "Chat",
    options=_options,
    index=_options.index(_preset) if _preset in _options else 0,
    format_func=lambda cid: chat_label(chats, cid),
)

repo = get_repo()
row = repo.get(picked)
if row is None:
    st.error("Chat vanished — reload the page.")
    st.stop()

override = row.override
baseline = load_baseline_config()
eff = effective_config(override)

# Baseline values, used both to prefill and to decide what counts as an override.
base_search = baseline.get("search") or {}
base_max_price = int(base_search.get("max_price") or 0)
base_min_area = float(base_search.get("min_area") or 0)
base_min_year = int(base_search.get("min_build_year") or 0)
base_group = int((baseline.get("notifications") or {}).get("min_group_size") or 3)
KNOWN_SOURCES = tuple((baseline.get("sources") or {}).keys())


def _origin(is_overridden: bool, default_text: str) -> None:
    """One-line provenance caption under a field."""
    if is_overridden:
        st.caption(f"🔸 overridden for this chat · default: {default_text}")
    else:
        st.caption(f"⚪️ default from config.yml ({default_text})")


# ── Search thresholds ─────────────────────────────────────────────────

st.subheader("City")
st.caption(
    "Drives every source URL. Switch it and this chat starts scanning a "
    "different market — create a second chat for Katowice and leave this one "
    "on Kraków."
)
_city_options = city_keys()
_eff_city = eff.city_key()
new_city = st.selectbox(
    "City",
    options=_city_options,
    index=_city_options.index(_eff_city) if _eff_city in _city_options else 0,
    format_func=city_label,
)
_baseline_city = (baseline.get("search") or {}).get("city") or "krakow"
_origin(override.city is not None, city_label(_baseline_city))

st.subheader("Search thresholds")
c1, c2, c3, c4 = st.columns(4)

with c1:
    new_max_price = st.number_input(
        "max_price (PLN)", min_value=0, max_value=10_000_000, step=10_000,
        value=int(eff.max_price() or 0),
    )
    _origin(override.max_price is not None, f"{base_max_price:,}".replace(",", " "))

with c2:
    new_min_area = st.number_input(
        "min_area (m²)", min_value=0.0, max_value=500.0, step=1.0,
        value=float(eff.min_area() or 0),
    )
    _origin(override.min_area is not None, f"{base_min_area:g}")

with c3:
    new_max_area = st.number_input(
        "max_area (m²)", min_value=0.0, max_value=1000.0, step=1.0,
        value=float(override.max_area or 0),
        help="Chat-only setting — the baseline has no upper area bound. 0 = no limit.",
    )
    _origin(override.max_area is not None, "no limit")

with c4:
    eff_year = eff.min_build_year()
    new_min_year = st.number_input(
        "min_build_year", min_value=0, max_value=2100, step=1,
        value=int(eff_year or 0),
        help="0 = accept any build year.",
    )
    _origin(override.min_build_year is not None, str(base_min_year or "off"))

new_group = st.number_input(
    "Group listings at the same address (min_group_size)",
    min_value=1, max_value=99, step=1,
    value=int(eff.min_group_size()),
    help=(
        "Fewer messages, never fewer flats. When this many new listings from "
        "the same portal share an address, they arrive as one message with a "
        "line per flat instead of separately. Set 99 to switch it off."
    ),
)
_origin(override.min_group_size is not None, str(base_group))
with st.expander("What a grouped message looks like"):
    st.markdown(
        "Developers post every unit of a new building as its own ad. Without "
        "grouping, twenty near-identical entries bury the one flat worth "
        "looking at.\n\n"
        "**Nothing is filtered out** — every flat keeps its own price, size, "
        "score and link, sorted best score first:"
    )
    st.code(
        "[morizon] 5 similar listings — Sołtysowska, Czyżyny\n"
        "1. 579 510 zł · 41 m² · ★ 53 · otwórz\n"
        "2. 594 935 zł · 41 m² · ★ 50 · otwórz\n"
        "3. 601 470 zł · 51 m² · ★ 74 · otwórz\n"
        "4. 604 200 zł · 42 m² · ★ 48 · otwórz\n"
        "5. 610 000 zł · 45 m² · ★ 52 · otwórz",
        language="text",
    )
    st.caption(
        "More than 20 at one address arrive as several messages “(1/4)”, "
        "“(2/4)” — Telegram rejects anything over 4096 characters, and a "
        "rejected message would be a flat you never see. The address comes "
        "from the portal: Morizon usually gives street + district, while "
        "Otodom and OLX report only city + district, so their listings rarely "
        "group at all."
    )

# ── Sources ───────────────────────────────────────────────────────────

st.subheader("Sources")
enabled_now = [s for s in KNOWN_SOURCES if s not in override.disabled_sources]
picked_sources = st.multiselect(
    "Enabled for this chat", KNOWN_SOURCES, default=enabled_now,
)
new_disabled = [s for s in KNOWN_SOURCES if s not in picked_sources]
_origin(bool(override.disabled_sources), f"all {len(KNOWN_SOURCES)} enabled")

with st.expander("Per-source URL overrides (advanced)"):
    st.caption(
        "Blank = use the baseline URL shown underneath. A custom URL applies "
        "to this chat only — handy for a different district or price band."
    )
    new_source_urls: dict = {}
    for name in KNOWN_SOURCES:
        baseline_url = ((baseline.get("sources") or {}).get(name) or {}).get("url", "")
        value = st.text_input(
            f"{name}.url", value=override.source_urls.get(name, ""), key=f"url_{name}",
        )
        if value.strip():
            new_source_urls[name] = value.strip()
        st.caption(f"⚪️ default: `{baseline_url[:110]}{'…' if len(baseline_url) > 110 else ''}`")

# ── Keywords ──────────────────────────────────────────────────────────

st.subheader("Keywords")
st.caption(
    "These **add to** the baseline lists — they don't replace them. "
    "Matched as a prefix on a word boundary, so `balkon` also catches "
    "`balkonem`/`balkony`. Optional per-keyword weight: `taras @5`."
)


def _kw_lines(entries) -> str:
    out = []
    for e in entries:
        if isinstance(e, dict):
            name, weight = e.get("name"), e.get("weight")
            out.append(f"{name} @{weight}" if weight is not None else str(name))
        else:
            out.append(str(e))
    return "\n".join(out)


def _parse_kw(text: str) -> list:
    parsed = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line:
            name, _, weight = line.rpartition("@")
            try:
                parsed.append({"name": name.strip(), "weight": int(weight.strip())})
                continue
            except ValueError:
                pass  # not a weight — treat the whole line as a keyword
        parsed.append(line)
    return parsed


k1, k2, k3 = st.columns(3)
with k1:
    new_pos = st.text_area(
        "Extra positive (+)", value=_kw_lines(override.extra_positive), height=150,
    )
    st.caption(f"⚪️ baseline already has {len(baseline.get('scoring', {}).get('positive_keywords', []))}")
with k2:
    new_neg = st.text_area(
        "Extra negative (−)", value=_kw_lines(override.extra_negative), height=150,
    )
    st.caption(f"⚪️ baseline already has {len(baseline.get('scoring', {}).get('negative_keywords', []))}")
with k3:
    new_rej = st.text_area(
        "Extra reject", value="\n".join(override.extra_reject), height=150,
    )
    st.caption(f"⚪️ baseline already has {len(baseline.get('filters', {}).get('reject_keywords', []))}")

with st.expander("Show the baseline lists this chat inherits"):
    b1, b2, b3 = st.columns(3)
    b1.markdown("**Positive**")
    b1.code(_kw_lines(baseline.get("scoring", {}).get("positive_keywords", [])) or "—")
    b2.markdown("**Negative**")
    b2.code(_kw_lines(baseline.get("scoring", {}).get("negative_keywords", [])) or "—")
    b3.markdown("**Reject**")
    b3.code("\n".join(baseline.get("filters", {}).get("reject_keywords", [])) or "—")

# ── Decision tree ─────────────────────────────────────────────────────

st.divider()
st.subheader("🌳 Decision tree")
st.caption(
    "Generated from the effective config above — the same text the bot returns "
    "for /decision_tree, so the two can never disagree."
)
with st.expander("Show how a listing is accepted, rejected or skipped", expanded=False):
    st.code(format_decision_tree(baseline, override), language="text")

with st.expander("Words that make us skip a listing entirely"):
    st.caption(
        "Any hit in title + description + location drops the listing. Matched "
        "as a prefix on a word boundary, so `udział` also catches `udziału`."
    )
    rejects = eff.reject_keywords()
    base_rejects = (baseline.get("filters") or {}).get("reject_keywords", [])
    r1, r2 = st.columns(2)
    r1.markdown(f"**Baseline ({len(base_rejects)})**")
    r1.code("\n".join(base_rejects) or "—")
    extra = [k for k in rejects if k not in base_rejects]
    r2.markdown(f"**Added for this chat ({len(extra)})**")
    r2.code("\n".join(extra) or "— none —")

# ── Save / reset ──────────────────────────────────────────────────────

st.divider()
paused = st.checkbox(
    "⏸ Pause — stop sending matches to this chat", value=override.paused,
)

col_save, col_reset, col_off = st.columns([1, 1, 1])

if col_save.button("💾 Save", type="primary"):
    # Only persist genuine deviations from the baseline: setting a field back
    # to its default removes it from the override entirely.
    new_override = ChatOverride(
        city=new_city if new_city != _baseline_city else None,
        max_price=new_max_price if new_max_price and new_max_price != base_max_price else None,
        min_area=new_min_area if new_min_area and new_min_area != base_min_area else None,
        max_area=new_max_area or None,
        min_build_year=new_min_year if new_min_year and new_min_year != base_min_year else None,
        min_group_size=new_group if new_group != base_group else None,
        disabled_sources=new_disabled,
        source_urls=new_source_urls,
        extra_positive=_parse_kw(new_pos),
        extra_negative=_parse_kw(new_neg),
        extra_reject=[ln.strip() for ln in new_rej.splitlines() if ln.strip()],
        weights=override.weights,
        paused=paused,
    )
    upsert_chat_override(picked, row.title, new_override)
    st.success("Saved. The next scan (≤ 15 min) picks this up.")
    st.rerun()

if col_reset.button("↩️ Reset to defaults"):
    # Keep only the pause flag — everything else falls back to the YAML baseline.
    upsert_chat_override(picked, row.title, ChatOverride(paused=paused))
    st.success("Reset — this chat now uses the config.yml defaults.")
    st.rerun()

if col_off.button("🗑 Unregister chat"):
    set_chat_enabled(picked, False)
    st.warning("Chat disabled. The row is kept for history.")
    st.rerun()

with st.expander("Raw override JSON (exactly what gets stored)"):
    st.caption("Empty `{}`-ish values mean 'inherit the baseline'.")
    st.code(override.to_json(), language="json")
