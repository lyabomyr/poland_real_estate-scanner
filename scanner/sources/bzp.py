"""BZP / eZamówienia — Polish public-procurement REST API.

Documented in *Załącznik 3 – Instrukcja integracji z API BZP*. Base URL is
``https://ezamowienia.gov.pl/mo-board/api/v1/Notice``. Overrides
:meth:`scan` because the fetch is one API call, not paged HTML.

Note: BZP is mostly tenders, not sales. Genuine "apartment for sale"
notices are rare (a handful per month). Tune ``order_object`` or ``cpv_code``
for narrower search.
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
        cpv_code: Optional[str] = None,                # e.g. 70123100 = residential sale
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
        params = self._query_params()
        log.info(
            "%s: fetching notices params=%s",
            self.name,
            {k: v for k, v in params.items()
             if k not in ("PublicationDateFrom", "PublicationDateTo")},
        )
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
            log.error("%s: fetch failed: %s", self.name, e)
            return

        if not isinstance(items, list):
            log.warning("%s: unexpected response shape: %r", self.name, type(items).__name__)
            return

        log.info("%s: got %d notice(s)", self.name, len(items))
        for it in items:
            listing = self._to_listing(it)
            if listing:
                yield listing

    def _query_params(self) -> dict:
        now = datetime.now(timezone.utc)
        params = {
            "NoticeType": self.notice_type,
            "PublicationDateFrom": (now - timedelta(days=self.days_back))
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PublicationDateTo": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PageSize": self.page_size,
        }
        for k, v in (
            ("OrderObject", self.order_object),
            ("OrganizationCity", self.organization_city),
            ("OrganizationProvince", self.organization_province),
            ("CpvCode", self.cpv_code),
        ):
            if v:
                params[k] = v
        return params

    def _to_listing(self, it: dict) -> Optional[Listing]:
        try:
            obj_id = it.get("objectId")
            if not obj_id:
                return None
            city = it.get("organizationCity") or ""
            org = it.get("organizationName") or ""
            location = ", ".join(x for x in (city, org) if x) or None
            return Listing(
                source=self.name,
                id=str(obj_id),
                url=self.DETAIL_URL.format(obj_id),
                title=it.get("orderObject") or it.get("bzpNumber") or "(no title)",
                location=location,
                description=it.get("cpvCode"),
            )
        except Exception as e:
            log.debug("bzp: item parse error: %s", e)
            return None
