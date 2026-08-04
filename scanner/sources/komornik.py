"""
Source for licytacje.komornik.pl — bailiff auction portal.

The search page is server-rendered by Nuxt and embeds all data in a
``<script id="__NUXT_DATA__">`` array using Devalue-style index refs.
We reconstruct the listings by resolving those refs.

Public URL pattern for a single auction: ``/licytacje/<id>``.
"""

import json
import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)

_NUXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.+?)</script>',
    re.S,
)

# regex fallback to sniff area (m²) out of the free-text title
_AREA_RE = re.compile(r"(\d+[.,]?\d*)\s*m", re.IGNORECASE)


class KomornikSource(BaseSource):
    name = "komornik"

    DETAIL_URL = "https://licytacje.komornik.pl/licytacje/{}"

    def __init__(
        self,
        url: str,
        pages: int = 1,
        user_agent: str = "",
        timeout: int = 30,
        delay: float = 2.0,
        **_ignored,
    ):
        super().__init__(url=url, pages=pages, user_agent=user_agent,
                         timeout=timeout, delay=delay)

    def scan(self) -> Iterable[Listing]:
        for page in range(1, self.pages + 1):
            url = self._page_url(page)
            log.info("komornik: fetching page %d", page)
            try:
                html = self.fetch(url)
            except Exception as e:
                log.error("komornik: fetch failed for %s: %s", url, e)
                break
            got = 0
            for listing in self._parse(html):
                got += 1
                yield listing
            log.info("komornik: page %d yielded %d listings", page, got)
            if got == 0:
                break
            self._sleep()

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}page={page}"

    def _parse(self, html: str) -> Iterable[Listing]:
        m = _NUXT_DATA_RE.search(html)
        if not m:
            log.warning("komornik: __NUXT_DATA__ not found")
            return
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            log.error("komornik: bad __NUXT_DATA__ json: %s", e)
            return

        listings = _extract_listings(data)
        for item in listings:
            listing = self._to_listing(item)
            if listing:
                yield listing

    def _to_listing(self, it: dict) -> Optional[Listing]:
        try:
            _id = it.get("id")
            if _id is None:
                return None
            title = it.get("title") or "(no title)"

            opening = it.get("openingValue")
            estimate = it.get("estimate")
            # bailiff auctions publish the starting bid ("cena wywoławcza") — that's
            # the number a buyer commits at, so we use it as price.
            price = int(opening) if isinstance(opening, (int, float)) else None

            addr = it.get("address") or {}
            city = (addr.get("city") or "").strip()
            street = (addr.get("street") or "").strip()
            province = it.get("province") or ""
            location = ", ".join(x for x in (street, city, province) if x) or None

            area = _guess_area(title)

            desc_parts = [it.get("subCategory")]
            if estimate:
                desc_parts.append(f"wartość szacunkowa: {int(estimate)} zł")
            description = " | ".join(x for x in desc_parts if x)

            return Listing(
                source="komornik",
                id=str(_id),
                url=self.DETAIL_URL.format(_id),
                title=title,
                price=price,
                area=area,
                location=location,
                description=description,
            )
        except Exception as e:
            log.debug("komornik: item parse error: %s", e)
            return None


def _extract_listings(data: list) -> list:
    """Walk the __NUXT_DATA__ index-ref array and return resolved listing dicts."""
    memo: dict = {}
    n = len(data)

    def resolve(idx: int, stack: set, depth: int = 0):
        if depth > 40:
            return None
        if idx in memo:
            return memo[idx]
        if idx in stack:
            return None
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

    listings = []
    marker_keys = {"id", "title", "openingValue", "mainCategory"}
    for i, node in enumerate(data):
        if isinstance(node, dict) and marker_keys <= set(node.keys()):
            resolved = resolve(i, set())
            if isinstance(resolved, dict) and resolved.get("mainCategory") == "REAL_ESTATE":
                listings.append(resolved)
    return listings


def _guess_area(title: str) -> Optional[float]:
    """Very rough — komornik titles occasionally contain '0,2200 ha' or '39,5m²'.
    Only accept plain m² numbers; skip 'ha' since it means hectares (land parcels)."""
    if not title:
        return None
    for m in _AREA_RE.finditer(title):
        num = m.group(1).replace(",", ".")
        # skip if preceded by 'ha' context (rough)
        try:
            v = float(num)
        except ValueError:
            continue
        # apartments realistically 15..500 m²
        if 15 <= v <= 500:
            return v
    return None
