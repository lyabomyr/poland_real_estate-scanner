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


def format_plain(l: Listing) -> str:
    parts = [f"[{l.source}] {l.title or '(no title)'}"]
    row = _price_area_row(l)
    if row:
        parts.append(row)
    if l.location:
        parts.append(f"loc: {l.location}")
    if l.build_year:
        parts.append(f"built: {l.build_year}")
    parts.append(l.url)
    return "\n".join(parts)


def format_html(l: Listing) -> str:
    parts = [f"<b>[{l.source}]</b> {_esc(l.title or '(no title)')}"]
    row = _price_area_row(l)
    if row:
        parts.append(row)
    if l.location:
        parts.append(f"📍 {_esc(l.location)}")
    if l.build_year:
        parts.append(f"🏗️ {l.build_year}")
    parts.append(l.url)
    return "\n".join(parts)


def format_group_plain(g: ListingGroup) -> str:
    header = f"[{g.source}] {g.size} similar — {g.label}"
    lines = [header]
    for l in sorted(g.items, key=lambda x: x.price or 0):
        lines.append(f"  • {_price_str(l)} · {_area_str(l)} · {l.url}")
    return "\n".join(lines)


def format_group_html(g: ListingGroup) -> str:
    header = (
        f"<b>[{g.source}]</b> {g.size} similar listings — "
        f"{_esc(g.label.title())}"
    )
    lines = [header]
    for i, l in enumerate(sorted(g.items, key=lambda x: x.price or 0), start=1):
        price = _price_str(l)
        area = _area_str(l)
        lines.append(f'{i}. {price} · {area} · <a href="{_esc(l.url)}">otwórz</a>')
    return "\n".join(lines)
