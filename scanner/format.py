from .models import Listing


def _price_area_row(l: Listing) -> str:
    row = []
    if l.price is not None:
        row.append(f"{l.price:,} zł".replace(",", " "))
    if l.area is not None:
        row.append(f"{l.area:g} m²")
    if l.price and l.area:
        row.append(f"{int(l.price / l.area):,} zł/m²".replace(",", " "))
    return " • ".join(row)


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
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = [f"<b>[{l.source}]</b> {esc(l.title or '(no title)')}"]
    row = _price_area_row(l)
    if row:
        parts.append(row)
    if l.location:
        parts.append(f"📍 {esc(l.location)}")
    if l.build_year:
        parts.append(f"🏗️ {l.build_year}")
    parts.append(l.url)
    return "\n".join(parts)
