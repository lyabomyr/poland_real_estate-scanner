"""Rule-based deal-quality scoring.

Given a :class:`Listing` and a :class:`ScoringContext` (batch statistics
computed once per run), produces a :class:`DealScore` in ``[0, 100]``.

The **formula** lives here in code (it's a function, not a value):

.. code-block:: text

    score = base
          + clamp(-delta_ppm2 / ppm2_full_at, ±1) * ppm2_weight     # price-vs-median
          + area_sweet_bonus  if area ∈ [area_sweet_min .. max]     # area sweet-spot
          + Σ hits(positive_keyword.weight)                         # amenity bonuses
          - Σ hits(negative_keyword.weight)                         # red-flag penalties
    → clamp(0..100)

Every **weight**, **threshold** and **keyword list** lives in ``scoring``
inside the YAML config — tune anything without touching Python.

The public API is minimal:

* :meth:`DealScorer.make_context` — build a per-run context from price/m²
  values (typically ``store.matched_price_per_m2()`` plus current run).
* :meth:`DealScorer.score` — produce a :class:`DealScore` for one listing.

Reasons are surfaced in the console + Telegram message. If the user can't
eyeball why a listing scored what it did, the scorer is wrong — fix the
config, don't hide the output.
"""

import re
import statistics
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Union

from .models import DealScore, Listing


# YAML keyword shapes accepted by :meth:`DealScorer` — either a plain string
# (uses ``weights.keyword`` as the weight) or an inline ``{name, weight}``.
KeywordEntry = Union[str, dict]


@dataclass
class ScoringWeights:
    """Every numeric knob the scorer uses. All configurable from YAML."""

    base: int = 50
    ppm2: int = 25
    ppm2_full_at: float = 0.20
    ppm2_reason_threshold: float = 0.03
    area_sweet_bonus: int = 5
    area_sweet_min: float = 40
    area_sweet_max: float = 60
    keyword: int = 3
    min_median_sample: int = 10


@dataclass
class KeywordRule:
    """One keyword to look for + its custom weight + its compiled matcher."""

    name: str
    weight: int
    pattern: re.Pattern = field(repr=False)


@dataclass
class ScoringContext:
    """Batch statistics reused across all scorer calls in one run."""

    median_price_per_m2: Optional[float] = None

    @classmethod
    def from_price_per_m2(
        cls,
        values: Iterable[float],
        min_sample: int = 10,
    ) -> "ScoringContext":
        """Build from an iterable of price / area ratios.

        Combines previously-persisted matches (via ``store.matched_price_per_m2``)
        with the current run so quiet ticks still get a stable baseline.
        Below ``min_sample`` values the median is unreliable and we disable
        the "% vs median" signal entirely for this run.
        """
        vals = [v for v in values if v and v > 0]
        if len(vals) < min_sample:
            return cls()
        return cls(median_price_per_m2=statistics.median(vals))


class DealScorer:
    def __init__(
        self,
        positive_kw: Iterable[KeywordEntry],
        negative_kw: Iterable[KeywordEntry],
        weights: Optional[ScoringWeights] = None,
    ):
        self.w = weights or ScoringWeights()
        self._positive = _compile_rules(positive_kw, self.w.keyword)
        self._negative = _compile_rules(negative_kw, self.w.keyword)

    def make_context(self, ppm2_values: Iterable[float]) -> ScoringContext:
        """Build the run-wide context using this scorer's ``min_median_sample``."""
        return ScoringContext.from_price_per_m2(
            ppm2_values, min_sample=self.w.min_median_sample,
        )

    def score(self, l: Listing, ctx: ScoringContext) -> DealScore:
        w = self.w
        value = w.base
        reasons: List[str] = []

        # 1. Price-per-m² vs median (skipped if we don't have enough info)
        if l.price and l.area and l.area > 0 and ctx.median_price_per_m2:
            ppm2 = l.price / l.area
            delta = (ppm2 - ctx.median_price_per_m2) / ctx.median_price_per_m2
            adj = -delta * (w.ppm2 / w.ppm2_full_at)
            adj = max(-w.ppm2, min(w.ppm2, adj))
            value += round(adj)
            if abs(delta) >= w.ppm2_reason_threshold:
                sign = "-" if delta < 0 else "+"
                reasons.append(f"{sign}{abs(delta * 100):.0f}% vs median")

        # 2. Area sweet-spot
        if l.area and w.area_sweet_min <= l.area <= w.area_sweet_max:
            value += w.area_sweet_bonus
            reasons.append("area sweet-spot")

        # 3. Keyword hits — scan title + description
        haystack = " ".join(filter(None, [l.title, l.description]))
        for rule in self._positive:
            if rule.pattern.search(haystack):
                value += rule.weight
                reasons.append(f"+{rule.name}")
        for rule in self._negative:
            if rule.pattern.search(haystack):
                value -= rule.weight
                reasons.append(f"-{rule.name}")

        return DealScore(value=max(0, min(100, value)), reasons=reasons)

    def describe_model(self) -> List[str]:
        """Readable scoring description derived from this scorer's active knobs."""
        w = self.w
        lines = [
            f"base score = {w.base}",
            (
                "if median sample count >= "
                f"{w.min_median_sample}: add price-vs-median signal up to ±{w.ppm2} "
                f"with full effect at {w.ppm2_full_at * 100:.0f}% deviation"
            ),
            (
                "surface '% vs median' reason only when deviation >= "
                f"{w.ppm2_reason_threshold * 100:.0f}%"
            ),
            (
                f"if area is known and in [{w.area_sweet_min:g}, {w.area_sweet_max:g}] "
                f"then add {w.area_sweet_bonus}"
            ),
        ]
        if self._positive:
            lines.append(
                "positive keywords: "
                + ", ".join(f"{r.name}(+{r.weight})" for r in self._positive)
            )
        if self._negative:
            lines.append(
                "negative keywords: "
                + ", ".join(f"{r.name}(-{r.weight})" for r in self._negative)
            )
        lines.append("final score = clamp(0..100)")
        return lines


# ── helpers ────────────────────────────────────────────────────────────

def _compile_rules(entries: Iterable[KeywordEntry], default_weight: int) -> List[KeywordRule]:
    """Turn ``[str | {name, weight}]`` YAML entries into :class:`KeywordRule` s."""
    rules: List[KeywordRule] = []
    for e in entries:
        if isinstance(e, str):
            name, weight = e, default_weight
        elif isinstance(e, dict):
            name = e.get("name") or e.get("keyword")
            weight = int(e.get("weight", default_weight))
        else:
            continue
        if not name:
            continue
        rules.append(KeywordRule(name=name, weight=weight, pattern=_prefix_pattern(name)))
    return rules


def _prefix_pattern(keyword: str) -> re.Pattern:
    """Compile ``keyword`` as a case-insensitive prefix match on a word boundary.

    Polish is heavily inflected — "balkon" appears as "balkonem", "windy" /
    "windą", "garaż" / "garażu". A ``\\b keyword \\b`` match would miss all
    of these. We anchor the start on a non-word char (via lookbehind) but
    leave the tail open so the keyword matches the root of any inflected
    form. Same convention as :mod:`scanner.filters` — keep them consistent.

    Trade-off: over-matches on rare compounds (e.g. "windykacja" starts with
    "windy") — acceptable because inflection is the common case.
    """
    return re.compile(rf"(?<!\w){re.escape(keyword)}", re.IGNORECASE)
