"""
Source for the Polish public procurement portal (BZP / eZamówienia).

API endpoint documented in "Załącznik 3 – Instrukcja integracji z API BZP".
The base URL is derived from the SPA config exposed at /mo-board/api/v1/Config.

Note: BZP mostly holds *public procurement* announcements (tenders, contracts,
services). Genuine "apartment for sale" listings are rare here compared to
Otodom/OLX — this source is worth having as a wide net for bankruptcy sales,
communal property auctions, and public entities selling flats, but expect low
volume.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from ..models import Listing
from .base import BaseSource

log = logging.getLogger(__name__)


class BzpSource(BaseSource):
    name = "bzp"

    API_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Notice"
    DETAIL_URL = "https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/{}"

    def __init__(
        self,
        user_agent: str = "",
        timeout: int = 30,
        delay: float = 2.0,
        notice_type: str = "ContractNotice",
        days_back: int = 7,
        order_object: Optional[str] = "mieszkanie",
        organization_city: Optional[str] = None,
        organization_province: Optional[str] = None,   # e.g. PL12 = małopolskie
        cpv_code: Optional[str] = None,                # e.g. 70123100 for residential sale
        page_size: int = 100,
        **_ignored,
    ):
        super().__init__(url=self.API_URL, pages=1, user_agent=user_agent,
                         timeout=timeout, delay=delay)
        self.notice_type = notice_type
        self.days_back = int(days_back)
        self.order_object = order_object
        self.organization_city = organization_city
        self.organization_province = organization_province
        self.cpv_code = cpv_code
        self.page_size = min(int(page_size), 500)

    def scan(self) -> Iterable[Listing]:
        now = datetime.now(timezone.utc)
        params = {
            "NoticeType": self.notice_type,
            "PublicationDateFrom": (now - timedelta(days=self.days_back))
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PublicationDateTo": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PageSize": self.page_size,
        }
        if self.order_object:
            params["OrderObject"] = self.order_object
        if self.organization_city:
            params["OrganizationCity"] = self.organization_city
        if self.organization_province:
            params["OrganizationProvince"] = self.organization_province
        if self.cpv_code:
            params["CpvCode"] = self.cpv_code

        log.info("bzp: fetching notices params=%s", {
            k: v for k, v in params.items() if k not in ("PublicationDateFrom", "PublicationDateTo")
        })
        try:
            r = self.session.get(
                self.API_URL,
                params=params,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            log.error("bzp: fetch failed: %s", e)
            return

        if not isinstance(items, list):
            log.warning("bzp: unexpected response shape: %r", type(items).__name__)
            return

        log.info("bzp: got %d notice(s)", len(items))
        for it in items:
            listing = self._to_listing(it)
            if listing:
                yield listing

    def _to_listing(self, it: dict) -> Optional[Listing]:
        try:
            obj_id = it.get("objectId")
            if not obj_id:
                return None
            title = it.get("orderObject") or it.get("bzpNumber") or "(no title)"
            city = it.get("organizationCity") or ""
            org = it.get("organizationName") or ""
            location = ", ".join(x for x in (city, org) if x) or None
            return Listing(
                source="bzp",
                id=str(obj_id),
                url=self.DETAIL_URL.format(obj_id),
                title=title,
                location=location,
                description=it.get("cpvCode"),
            )
        except Exception as e:
            log.debug("bzp: item parse error: %s", e)
            return None
