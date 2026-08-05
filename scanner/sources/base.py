import logging
import time
from typing import Iterable, Optional
from urllib.parse import quote

import requests

from ..cities import City
from ..models import Listing

log = logging.getLogger(__name__)


class BaseSource:
    """Base class for real-estate sources.

    Default flow is ``pages``-many HTML fetches; subclasses implement
    :meth:`_parse` to yield listings from one page of HTML.

    Sources with a fundamentally different flow (single API call, POST body,
    etc.) can override :meth:`scan` and skip :meth:`_parse` entirely.
    """

    name = "base"

    #: ``str.format`` template with ``{city}`` / ``{city_label}`` /
    #: ``{region_slug}`` / ``{region_name}`` / ``{max_price}`` / ``{min_area}``
    #: placeholders. Subclasses set this so a URL can be derived from the
    #: search config instead of being hardcoded per city and price band.
    URL_TEMPLATE: Optional[str] = None

    @classmethod
    def build_url(
        cls,
        city: City,
        *,
        max_price: Optional[int] = None,
        min_area: Optional[float] = None,
    ) -> Optional[str]:
        """Render :attr:`URL_TEMPLATE` for a city + search thresholds.

        Returns None when the subclass has no template (e.g. an API-backed
        source that doesn't use URLs at all), letting the caller fall back to
        an explicitly configured URL.

        Values that appear inside a query string are percent-encoded — Polish
        voivodeship names carry diacritics that must not go out raw.
        """
        if not cls.URL_TEMPLATE:
            return None
        return cls.URL_TEMPLATE.format(
            city=city.key,
            city_label=quote(city.label),
            region_slug=city.region_slug,
            region_name=quote(city.region_name),
            max_price="" if max_price is None else int(max_price),
            min_area="" if min_area is None else _fmt_area(min_area),
        )

    def __init__(
        self,
        url: str = "",
        pages: int = 1,
        user_agent: str = "",
        timeout: int = 30,
        delay: float = 2.0,
        **_extra,
    ):
        self.url = url
        self.pages = int(pages)
        self.timeout = int(timeout)
        self.delay = float(delay)
        #: True once :meth:`scan` has walked the result set to its end. False
        #: after a fetch failure cut the walk short. Callers doing a one-off
        #: full sweep must check this: scan() swallows fetch errors so one
        #: source can't kill a run, which without this flag makes a truncated
        #: walk indistinguishable from a complete one.
        self.scan_completed = False
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pl,en;q=0.8",
        })

    # --- public --------------------------------------------------------

    #: Hard stop for an unlimited (``pages=0``) sweep. Portals happily serve
    #: page 999 by wrapping around or repeating, so a "page yielded nothing"
    #: exit condition alone could loop forever. No Polish city has anywhere
    #: near this many pages of flats in one price band.
    MAX_PAGES = 100

    def scan(self) -> Iterable[Listing]:
        # pages=0 means "walk until a page comes back empty" — used for the
        # first sweep of a URL, where capping at 2 would leave most of the
        # market unseen. Steady-state runs use the small configured cap
        # because both portals sort newest-first.
        self.scan_completed = False
        unlimited = self.pages <= 0
        last = self.MAX_PAGES if unlimited else self.pages
        seen_ids: set[str] = set()
        for page in range(1, last + 1):
            url = self._page_url(page)
            log.info("%s: fetching page %d", self.name, page)
            try:
                html = self.fetch(url)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if page > 1 and status in (404, 410):
                    # Walking off the end of the result set. Morizon answers
                    # the page after the last one with a 404 rather than an
                    # empty list, and that is a completed walk, not a failure.
                    log.info("%s: page %d is past the last page (%s)", self.name, page, status)
                    self.scan_completed = True
                    return
                log.error("%s: fetch failed for %s: %s", self.name, url, e)
                return
            except Exception as e:
                # Deliberately not re-raised: one flaky portal must not abort
                # the whole run. scan_completed stays False so a caller doing
                # a first sweep knows the deep pages went unread.
                log.error("%s: fetch failed for %s: %s", self.name, url, e)
                return
            got = 0
            fresh = 0
            for listing in self._parse(html):
                got += 1
                # Only yield ids we haven't already handed out in this scan.
                # On a well-behaved portal nothing repeats and this is a
                # no-op; it matters for sources that ignore ?page= and for
                # listings that shift across a page boundary mid-walk.
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                fresh += 1
                yield listing
            log.info("%s: page %d yielded %d listings (%d new)", self.name, page, got, fresh)
            if got == 0:
                self.scan_completed = True
                return
            if fresh == 0:
                # Every id repeats one we already have, so this source ignores
                # our page parameter and is serving page 1 forever. Komornik
                # does exactly this. Without the check an unlimited sweep
                # would refetch the same 20 rows MAX_PAGES times.
                log.info(
                    "%s: page %d repeated the previous results — pagination "
                    "is not supported here, stopping", self.name, page,
                )
                self.scan_completed = True
                return
            self._sleep()
        if unlimited:
            log.warning(
                "%s: stopped at the %d-page safety cap without hitting an "
                "empty page — some listings may be unread",
                self.name, last,
            )
        self.scan_completed = True

    def fetch(self, url: str) -> str:
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    # --- hooks ---------------------------------------------------------

    def _parse(self, html: str) -> Iterable[Listing]:  # pragma: no cover
        raise NotImplementedError(
            f"{type(self).__name__} must implement _parse() or override scan()"
        )

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}page={page}"

    def _sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay)


def _fmt_area(value: float) -> str:
    """39.0 -> "39" so URLs stay clean; 39.5 -> "39.5"."""
    return str(int(value)) if float(value).is_integer() else str(value)
