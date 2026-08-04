"""
Source for morizon.pl — one of the older PL real-estate aggregators.

Parses SSR HTML: 35 cards per page in ``.card`` elements with predictable
``data-cy`` attributes.
"""

import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"([\d\s]+)\s*zł", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+[.,]?\d*)\s*m", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+)\s*pok", re.IGNORECASE)
_ID_RE = re.compile(r"(mzn\d+)")


class MorizonSource(BaseSource):
    name = "morizon"

    def scan(self) -> Iterable[Listing]:
        for page in range(1, self.pages + 1):
            url = self._page_url(page)
            log.info("morizon: fetching page %d", page)
            try:
                html = self.fetch(url)
            except Exception as e:
                log.error("morizon: fetch failed for %s: %s", url, e)
                break
            got = 0
            for listing in self._parse(html):
                got += 1
                yield listing
            log.info("morizon: page %d yielded %d listings", page, got)
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
        for card in soup.select(".card"):
            listing = _card_to_listing(card)
            if listing:
                yield listing


def _card_to_listing(card) -> Optional[Listing]:
    try:
        a = card.select_one('a[data-cy="propertyUrl"]') or card.find("a", href=True)
        if not a or not a.get("href"):
            return None
        href = a["href"]
        url = href if href.startswith("http") else f"https://www.morizon.pl{href}"

        m = _ID_RE.search(href)
        _id = m.group(1) if m else href.rsplit("/", 1)[-1]

        title_tag = card.select_one('[data-cy="propertyCardTitle"]')
        title = title_tag.get_text(strip=True) if title_tag else ""

        loc_tag = card.select_one('[data-cy="propertyCardLocation"]')
        location = loc_tag.get_text(strip=True) if loc_tag else None

        price_tag = card.select_one('[data-cy="cardPropertyOfferPrice"]')
        price = _parse_price(price_tag.get_text(" ", strip=True) if price_tag else "")

        info_tag = card.select_one('[data-cy="propertyCardInfo"]')
        info_text = info_tag.get_text(" ", strip=True) if info_tag else ""
        area = _parse_area(info_text)
        rooms = _parse_rooms(info_text)

        return Listing(
            source="morizon",
            id=str(_id),
            url=url,
            title=title,
            price=price,
            area=area,
            rooms=rooms,
            location=location,
        )
    except Exception as e:
        log.debug("morizon: card parse error: %s", e)
        return None


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_area(text: str) -> Optional[float]:
    m = _AREA_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_rooms(text: str) -> Optional[int]:
    m = _ROOMS_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
