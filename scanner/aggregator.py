"""In-run aggregation of near-duplicate listings.

Developers frequently list every unit in a new building as a separate ad on
Morizon/OLX. Same street, same district, close prices, only the flat number
differs. Sending each as a Telegram message spams the channel — instead we
group them by a coarse location key and emit a single roll-up.
"""

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Union

from .models import Listing


#: Most listings in one Telegram message. A group message runs ~140 chars per
#: listing, and Telegram hard-rejects anything over 4096 — a 77-listing group
#: rendered to 10 853 chars, so sendMessage returned 400, the group was never
#: recorded as delivered, and it failed again identically on every later run.
#: Those listings were unreachable forever. 20 keeps a message near 2 800
#: chars with room for long URLs.
MAX_PER_MESSAGE = 20


@dataclass
class ListingGroup:
    key: str                          # normalized grouping key (also used as label)
    items: List[Listing] = field(default_factory=list)
    part: int = 1                     # 1-based index when a group is split
    parts: int = 1                    # how many messages this group became

    @property
    def source(self) -> str:
        return self.items[0].source if self.items else ""

    @property
    def label(self) -> str:
        """Grouping key, with a part marker when the group spans messages."""
        return self.key if self.parts == 1 else f"{self.key} ({self.part}/{self.parts})"

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
    max_per_message: int = MAX_PER_MESSAGE,
) -> Iterator[ListingOrGroup]:
    """Yield individual listings OR groups of ``min_group_size``+ near-duplicates.

    Grouping is per source — we never mix Otodom+OLX+Morizon under one roll-up.

    A location key with more than ``max_per_message`` listings is split across
    several groups rather than crammed into one oversized message. Every
    listing always ends up in exactly one yielded item: grouping changes how
    listings are *packaged*, never whether they are sent.
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
            if len(items) < min_group_size:
                for l in items:
                    yield l
                continue
            chunks = [
                items[i:i + max_per_message]
                for i in range(0, len(items), max_per_message)
            ]
            for index, chunk in enumerate(chunks, start=1):
                yield ListingGroup(
                    key=key, items=chunk, part=index, parts=len(chunks),
                )
