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
        self._reject_patterns = [
            re.compile(rf"(?<!\w){re.escape(k)}(?!\w)", re.IGNORECASE)
            for k in reject_keywords
        ]

    def accepts(self, l: Listing) -> Tuple[bool, str]:
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
