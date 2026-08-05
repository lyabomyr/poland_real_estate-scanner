"""Accept/reject rules applied to every :class:`~scanner.models.Listing`.

Rejection reasons are returned as strings so we can persist them in the seen-store
and later run ``sqlite3 data/seen.db 'SELECT reject_reason, COUNT(*) ...'`` to
audit which filter is doing most of the work.
"""

import re
from typing import Iterable, Optional, Tuple

from .models import Listing


class ListingFilter:
    def __init__(
        self,
        min_area: float,
        max_price: int,
        min_build_year: Optional[int] = None,
        reject_keywords: Iterable[str] = (),
    ):
        self.min_area = min_area
        self.max_price = max_price
        self.min_build_year = min_build_year
        self.reject_keywords = list(reject_keywords)
        # Prefix-match on a word boundary — Polish inflection routinely
        # appends 1–3 chars ("udział" → "udziału", "wielkopłyt" →
        # "wielkopłytowy"). A strict full-word match would miss all these.
        # Lookbehind (not ``\b``) preserves unicode-adjacent non-word chars.
        # Same convention as :mod:`scanner.scoring` — keep them consistent.
        self._reject_patterns = [
            re.compile(rf"(?<!\w){re.escape(k)}", re.IGNORECASE)
            for k in self.reject_keywords
        ]

    def accepts(self, l: Listing) -> Tuple[bool, str]:
        """Return ``(True, "")`` if the listing passes, else ``(False, reason)``.

        Missing-value convention: unknown fields (``price=None``, ``area=None``,
        ``build_year=None``) never trigger rejection. We only reject when the
        source actually gave us a number that failed the threshold — otherwise
        we'd throw away every komornik listing (they rarely publish m²).
        """
        if l.price is not None and l.price > self.max_price:
            return False, f"price {l.price} > {self.max_price}"
        if l.area is not None and l.area < self.min_area:
            return False, f"area {l.area} < {self.min_area}"
        if self.min_build_year and l.build_year and l.build_year < self.min_build_year:
            return False, f"build_year {l.build_year} < {self.min_build_year}"

        haystack = " ".join(filter(None, [l.title, l.description, l.location]))
        for p in self._reject_patterns:
            if p.search(haystack):
                return False, f"keyword {p.pattern!r}"
        return True, ""

    def describe(self, max_area: Optional[float] = None) -> list[str]:
        """Human-readable rule list built from the same thresholds we execute."""
        rules = [
            f"reject if price is known and > {self.max_price}",
            f"reject if area is known and < {self.min_area}",
        ]
        if max_area is not None:
            rules.append(f"reject if area is known and > {max_area}")
        if self.min_build_year is not None:
            rules.append(
                f"reject if build_year is known and < {self.min_build_year}"
            )
        if self.reject_keywords:
            rules.append(
                "reject if title/description/location matches any reject keyword: "
                + ", ".join(self.reject_keywords)
            )
        rules.append("missing price / area / build_year never reject by themselves")
        return rules
