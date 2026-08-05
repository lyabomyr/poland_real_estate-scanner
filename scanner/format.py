"""Human-readable rendering of listings and groups.

Two outputs per shape:

* ``format_plain`` / ``format_group_plain`` — for the console (and dry-run
  preview). No markup.
* ``format_html`` / ``format_group_html`` — for Telegram (``parse_mode=HTML``).
  Only ``<b>`` and ``<a>`` are used, and everything user-generated goes through
  :func:`_esc` to survive Telegram's HTML parser.

Kept in its own module so future changes to the rendering don't touch the
notifier or the pipeline logic.
"""

from .aggregator import ListingGroup
from .models import Listing


def _esc(s: str) -> str:
    """Minimal HTML escape — Telegram's HTML mode only cares about `& < >`."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _price_area_row(l: Listing) -> str:
    row = []
    if l.price is not None:
        row.append(f"{l.price:,} zł".replace(",", " "))
    if l.area is not None:
        row.append(f"{l.area:g} m²")
    if l.price and l.area:
        row.append(f"{int(l.price / l.area):,} zł/m²".replace(",", " "))
    return " • ".join(row)


def _price_str(l: Listing) -> str:
    return f"{l.price:,} zł".replace(",", " ") if l.price else "—"


def _area_str(l: Listing) -> str:
    return f"{l.area:g} m²" if l.area else "—"


def _price_change_str(l: Listing) -> str:
    """"⬇️ price cut 40 000 zł (was 590 000 zł)" — empty unless re-notifying."""
    delta = l.price_delta
    if not delta:
        return ""
    arrow = "⬇️ price cut" if delta < 0 else "⬆️ price up"
    amount = f"{abs(delta):,} zł".replace(",", " ")
    was = f"{l.previous_price:,} zł".replace(",", " ")
    pct = f" ({abs(delta) / l.previous_price * 100:.0f}%)" if l.previous_price else ""
    return f"{arrow} {amount}{pct} — was {was}"


def _score_str(l: Listing) -> str:
    """Compact "★ 78/100 (…reasons…)" — empty if no score attached."""
    if not l.score:
        return ""
    reasons = ", ".join(l.score.reasons)
    return f"★ {l.score.value}/100 ({reasons})" if reasons else f"★ {l.score.value}/100"


def _sort_key(l: Listing):
    """Prefer higher score, then lower price. Listings without a score sort last."""
    score = l.score.value if l.score else -1
    return (-score, l.price or 10 ** 9)


def format_plain(l: Listing) -> str:
    parts = [f"[{l.source}] {l.title or '(no title)'}"]
    change = _price_change_str(l)
    if change:
        parts.append(change)
    row = _price_area_row(l)
    if row:
        parts.append(row)
    score = _score_str(l)
    if score:
        parts.append(score)
    if l.location:
        parts.append(f"loc: {l.location}")
    if l.build_year:
        parts.append(f"built: {l.build_year}")
    parts.append(l.url)
    return "\n".join(parts)


def format_html(l: Listing) -> str:
    prefix = "🔔 <b>PRICE CHANGE</b>\n" if l.price_delta else ""
    parts = [f"{prefix}<b>[{l.source}]</b> {_esc(l.title or '(no title)')}"]
    change = _price_change_str(l)
    if change:
        parts.append(f"<b>{_esc(change)}</b>")
    row = _price_area_row(l)
    if row:
        parts.append(row)
    score = _score_str(l)
    if score:
        parts.append(_esc(score))
    if l.location:
        parts.append(f"📍 {_esc(l.location)}")
    if l.build_year:
        parts.append(f"🏗️ {l.build_year}")
    parts.append(l.url)
    return "\n".join(parts)


def format_group_plain(g: ListingGroup) -> str:
    header = f"[{g.source}] {g.size} similar — {g.label}"
    lines = [header]
    for l in sorted(g.items, key=_sort_key):
        score = f" · ★ {l.score.value}" if l.score else ""
        lines.append(f"  • {_price_str(l)} · {_area_str(l)}{score} · {l.url}")
    return "\n".join(lines)


def format_group_html(g: ListingGroup) -> str:
    header = (
        f"<b>[{g.source}]</b> {g.size} similar listings — "
        f"{_esc(g.label.title())}"
    )
    lines = [header]
    for i, l in enumerate(sorted(g.items, key=_sort_key), start=1):
        score = f" · ★ {l.score.value}" if l.score else ""
        price = _price_str(l)
        area = _area_str(l)
        lines.append(
            f'{i}. {price} · {area}{score} · <a href="{_esc(l.url)}">otwórz</a>'
        )
    return "\n".join(lines)
