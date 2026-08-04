"""Domain model shared by every source and the pipeline."""

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Listing:
    """One apartment offer from one source.

    ``source`` + ``id`` is the natural dedup key — the same physical apartment
    might appear on Otodom AND OLX with different ids and we treat those as two
    listings (partly by design: prices/photos can differ, partly because there
    is no reliable cross-source join key).
    """

    source: str                        # "otodom" | "olx" | "morizon" | "komornik" | "bzp"
    id: str                            # source-native id (stable across runs)
    url: str
    title: str
    price: Optional[int] = None        # PLN — None means unknown, not "free"
    area: Optional[float] = None       # m²
    rooms: Optional[int] = None
    location: Optional[str] = None     # free-form "street, district, city, region"
    build_year: Optional[int] = None
    description: Optional[str] = None

    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)
