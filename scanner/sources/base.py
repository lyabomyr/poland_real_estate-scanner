import logging
import time
from typing import Iterable

import requests

from ..models import Listing

log = logging.getLogger(__name__)


class BaseSource:
    name = "base"

    def __init__(
        self,
        url: str,
        pages: int = 1,
        user_agent: str = "",
        timeout: int = 30,
        delay: float = 2.0,
    ):
        self.url = url
        self.pages = pages
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pl,en;q=0.8",
        })

    def fetch(self, url: str) -> str:
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def scan(self) -> Iterable[Listing]:
        raise NotImplementedError

    def _sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay)
