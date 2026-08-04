import json
import logging
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)


class OtodomSource(BaseSource):
    name = "otodom"

    def scan(self) -> Iterable[Listing]:
        for page in range(1, self.pages + 1):
            url = self._page_url(page)
            log.info("otodom: fetching page %d", page)
            try:
                html = self.fetch(url)
            except Exception as e:
                log.error("otodom: fetch failed for %s: %s", url, e)
                break
            got = 0
            for l in self._parse(html):
                got += 1
                yield l
            log.info("otodom: page %d yielded %d listings", page, got)
            if got == 0:
                break
            self._sleep()

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}page={page}"

    def _parse(self, html: str) -> Iterable[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            log.warning("otodom: __NEXT_DATA__ script not found")
            return
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError as e:
            log.error("otodom: bad __NEXT_DATA__ json: %s", e)
            return
        items = _extract_items(data)
        if not items:
            log.warning("otodom: no items in __NEXT_DATA__ (schema drift?)")
            return
        for it in items:
            listing = _to_listing(it)
            if listing:
                yield listing


def _extract_items(data: dict) -> list:
    for path in (
        ("props", "pageProps", "data", "searchAds", "items"),
        ("props", "pageProps", "data", "listing", "items"),
        ("props", "pageProps", "searchAds", "items"),
    ):
        node = data
        try:
            for k in path:
                node = node[k]
            if isinstance(node, list):
                return node
        except (KeyError, TypeError):
            continue
    return []


def _pick_price(it: dict):
    tp = it.get("totalPrice") or {}
    if isinstance(tp, dict) and tp.get("value") is not None:
        return tp["value"]
    return it.get("price")


def _pick_number(it: dict, keys):
    for k in keys:
        v = it.get(k)
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, dict) and "value" in v:
            return v["value"]
    return None


def _pick_location(it: dict) -> Optional[str]:
    loc = it.get("location") or {}
    addr = loc.get("address") or {}
    parts = []
    for key in ("street", "district", "city"):
        v = addr.get(key) or {}
        if isinstance(v, dict) and v.get("name"):
            parts.append(v["name"])
    if not parts:
        rl = loc.get("reverseGeocoding") or {}
        for level in (rl.get("locations") or []):
            if isinstance(level, dict) and level.get("fullName"):
                return level["fullName"]
    return ", ".join(parts) or None


def _to_listing(it: dict) -> Optional[Listing]:
    try:
        _id = str(it.get("id") or "")
        slug = it.get("slug") or ""
        if not _id or not slug:
            return None
        url = f"https://www.otodom.pl/pl/oferta/{slug}"
        price = _pick_price(it)
        area = _pick_number(it, ["areaInSquareMeters", "area"])
        rooms = _pick_number(it, ["roomsNumber", "rooms"])
        build_year = _pick_number(it, ["buildYear", "yearBuilt"])
        return Listing(
            source="otodom",
            id=_id,
            url=url,
            title=it.get("title") or "",
            price=int(price) if isinstance(price, (int, float)) else None,
            area=float(area) if isinstance(area, (int, float)) else None,
            rooms=int(rooms) if isinstance(rooms, (int, float)) else None,
            location=_pick_location(it),
            build_year=int(build_year) if isinstance(build_year, (int, float)) else None,
            description=it.get("shortDescription") or it.get("description"),
        )
    except Exception as e:
        log.debug("otodom: item parse error: %s", e)
        return None
