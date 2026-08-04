import logging
import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"([\d\s]+)\s*zł", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+[.,]?\d*)\s*m", re.IGNORECASE)


class ListaPrzetargowSource(BaseSource):
    """Best-effort scraper for listaprzetargow.pl monitoring-rynku page.

    The site heavily uses client-side rendering. SSR HTML may not contain all rows.
    If nothing is found, we log a warning — swap to Playwright/Selenium for full coverage.
    """

    name = "listaprzetargow"

    def scan(self) -> Iterable[Listing]:
        log.info("listaprzetargow: fetching %s", self.url)
        try:
            html = self.fetch(self.url)
        except Exception as e:
            log.error("listaprzetargow: fetch failed: %s", e)
            return
        yield from self._parse(html)

    def _parse(self, html: str) -> Iterable[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select(
            "tr[data-id], div[data-listing-id], article.listing, "
            "div.result-row, tr.offer-row, a.result-item"
        )
        if not rows:
            log.warning(
                "listaprzetargow: no listings visible in SSR HTML — "
                "the page is JS-rendered. Use a headless browser to scrape it fully."
            )
            return
        for row in rows:
            listing = _row_to_listing(row)
            if listing:
                yield listing


def _row_to_listing(row) -> Optional[Listing]:
    try:
        _id = row.get("data-id") or row.get("data-listing-id") or ""
        a = row.find("a", href=True) if hasattr(row, "find") else None
        if not a:
            return None
        href = a["href"]
        url = href if href.startswith("http") else f"https://listaprzetargow.pl{href}"
        title = a.get_text(strip=True)
        text = row.get_text(" ", strip=True)
        price = _parse_price(text)
        area = _parse_area(text)
        if not _id:
            m = re.search(r"/([^/?#]+?)(?:\?|#|$)", href)
            _id = m.group(1) if m else url
        return Listing(
            source="listaprzetargow",
            id=str(_id),
            url=url,
            title=title,
            price=price,
            area=area,
        )
    except Exception as e:
        log.debug("listaprzetargow: row parse error: %s", e)
        return None


def _parse_price(text: str) -> Optional[int]:
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
