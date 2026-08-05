"""Domain model shared by every source and the pipeline."""

from dataclasses import dataclass, field
from typing import List, Optional

# Location tokens that carry no signal for dedup ("everything in Kraków is
# in Kraków"). Stripped before building :attr:`Listing.fuzzy_key`.
_GENERIC_LOC_TOKENS = frozenset({
    "kraków", "krakow", "małopolskie", "malopolskie", "polska", "poland"
})


@dataclass
class DealScore:
    """A 0–100 "how interesting is this deal" score with reasons.

    ``value`` is clamped to [0, 100]. ``reasons`` is a short list of
    human-readable contribution tags, e.g. ``["-15% vs median", "+balkon",
    "-do remontu"]`` — surfaced in the console + Telegram message so the user
    can sanity-check the number.
    """
    value: int
    reasons: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        joined = ", ".join(self.reasons) if self.reasons else "no signals"
        return f"{self.value}/100 ({joined})"


@dataclass
class Listing:
    """One apartment offer from one source.

    Two dedup keys are exposed:

    * :attr:`dedup_key` — ``"<source>:<id>"``. Same-source strict identity.
    * :attr:`fuzzy_key` — ``"<price>|<area_int>|<loc_prefix>"``. Cross-source
      probable-identity — same physical apartment listed on Otodom AND
      Morizon collapses if price + rounded area + street/district match.
      Returns ``None`` when we don't have enough info (missing price / area /
      only city known) — those listings pass through without cross-source
      dedup.
    """

    source: str                        # "otodom" | "olx" | "morizon" | "komornik"
    id: str                            # source-native id (stable across runs)
    url: str
    title: str
    price: Optional[int] = None        # PLN — None means unknown, not "free"
    area: Optional[float] = None       # m²
    rooms: Optional[int] = None
    location: Optional[str] = None     # free-form "street, district, city, region"
    build_year: Optional[int] = None
    description: Optional[str] = None

    # Set by the scoring phase (main.py) once per run. Not part of the natural
    # source data — treat as derived.
    score: Optional[DealScore] = None

    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def fuzzy_key(self) -> Optional[str]:
        if self.price is None or self.area is None or not self.location:
            return None
        parts = [p.strip().lower() for p in self.location.split(",") if p.strip()]
        # Drop city/region tokens — they add no dedup signal for a
        # single-city scanner.
        parts = [p for p in parts if p not in _GENERIC_LOC_TOKENS]
        if not parts:
            return None
        # Take the most specific part (usually the street; district if street
        # isn't published). Slice to 20 chars so trailing punctuation doesn't
        # split otherwise-equal keys.
        loc = parts[0][:20]
        return f"{self.price}|{int(round(self.area))}|{loc}"
