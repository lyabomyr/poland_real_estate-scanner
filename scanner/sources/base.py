import logging
import time
from typing import Iterable

import requests

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
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pl,en;q=0.8",
        })

    # --- public --------------------------------------------------------

    def scan(self) -> Iterable[Listing]:
        for page in range(1, self.pages + 1):
            url = self._page_url(page)
            log.info("%s: fetching page %d", self.name, page)
            try:
                html = self.fetch(url)
            except Exception as e:
                log.error("%s: fetch failed for %s: %s", self.name, url, e)
                return
            got = 0
            for listing in self._parse(html):
                got += 1
                yield listing
            log.info("%s: page %d yielded %d listings", self.name, page, got)
            if got == 0:
                return
            self._sleep()

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
