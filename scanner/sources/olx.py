"""OLX source — parses ``[data-cy="l-card"]`` cards from SSR HTML."""

import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from ..parsing import parse_area, parse_price
from .base import BaseSource

log = logging.getLogger(__name__)

_ID_FROM_URL = re.compile(r"ID([A-Za-z0-9]+)\.html")


class OlxSource(BaseSource):
    name = "olx"

    URL_TEMPLATE = (
        "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/{city}/?search%5Bfilter_float_price%3Ato%5D={max_price}&search%5Bfilter_float_m%3Afrom%5D={min_area}"
    )

    def _parse(self, html: str) -> Iterable[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select('[data-cy="l-card"]'):
            listing = _card_to_listing(card)
            if listing:
                yield listing


def _card_to_listing(card) -> Optional[Listing]:
    try:
        a = card.find("a", href=True)
        if not a:
            return None
        href = a["href"]
        url = href if href.startswith("http") else f"https://www.olx.pl{href}"
        # OLX cards that link to Otodom are handled by the Otodom source.
        if "otodom.pl" in url:
            return None

        _id = card.get("id") or ""
        if not _id:
            m = _ID_FROM_URL.search(url)
            _id = m.group(1) if m else url

        title_tag = card.find(["h4", "h6"])
        title = title_tag.get_text(strip=True) if title_tag else a.get_text(strip=True)

        price_tag = card.select_one('[data-testid="ad-price"]')
        price = parse_price(price_tag.get_text(strip=True) if price_tag else "")

        area = None
        for span in card.find_all(["span", "p"]):
            txt = span.get_text(strip=True)
            if "m²" in txt or " m2" in txt.lower():
                area = parse_area(txt)
                if area is not None:
                    break

        loc_tag = card.select_one('[data-testid="location-date"]')
        location = _clean_location(loc_tag.get_text(strip=True) if loc_tag else None)

        return Listing(
            source="olx",
            image_url=_pick_image(card),
            id=str(_id),
            url=url,
            title=title,
            price=price,
            area=area,
            location=location,
        )
    except Exception as e:
        log.debug("olx: card parse error: %s", e)
        return None


#: OLX packs place and posting time into one element, so the raw text reads
#: "Kraków, Mistrzejowice - Odświeżono dnia 03 sierpnia 2026" or
#: "Kraków, Nowa Huta - 04 sierpnia 2026" or "… - Dzisiaj o 09:12".
#: Everything from the " - " separator onwards is the timestamp.
_OLX_TIMESTAMP = re.compile(
    r"\s+-\s+(?:Odświeżono|Dzisiaj|Wczoraj|\d{1,2}\s+\w+\s+\d{4}).*$"
)


def _clean_location(raw: Optional[str]) -> Optional[str]:
    """Strip OLX's timestamp out of the location field.

    Two things go wrong if it stays. It is shown to the user as part of the
    address, and — worse — it lands in the cross-source dedup key:

        577000|43|bieżanów-prokocim -

    The key truncates to 20 chars, so the date eats the district and the same
    flat stops matching its Otodom twin the day either ad is refreshed.
    """
    if not raw:
        return None
    return _OLX_TIMESTAMP.sub("", raw).strip() or None


def _pick_image(card) -> Optional[str]:
    """First real photo in the card, skipping inline SVG placeholders."""
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http") and not src.endswith(".svg"):
            return src
    return None
