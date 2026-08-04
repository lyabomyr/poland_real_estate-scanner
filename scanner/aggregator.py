"""In-run aggregation of near-duplicate listings.

Developers frequently list every unit in a new building as a separate ad on
Morizon/OLX. Same street, same district, close prices, only the flat number
differs. Sending each as a Telegram message spams the channel — instead we
group them by a coarse location key and emit a single roll-up.
"""

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Union

from .models import Listing


@dataclass
class ListingGroup:
    key: str                          # normalized grouping key (also used as label)
    items: List[Listing] = field(default_factory=list)

    @property
    def source(self) -> str:
        return self.items[0].source if self.items else ""

    @property
    def label(self) -> str:
        return self.key

    @property
    def size(self) -> int:
        return len(self.items)


ListingOrGroup = Union[Listing, ListingGroup]


def _group_key(l: Listing) -> Optional[str]:
    """Coarse key: 'first two comma-separated parts of location'.

    ``"Sołtysowska, Czyżyny, Kraków, małopolskie"`` → ``"sołtysowska, czyżyny"``.
    Returns ``None`` for listings with no location or too-generic location
    (e.g. just ``"Kraków, małopolskie"``) so they never aggregate.
    """
    if not l.location:
        return None
    parts = [p.strip() for p in l.location.split(",") if p.strip()]
    if len(parts) < 3:
        return None
    return ", ".join(p.lower() for p in parts[:2])


def group_listings(
    listings: Iterable[Listing],
    min_group_size: int = 3,
) -> Iterator[ListingOrGroup]:
    """Yield individual listings OR groups of ``min_group_size``+ near-duplicates.

    Grouping is per source — we never mix Otodom+OLX+Morizon under one roll-up.
    """
    by_source: dict = {}
    for l in listings:
        by_source.setdefault(l.source, []).append(l)

    for _, source_listings in by_source.items():
        buckets: dict = {}
        loose: list = []
        for l in source_listings:
            k = _group_key(l)
            if k is None:
                loose.append(l)
            else:
                buckets.setdefault(k, []).append(l)

        for l in loose:
            yield l

        for key, items in buckets.items():
            if len(items) >= min_group_size:
                yield ListingGroup(key=key, items=items)
            else:
                for l in items:
                    yield l
