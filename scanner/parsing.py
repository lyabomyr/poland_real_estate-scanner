"""Text-parsing helpers shared by HTML-scraping sources."""

import re
from typing import Optional

_PRICE_RE = re.compile(r"([\d\s]+)\s*zł", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+[.,]?\d*)\s*m", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+)\s*pok", re.IGNORECASE)


def parse_price(text: str) -> Optional[int]:
    """Return PLN as int, or None. Accepts formats like '598 955 zł', '610000zł'."""
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_area(text: str) -> Optional[float]:
    """Return m² as float, or None. Accepts '41 m²', '40,60 m2'."""
    if not text:
        return None
    m = _AREA_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_rooms(text: str) -> Optional[int]:
    """Return room count as int, or None. Accepts '2 pokoje', '3pok'."""
    if not text:
        return None
    m = _ROOMS_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
