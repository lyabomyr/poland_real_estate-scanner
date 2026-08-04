import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"([\d\s]+)\s*zł", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+[.,]?\d*)\s*m", re.IGNORECASE)
_ID_FROM_URL = re.compile(r"ID([A-Za-z0-9]+)\.html")


class OlxSource(BaseSource):
    name = "olx"

    def scan(self) -> Iterable[Listing]:
        for page in range(1, self.pages + 1):
            url = self._page_url(page)
            log.info("olx: fetching page %d", page)
            try:
                html = self.fetch(url)
            except Exception as e:
                log.error("olx: fetch failed for %s: %s", url, e)
                break
            got = 0
            for l in self._parse(html):
                got += 1
                yield l
            log.info("olx: page %d yielded %d listings", page, got)
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
        cards = soup.select('[data-cy="l-card"]')
        for card in cards:
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
        # Skip cards that redirect to Otodom — those are handled by the Otodom source.
        if "otodom.pl" in url:
            return None

        _id = card.get("id") or ""
        if not _id:
            m = _ID_FROM_URL.search(url)
            _id = m.group(1) if m else url

        title_tag = card.find(["h4", "h6"])
        title = title_tag.get_text(strip=True) if title_tag else a.get_text(strip=True)

        price_tag = card.select_one('[data-testid="ad-price"]')
        price = _parse_price(price_tag.get_text(strip=True) if price_tag else "")

        area = None
        for span in card.find_all(["span", "p"]):
            txt = span.get_text(strip=True)
            if "m²" in txt or " m2" in txt.lower():
                m = _AREA_RE.search(txt)
                if m:
                    try:
                        area = float(m.group(1).replace(",", "."))
                        break
                    except ValueError:
                        pass

        loc_tag = card.select_one('[data-testid="location-date"]')
        location = loc_tag.get_text(strip=True) if loc_tag else None

        return Listing(
            source="olx",
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


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None
