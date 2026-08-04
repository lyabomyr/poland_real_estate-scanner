from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Listing:
    source: str
    id: str
    url: str
    title: str
    price: Optional[int] = None       # PLN
    area: Optional[float] = None      # m²
    rooms: Optional[int] = None
    location: Optional[str] = None
    build_year: Optional[int] = None
    description: Optional[str] = None

    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)
