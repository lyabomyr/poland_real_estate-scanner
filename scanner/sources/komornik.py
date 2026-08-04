"""Komornik source — parses ``__NUXT_DATA__`` (Devalue index-ref format)
from the bailiff-auctions portal ``licytacje.komornik.pl``.
"""

import json
import logging
import re
from typing import Iterable, Optional

from ..models import Listing
from ..parsing import parse_area
from .base import BaseSource

log = logging.getLogger(__name__)

_NUXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.+?)</script>', re.S
)
_LISTING_MARKERS = frozenset({"id", "title", "openingValue", "mainCategory"})
_DETAIL_URL = "https://licytacje.komornik.pl/licytacje/{}"


class KomornikSource(BaseSource):
    name = "komornik"

    def _parse(self, html: str) -> Iterable[Listing]:
        m = _NUXT_DATA_RE.search(html)
        if not m:
            log.warning("%s: __NUXT_DATA__ not found", self.name)
            return
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            log.error("%s: bad __NUXT_DATA__ json: %s", self.name, e)
            return
        for item in _extract_listings(data):
            listing = _to_listing(item)
            if listing:
                yield listing


def _extract_listings(data: list) -> list:
    """Walk the Devalue-encoded array and return resolved real-estate dicts."""
    memo: dict = {}
    n = len(data)

    def resolve(idx, stack, depth=0):
        if depth > 40 or idx in stack:
            return None
        if idx in memo:
            return memo[idx]
        stack.add(idx)
        node = data[idx] if 0 <= idx < n else None
        if isinstance(node, dict):
            result = {
                k: resolve(v, stack, depth + 1) if isinstance(v, int) and 0 <= v < n else v
                for k, v in node.items()
            }
        elif isinstance(node, list):
            result = [
                resolve(i, stack, depth + 1) if isinstance(i, int) and 0 <= i < n else i
                for i in node
            ]
        else:
            result = node
        stack.discard(idx)
        memo[idx] = result
        return result

    out = []
    for i, node in enumerate(data):
        if isinstance(node, dict) and _LISTING_MARKERS <= set(node.keys()):
            resolved = resolve(i, set())
            if isinstance(resolved, dict) and resolved.get("mainCategory") == "REAL_ESTATE":
                out.append(resolved)
    return out


def _to_listing(it: dict) -> Optional[Listing]:
    try:
        _id = it.get("id")
        if _id is None:
            return None
        title = it.get("title") or "(no title)"

        # Bailiff auctions publish the starting bid ("cena wywoławcza") — that's the
        # commit amount, so we treat it as the price.
        opening = it.get("openingValue")
        price = int(opening) if isinstance(opening, (int, float)) else None

        addr = it.get("address") or {}
        city = (addr.get("city") or "").strip()
        street = (addr.get("street") or "").strip()
        province = it.get("province") or ""
        location = ", ".join(x for x in (street, city, province) if x) or None

        area = _guess_apartment_area(title)

        estimate = it.get("estimate")
        desc_parts = [it.get("subCategory")]
        if estimate:
            desc_parts.append(f"wartość szacunkowa: {int(estimate)} zł")
        description = " | ".join(x for x in desc_parts if x)

        return Listing(
            source="komornik",
            id=str(_id),
            url=_DETAIL_URL.format(_id),
            title=title,
            price=price,
            area=area,
            location=location,
            description=description,
        )
    except Exception as e:
        log.debug("komornik: item parse error: %s", e)
        return None


def _guess_apartment_area(title: str) -> Optional[float]:
    """Best-effort m² from title text. Only accept realistic apartment sizes to
    avoid misreading '0,2200 ha' as an area."""
    area = parse_area(title or "")
    if area is None:
        return None
    if 15 <= area <= 500:
        return area
    return None
