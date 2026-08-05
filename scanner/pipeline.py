"""Multi-chat scan pipeline.

.. code-block:: text

    ┌─────────┐   ┌────────┐   ┌────────────┐        ┌───────────┐
    │  scan + │──▶│ global │──▶│ per-chat:  │──…──▶  │ per-chat: │
    │  fetch  │   │ dedup  │   │ filter     │        │ score     │
    │ (union) │   │ (seen) │   │ + fuzzy    │        │ + emit    │
    └─────────┘   └────────┘   └────────────┘        └───────────┘

Multi-tenant model
------------------
One "chat context" per enabled row in ``chat_configs``. Each context owns
its own :class:`ListingFilter`, :class:`DealScorer`, source list and
group-size threshold (composed from YAML baseline + the chat's overrides).

Scanning is per-context — different chats may point their sources at
different URLs, so we can't safely share fetch output between them. For
identical URLs this is 1-2 seconds extra per repeat; users typically run
1-3 chats. If that becomes a bottleneck, cache by ``(source_name, url)``
inside the loop.

Global state (dedup, chat emissions, greeting log) lives in the SQL store
and is passed in through :class:`SeenStore` + :class:`ChatConfigRepo`.
Dependency-injection everywhere so each pipeline is stateless-per-run and
easy to unit-test.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from logging import getLogger
from typing import Dict, List, Optional

from .aggregator import ListingGroup, group_listings
from .chat_config import EffectiveConfig
from .chat_repo import ChatConfigRepo, ChatRow
from .filters import ListingFilter
from .format import format_group_plain, format_plain
from .models import Listing
from .scoring import DealScorer, ScoringWeights
from .sources.base import BaseSource
from .telegram import TelegramNotifier

log = getLogger(__name__)


@dataclass
class RunStats:
    """Global counters across every chat in one pipeline run."""
    seen: int = 0
    already_seen: int = 0
    rejected: int = 0
    matched: int = 0        # aggregate across all chats
    cross_dup: int = 0
    sent: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatContext:
    """One chat's effective pipeline: filter + scorer + sources + destination."""
    chat_id: str
    title: Optional[str]
    filter: ListingFilter
    scorer: Optional[DealScorer]
    sources: List[BaseSource]
    min_group_size: int
    notifier: TelegramNotifier


class MultiChatPipeline:
    """Runs the whole flow once per :class:`ChatContext`.

    Same store/repo shared across contexts (persistent dedup + emission
    tracking). Aggregation is per-chat because different chats can have
    different filter overrides — a match in one chat might be rejected in
    another.
    """

    def __init__(
        self,
        contexts: List[ChatContext],
        store,                     # SeenStore
        repo: ChatConfigRepo,
        dry_run: bool = False,
    ):
        self.contexts = contexts
        self.store = store
        self.repo = repo
        self.dry_run = dry_run
        self.stats = RunStats()

    def run(self) -> RunStats:
        for ctx in self.contexts:
            if not ctx.sources:
                log.info("chat %s: no sources enabled — skipping", ctx.chat_id)
                continue
            log.info("=== chat %s (%s) ===", ctx.chat_id, ctx.title or "")
            matched = self._scan_and_filter(ctx)
            matched = self._cross_source_dedup(ctx, matched)
            if ctx.scorer:
                self._score(ctx, matched)
            self._emit(ctx, matched)
        return self.stats

    # ── phase 1 ────────────────────────────────────────────────────────

    def _scan_and_filter(self, ctx: ChatContext) -> Dict[str, List[Listing]]:
        """Fetch every source for this chat, apply filter, collect matches per source.

        Same-source strict dedup uses the *global* seen table — a listing
        seen for chat A won't be re-parsed for chat B, but chat B still
        gets it emitted if chat_emissions doesn't have it yet.
        """
        matched: Dict[str, List[Listing]] = {}
        for src in ctx.sources:
            log.info("[%s] scanning %s", ctx.chat_id, src.name)
            matched[src.name] = []
            try:
                for listing in src.scan():
                    self.stats.seen += 1
                    if not self.store.has(listing.dedup_key):
                        # First time we see this listing anywhere.
                        ok, reason = ctx.filter.accepts(listing)
                        if not ok:
                            log.debug("[%s] reject %s: %s", ctx.chat_id, listing.url, reason)
                            self.stats.rejected += 1
                            if not self.dry_run:
                                self.store.add(listing, status="rejected", reject_reason=reason)
                            continue
                        if not self.dry_run:
                            self.store.add(listing, status="matched", fuzzy_key=listing.fuzzy_key)
                    else:
                        self.stats.already_seen += 1
                        # Still evaluate per-chat: filter override might let
                        # this chat see a listing globally rejected earlier.
                        ok, _ = ctx.filter.accepts(listing)
                        if not ok:
                            continue

                    if self.repo.has_emitted(ctx.chat_id, listing.dedup_key):
                        continue
                    matched[src.name].append(listing)
            except Exception:
                log.exception("[%s] source %s crashed", ctx.chat_id, src.name)
        return matched

    # ── phase 2 ────────────────────────────────────────────────────────

    def _cross_source_dedup(
        self, ctx: ChatContext, matched: Dict[str, List[Listing]]
    ) -> Dict[str, List[Listing]]:
        """Per-chat cross-source dedup: same apartment on multiple platforms → one msg."""
        emitted_fk = set() if self.dry_run else self.repo.emitted_fuzzy_keys(ctx.chat_id)
        out: Dict[str, List[Listing]] = {}
        for src_name, listings in matched.items():
            kept: List[Listing] = []
            for l in listings:
                fk = l.fuzzy_key
                if fk and fk in emitted_fk:
                    self.stats.cross_dup += 1
                    continue
                if fk:
                    emitted_fk.add(fk)
                kept.append(l)
            out[src_name] = kept
        return out

    # ── phase 2.5 ──────────────────────────────────────────────────────

    def _score(self, ctx: ChatContext, matched: Dict[str, List[Listing]]) -> None:
        """Attach a DealScore to every match, using median from persistence + current."""
        current_ppm2 = [
            l.price / l.area
            for listings in matched.values()
            for l in listings
            if l.price and l.area and l.area > 0
        ]
        context = ctx.scorer.make_context(
            list(self.store.matched_price_per_m2()) + current_ppm2
        )
        if context.median_price_per_m2:
            log.info(
                "[%s] scoring: median = %.0f zł/m²",
                ctx.chat_id, context.median_price_per_m2,
            )
        for listings in matched.values():
            for l in listings:
                l.score = ctx.scorer.score(l, context)

    # ── phase 3 ────────────────────────────────────────────────────────

    def _emit(self, ctx: ChatContext, matched: Dict[str, List[Listing]]) -> None:
        """Aggregate near-duplicates, emit to the chat, record emission."""
        for src_name, listings in matched.items():
            for item in group_listings(listings, min_group_size=ctx.min_group_size):
                if isinstance(item, ListingGroup):
                    self.stats.matched += item.size
                    print(format_group_plain(item))
                    print("-" * 60)
                    if not self.dry_run and ctx.notifier.send_group(item):
                        self.stats.sent += 1
                        for l in item.items:
                            self.repo.record_emission(ctx.chat_id, l.dedup_key)
                else:
                    self.stats.matched += 1
                    print(format_plain(item))
                    print("-" * 60)
                    if not self.dry_run and ctx.notifier.send(item):
                        self.stats.sent += 1
                        self.repo.record_emission(ctx.chat_id, item.dedup_key)


# ── context builder ────────────────────────────────────────────────────

def build_chat_context(
    row: ChatRow,
    baseline_cfg: dict,
    bot_token: str,
    source_registry: Dict[str, type],
) -> ChatContext:
    """Compose a :class:`ChatContext` from a chat row and the YAML baseline."""
    from .filters import ListingFilter
    from .scoring import DealScorer

    ec = EffectiveConfig(baseline=baseline_cfg, override=row.override)
    flt = ListingFilter(
        min_area=ec.min_area(),
        max_price=ec.max_price(),
        min_build_year=ec.min_build_year(),
        reject_keywords=ec.reject_keywords(),
    )

    # Bound the filter to a chat-only max_area if set (baseline has no upper bound).
    max_area = ec.override.max_area
    if max_area is not None:
        flt = _wrap_with_max_area(flt, max_area)

    scoring_cfg = baseline_cfg.get("scoring") or {}
    scorer: Optional[DealScorer] = None
    if scoring_cfg.get("enabled", True):
        weights = _build_weights(scoring_cfg, ec.weights())
        scorer = DealScorer(
            positive_kw=ec.positive_keywords(),
            negative_kw=ec.negative_keywords(),
            weights=weights,
        )

    http = baseline_cfg.get("http") or {}
    common = {
        "user_agent": http.get("user_agent", ""),
        "timeout": http.get("timeout", 30),
        "delay": http.get("delay_seconds", 2),
    }
    sources: List[BaseSource] = []
    for name, sconf in ec.enabled_source_configs().items():
        cls = source_registry.get(name)
        if not cls:
            log.warning("chat %s: unknown source '%s' — ignored", row.chat_id, name)
            continue
        params = {k: v for k, v in sconf.items() if k != "enabled"}
        params.update(common)
        sources.append(cls(**params))

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=row.chat_id,
        parse_mode=(baseline_cfg.get("telegram") or {}).get("parse_mode", "HTML"),
    )

    return ChatContext(
        chat_id=row.chat_id,
        title=row.title,
        filter=flt,
        scorer=scorer,
        sources=sources,
        min_group_size=ec.min_group_size(),
        notifier=notifier,
    )


def _build_weights(scoring_cfg: dict, merged_weights: dict) -> ScoringWeights:
    """YAML weights + chat override → :class:`ScoringWeights` instance."""
    w = merged_weights or {}
    return ScoringWeights(
        base=int(w.get("base", 50)),
        ppm2=int(w.get("price_per_m2", 25)),
        ppm2_full_at=float(w.get("price_per_m2_full_at", 0.20)),
        ppm2_reason_threshold=float(w.get("ppm2_reason_threshold", 0.03)),
        area_sweet_bonus=int(w.get("area_sweet_bonus", 5)),
        area_sweet_min=float(w.get("area_sweet_min", 40)),
        area_sweet_max=float(w.get("area_sweet_max", 60)),
        keyword=int(w.get("keyword", 3)),
        min_median_sample=int(w.get("min_median_sample", 10)),
    )


def _wrap_with_max_area(inner: ListingFilter, max_area: float) -> ListingFilter:
    """Chat-level max_area — baseline never had it, so wrap the filter."""
    original_accepts = inner.accepts

    def accepts(l):
        if l.area is not None and l.area > max_area:
            return False, f"area {l.area} > {max_area}"
        return original_accepts(l)

    inner.accepts = accepts  # type: ignore[method-assign]
    return inner
