"""Otodom source — parses the Next.js ``__NEXT_DATA__`` blob for structured data."""

import json
import logging
import shutil
import subprocess
from typing import Iterable, Optional

from bs4 import BeautifulSoup
import requests

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)

_ITEMS_PATHS = (
    ("props", "pageProps", "data", "searchAds", "items"),
    ("props", "pageProps", "data", "listing", "items"),
    ("props", "pageProps", "searchAds", "items"),
)

#: Text CloudFront puts on its block page. Matching on it rather than on the
#: status code alone catches the case where the block ships with a 200.
_BLOCK_TEXT = "request could not be satisfied"

#: Sentinel the curl fallback appends via --write-out so the HTTP status can
#: be read off stdout. Chosen to be something no HTML page would contain.
_STATUS_MARKER = "\n__OTODOM_HTTP_STATUS__:"


def _split_status(stdout: str) -> tuple[str, int]:
    """Split curl stdout into ``(body, status)``.

    A missing or unparseable marker means curl never reported a status, which
    we treat as a failure (0) rather than optimistically as success.
    """
    body, _, raw = stdout.rpartition(_STATUS_MARKER)
    if not _:
        return stdout, 0
    try:
        return body, int(raw.strip())
    except ValueError:
        return body, 0


class OtodomSource(BaseSource):
    name = "otodom"

    URL_TEMPLATE = (
        "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/{region_slug}/{city}/{city}/{city}?priceMax={max_price}&areaMin={min_area}&limit=36&by=LATEST&direction=DESC"
    )

    def fetch(self, url: str) -> str:
        """Retry Otodom via curl when CloudFront blocks ``requests``.

        Otodom sits behind CloudFront, which fingerprints the TLS handshake
        rather than looking at the user-agent. Measured on 2026-08-05 against
        the Kraków search URL:

        ==========================  ======
        client                      status
        ==========================  ======
        ``requests`` (urllib3)      200
        ``curl``, any UA/headers    403
        ``curl --tls-max 1.2``      200
        ==========================  ======

        So ``requests`` is currently the client that gets through and curl is
        the fallback — but only with TLS pinned to 1.2, see
        :meth:`_fetch_via_curl`. Which side is blocked can flip when CloudFront
        updates its rules, which is exactly why both paths exist.
        """
        try:
            return super().fetch(url)
        except requests.HTTPError as exc:
            response = exc.response
            blocked = (
                response is not None
                and response.status_code == 403
                and _BLOCK_TEXT in (response.text or "").lower()
            )
            if not blocked:
                raise
            log.warning("otodom: requests got CloudFront 403, retrying via curl")
            return self._fetch_via_curl(url)

    def _fetch_via_curl(self, url: str) -> str:
        """Fetch via the curl binary, raising on anything that isn't a real page.

        curl exits 0 on an HTTP 403 unless told otherwise, so a naive
        ``check=True`` would hand the CloudFront block page straight to
        :meth:`_parse`. That parses to zero listings and only logs a warning,
        which reads in the logs as "Otodom had nothing today" rather than
        "Otodom is blocked". We check the status code explicitly instead, and
        re-check the body because CloudFront has served the block page with a
        200 as well.
        """
        curl_bin = shutil.which("curl")
        if not curl_bin:
            raise RuntimeError("curl is required for Otodom fallback but was not found")

        cmd = [
            curl_bin,
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            # Load-bearing, not tuning: CloudFront 403s curl's TLS 1.3
            # handshake regardless of headers, and returns 200 as soon as the
            # handshake is pinned to 1.2. Drop these two flags and the fallback
            # is a guaranteed 403. Verified 4/4 requests, 37 listings parsed.
            "--tlsv1.2",
            "--tls-max",
            "1.2",
            # Append the final status code so an HTTP-level block can't pass
            # for a page. --fail would hide the body we want for diagnostics.
            "--write-out",
            _STATUS_MARKER + "%{http_code}",
            "--max-time",
            str(self.timeout),
            url,
        ]
        user_agent = self.session.headers.get("User-Agent")
        if user_agent:
            cmd.extend(["-A", user_agent])
        accept_language = self.session.headers.get("Accept-Language")
        if accept_language:
            cmd.extend(["-H", f"Accept-Language: {accept_language}"])

        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
        )
        body, status = _split_status(result.stdout)
        if status != 200:
            raise RuntimeError(
                f"otodom: curl fallback also blocked (HTTP {status}). "
                "CloudFront is rejecting this IP, not the TLS fingerprint — "
                "a residential proxy or a scraping API is the only way through."
            )
        if _BLOCK_TEXT in body.lower():
            raise RuntimeError(
                "otodom: curl fallback returned the CloudFront block page with "
                "HTTP 200 — treat Otodom as blocked for this run."
            )
        return body

    def _parse(self, html: str) -> Iterable[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            log.warning("%s: __NEXT_DATA__ script not found", self.name)
            return
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError as e:
            log.error("%s: bad __NEXT_DATA__ json: %s", self.name, e)
            return
        items = _extract_items(data)
        if not items:
            log.warning("%s: no items in __NEXT_DATA__ (schema drift?)", self.name)
            return
        for it in items:
            listing = _to_listing(it)
            if listing:
                yield listing


def _extract_items(data: dict) -> list:
    for path in _ITEMS_PATHS:
        node = data
        try:
            for k in path:
                node = node[k]
        except (KeyError, TypeError):
            continue
        if isinstance(node, list):
            return node
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


def _pick_image(it: dict) -> Optional[str]:
    """First thumbnail from the ``images`` array (medium, else large)."""
    images = it.get("images")
    if not isinstance(images, list) or not images:
        return None
    first = images[0]
    if isinstance(first, dict):
        return first.get("medium") or first.get("large") or first.get("small")
    return first if isinstance(first, str) else None


def _to_listing(it: dict) -> Optional[Listing]:
    try:
        _id = str(it.get("id") or "")
        slug = it.get("slug") or ""
        if not _id or not slug:
            return None
        price = _pick_price(it)
        area = _pick_number(it, ["areaInSquareMeters", "area"])
        rooms = _pick_number(it, ["roomsNumber", "rooms"])
        build_year = _pick_number(it, ["buildYear", "yearBuilt"])
        return Listing(
            source="otodom",
            id=_id,
            url=f"https://www.otodom.pl/pl/oferta/{slug}",
            title=it.get("title") or "",
            price=int(price) if isinstance(price, (int, float)) else None,
            area=float(area) if isinstance(area, (int, float)) else None,
            rooms=int(rooms) if isinstance(rooms, (int, float)) else None,
            location=_pick_location(it),
            build_year=int(build_year) if isinstance(build_year, (int, float)) else None,
            description=it.get("shortDescription") or it.get("description"),
            image_url=_pick_image(it),
        )
    except Exception as e:
        log.debug("otodom: item parse error: %s", e)
        return None
