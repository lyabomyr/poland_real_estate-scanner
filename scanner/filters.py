"""Accept/reject rules applied to every :class:`~scanner.models.Listing`.

Rejection reasons are returned as strings so we can persist them in the seen-store
and later run ``sqlite3 data/seen.db 'SELECT reject_reason, COUNT(*) ...'`` to
audit which filter is doing most of the work.
"""

import hashlib
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
        max_area: Optional[float] = None,
    ):
        self.min_area = min_area
        #: Upper area cap. Chat-level only — the YAML baseline has no such
        #: bound, so ``None`` means "no upper limit".
        self.max_area = max_area
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

    @classmethod
    def from_config(cls, ec) -> "ListingFilter":
        """Build from an ``EffectiveConfig`` (YAML baseline + chat override).

        The scanner and the decision-tree renderer both build their filter
        here, so what the tree describes is by construction what runs.
        """
        return cls(
            min_area=ec.min_area(),
            max_area=ec.max_area(),
            max_price=ec.max_price(),
            min_build_year=ec.min_build_year(),
            reject_keywords=ec.reject_keywords(),
        )

    def fingerprint(self) -> str:
        """Short stable hash of everything :meth:`accepts` decides on.

        Used to retire a completed sweep when the rules change. Relax a rule
        and the fingerprint moves, so the next run re-walks the whole
        back-catalogue and re-judges listings it had rejected — which is what
        makes deleting a phrase from ``reject_keywords`` reach listings buried
        on page 14, not just the two pages a routine run looks at.

        Only *loosening* really needs this, but distinguishing loosening from
        tightening is not worth the complexity: a re-sweep is a few minutes
        and yields nothing new when the rules got stricter.
        """
        payload = "|".join([
            f"{self.min_area:g}",
            "-" if self.max_area is None else f"{self.max_area:g}",
            str(self.max_price),
            str(self.min_build_year or "-"),
            ",".join(sorted(self.reject_keywords)),
        ])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

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
        if self.max_area is not None and l.area is not None and l.area > self.max_area:
            return False, f"area {l.area} > {self.max_area}"
        if self.min_build_year and l.build_year and l.build_year < self.min_build_year:
            return False, f"build_year {l.build_year} < {self.min_build_year}"

        haystack = " ".join(filter(None, [l.title, l.description, l.location]))
        for p in self._reject_patterns:
            if p.search(haystack):
                return False, f"keyword {p.pattern!r}"
        return True, ""

    def describe(self) -> list[str]:
        """Human-readable rule list built from the same thresholds we execute.

        Reads its own attributes so the decision tree shown in Telegram and on
        the dashboard cannot drift from what :meth:`accepts` actually does.
        """
        rules = [
            f"reject if price is known and > {self.max_price}",
            f"reject if area is known and < {self.min_area}",
        ]
        if self.max_area is not None:
            rules.append(f"reject if area is known and > {self.max_area}")
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
