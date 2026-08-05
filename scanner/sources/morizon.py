"""Morizon source — parses SSR HTML cards with ``data-cy`` attributes."""

import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from ..parsing import parse_area, parse_price, parse_rooms
from .base import BaseSource

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"(mzn\d+)")


class MorizonSource(BaseSource):
    name = "morizon"

    URL_TEMPLATE = (
        "https://www.morizon.pl/mieszkania/najnowsze/{city}/?ps%5Bprice_to%5D={max_price}&ps%5Bliving_area_from%5D={min_area}"
    )

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
        price = parse_price(price_tag.get_text(" ", strip=True) if price_tag else "")

        info_tag = card.select_one('[data-cy="propertyCardInfo"]')
        info_text = info_tag.get_text(" ", strip=True) if info_tag else ""

        return Listing(
            source="morizon",
            id=str(_id),
            url=url,
            title=title,
            price=price,
            area=parse_area(info_text),
            rooms=parse_rooms(info_text),
            location=location,
        )
    except Exception as e:
        log.debug("morizon: card parse error: %s", e)
        return None
