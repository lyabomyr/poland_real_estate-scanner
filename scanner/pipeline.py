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

from collections import Counter
from dataclasses import asdict, dataclass
from logging import getLogger
from typing import Dict, List, Optional

from .aggregator import ListingGroup, group_listings
from .chat_config import EffectiveConfig
from .chat_repo import ChatConfigRepo, ChatRow
from .filters import ListingFilter
from .format import format_group_plain, format_plain
from .models import Listing
from .scoring import DealScorer
from .sources.base import BaseSource
from .telegram import TelegramNotifier

log = getLogger(__name__)


def _reject_kind(reason: str) -> str:
    """Collapse a per-listing reason into a countable category.

    ``"price 843393 > 610000"`` and ``"price 727500 > 610000"`` are the same
    story told twice; ``"keyword '(?<!\\w)udział'"`` is a different one.
    """
    if reason.startswith("keyword "):
        parts = reason.split("'")
        return f"keyword {parts[1]}" if len(parts) > 1 else "keyword"
    return reason.split()[0] if reason else "unknown"


def _fmt_rejects(counter: "Counter") -> str:
    """``price ×120, keyword '(?<!\\w)udział' ×8`` — most common first."""
    return ", ".join(f"{kind} ×{n}" for kind, n in counter.most_common())


@dataclass
class RunStats:
    """Global counters across every chat in one pipeline run."""
    seen: int = 0
    already_seen: int = 0
    rejected: int = 0
    matched: int = 0        # aggregate across all chats
    cross_dup: int = 0
    price_changed: int = 0  # re-notified because the price moved
    sent: int = 0
    #: Messages we had ready but Telegram refused (or that were skipped
    #: because no bot token is configured). Counted separately from `sent`
    #: so a run that finds matches but delivers none can't read as success.
    send_failed: int = 0
    #: Previously-rejected listings that pass now because a filter was
    #: relaxed. Visible so "I deleted a reject keyword" has an observable
    #: effect in the logs rather than being taken on faith.
    promoted: int = 0

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
            swept = []
            matched = self._scan_and_filter(ctx, swept)
            matched = self._cross_source_dedup(ctx, matched)
            if ctx.scorer:
                self._score(ctx, matched)
            # The scan's job ends here: everything it found is now stored and
            # scored, so a URL is fully swept regardless of what gets sent
            # below. Delivery is driven off the database from this point on,
            # which is what makes an interrupted run cheap to resume.
            if not self.dry_run:
                for url in swept:
                    self.store.record_swept(url, ctx.filter.fingerprint())
                matched = self._delivery_backlog(ctx, matched)
            before_sent = self.stats.sent
            before_failed = self.stats.send_failed
            self._emit(ctx, matched)
            sent = self.stats.sent - before_sent
            failed = self.stats.send_failed - before_failed
            if self.dry_run:
                log.info("[%s] dry run — nothing sent", ctx.chat_id)
            else:
                log.info("[%s] delivered %d message(s)", ctx.chat_id, sent)
            if failed:
                log.error(
                    "[%s] %d message(s) could NOT be delivered — they stay "
                    "unrecorded and will be retried next run",
                    ctx.chat_id, failed,
                )
        return self.stats

    def _delivery_backlog(
        self, ctx: ChatContext, matched: Dict[str, List[Listing]]
    ) -> Dict[str, List[Listing]]:
        """Replace this run's matches with everything this chat is still owed.

        Discovery and delivery run at very different speeds. A first sweep
        finds ~1000 listings in six minutes; Telegram takes about twenty
        messages a minute into a group, so sending them takes over half an
        hour and the scheduled run is killed long before it finishes. Driving
        delivery from "what this scan saw" then stranded everything the killed
        run never reached — 647 listings sat matched-but-unsent, and because
        later runs only walk the first two pages, they were never seen again.

        Reading the backlog from the database fixes that by construction:
        whatever is left is picked up by the next run, in score order.

        Price changes are the one thing not in here — those listings *have*
        been emitted, so they are carried over from the scan.
        """
        price_changes = [
            l for listings in matched.values() for l in listings
            if l.previous_price is not None
        ]
        backlog = self.repo.undelivered(ctx.chat_id)
        if backlog:
            log.info(
                "[%s] delivery backlog: %d listing(s) matched but never sent",
                ctx.chat_id, len(backlog),
            )
        out: Dict[str, List[Listing]] = {}
        for listing in price_changes + backlog:
            out.setdefault(listing.source, []).append(listing)
        return out

    # ── phase 1 ────────────────────────────────────────────────────────

    def _scan_and_filter(
        self, ctx: ChatContext, swept: List[str]
    ) -> Dict[str, List[Listing]]:
        """Fetch every source for this chat, apply filter, collect matches per source.

        Same-source strict dedup uses the *global* seen table — a listing
        seen for chat A won't be re-parsed for chat B, but chat B still
        gets it emitted if chat_emissions doesn't have it yet.
        """
        matched: Dict[str, List[Listing]] = {}
        # Why listings were dropped, aggregated for one INFO line per source.
        # Individual rejects stay at DEBUG (thousands of them on a first
        # sweep), but a run whose logs don't say *why* the market shrank is
        # impossible to trust — this is the line that shows a filter has
        # started eating everything.
        rejects: Counter = Counter()
        fingerprint = ctx.filter.fingerprint()
        for src in ctx.sources:
            first_sweep = bool(src.url) and not self.store.is_swept(src.url, fingerprint)
            if first_sweep:
                # Never seen this URL before — take the whole back-catalogue
                # instead of just the newest page or two.
                src.pages = 0
                log.info(
                    "[%s] %s: no completed sweep for this URL under the "
                    "current filters — scanning every page",
                    ctx.chat_id, src.name,
                )
            # Log the URL, not just the source name: it is the one thing you
            # need to reproduce a scan in a browser, and it encodes the city
            # and thresholds actually in effect for this chat.
            log.info("[%s] scanning %s: %s", ctx.chat_id, src.name, src.url or "(no URL)")
            matched[src.name] = []
            before = (self.stats.seen, self.stats.rejected, self.stats.already_seen)
            before_promoted = self.stats.promoted
            src_rejects: Counter = Counter()
            try:
                for listing in src.scan():
                    self.stats.seen += 1
                    if not self.store.has(listing.dedup_key):
                        # First time we see this listing anywhere.
                        ok, reason = ctx.filter.accepts(listing)
                        if not ok:
                            log.debug("[%s] reject %s: %s", ctx.chat_id, listing.url, reason)
                            src_rejects[_reject_kind(reason)] += 1
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
                        # It passes now but was stored as rejected — someone
                        # relaxed a filter. Promote it, or it stays invisible
                        # to the dashboard and to the delivery backlog.
                        if not self.dry_run and self.store.promote_rejected(
                            listing.dedup_key, listing.fuzzy_key
                        ):
                            self.stats.promoted += 1
                        # A re-seen listing whose price moved is news again —
                        # portals edit prices in place, keeping the same id, so
                        # without this a drop would be silently swallowed by the
                        # has_emitted() check below.
                        self._note_price_change(listing)

                    emitted_price = self.repo.emitted_price(ctx.chat_id, listing.dedup_key)
                    already = self.repo.has_emitted(ctx.chat_id, listing.dedup_key)
                    if already:
                        # Re-notify only on a real price move we can prove: we
                        # need both the old and new numbers.
                        if not (listing.price and emitted_price and listing.price != emitted_price):
                            continue
                        listing.previous_price = emitted_price
                        self.stats.price_changed += 1
                        log.info(
                            "[%s] price change %s: %s -> %s",
                            ctx.chat_id, listing.url, emitted_price, listing.price,
                        )
                    matched[src.name].append(listing)
            except Exception:
                log.exception("[%s] source %s crashed", ctx.chat_id, src.name)
                # A crash mid-sweep means we did NOT see the whole
                # back-catalogue, so leave the URL unmarked and sweep again
                # next run rather than writing off the pages we never reached.
                continue
            seen = self.stats.seen - before[0]
            rejected = self.stats.rejected - before[1]
            known = self.stats.already_seen - before[2]
            log.info(
                "[%s] %s: %d seen (%d new, %d already known), %d rejected, "
                "%d to notify%s",
                ctx.chat_id, src.name, seen, seen - known, known, rejected,
                len(matched[src.name]),
                "  rejects: " + _fmt_rejects(src_rejects) if src_rejects else "",
            )
            if seen == 0:
                # Not fatal — a portal can genuinely have nothing — but it is
                # also what a silently broken parser or a bot-block looks
                # like, so it should never pass unremarked.
                log.warning(
                    "[%s] %s returned no listings at all — check the URL or the parser",
                    ctx.chat_id, src.name,
                )
            promoted = self.stats.promoted - before_promoted
            if promoted:
                log.info(
                    "[%s] %s: %d previously-rejected listing(s) now pass — a "
                    "filter was relaxed", ctx.chat_id, src.name, promoted,
                )
            rejects.update(src_rejects)

            if first_sweep:
                if src.scan_completed:
                    swept.append(src.url)
                else:
                    log.warning(
                        "[%s] %s: first sweep was cut short — will retry next run",
                        ctx.chat_id, src.name,
                    )
        if rejects:
            log.info("[%s] rejects this run: %s", ctx.chat_id, _fmt_rejects(rejects))
        return matched

    def _note_price_change(self, listing: Listing) -> None:
        """Persist a price move on an already-stored listing.

        ``SeenStore.add()`` is INSERT OR IGNORE, so a re-seen row never
        updates itself. This is the one place that writes a new price and
        appends to ``price_history``, giving the dashboard a price timeline.
        """
        if listing.price is None or self.dry_run:
            return
        stored = self.store.stored_price(listing.dedup_key)
        if stored is not None and stored != listing.price:
            self.store.record_price_change(
                listing.dedup_key, stored, listing.price, fuzzy_key=listing.fuzzy_key
            )

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
                # Persist it so the dashboard can display and sort by score.
                # Scoring runs after the row is inserted (the median needs the
                # full run sample), hence a follow-up UPDATE.
                if not self.dry_run:
                    self.store.update_score(l.dedup_key, l.score)

    # ── phase 3 ────────────────────────────────────────────────────────

    def _emit(self, ctx: ChatContext, matched: Dict[str, List[Listing]]) -> None:
        """Aggregate near-duplicates, emit to the chat, record emission."""
        for src_name, listings in matched.items():
            for item in group_listings(listings, min_group_size=ctx.min_group_size):
                if isinstance(item, ListingGroup):
                    self.stats.matched += item.size
                    print(format_group_plain(item))
                    print("-" * 60)
                    if self.dry_run:
                        continue
                    if ctx.notifier.send_group(item):
                        self.stats.sent += 1
                        for l in item.items:
                            self.repo.record_emission(ctx.chat_id, l.dedup_key, l.price)
                    else:
                        # Not recorded as emitted, so the next run retries it.
                        self.stats.send_failed += 1
                else:
                    self.stats.matched += 1
                    print(format_plain(item))
                    print("-" * 60)
                    if self.dry_run:
                        continue
                    if ctx.notifier.send(item):
                        self.stats.sent += 1
                        self.repo.record_emission(ctx.chat_id, item.dedup_key, item.price)
                    else:
                        self.stats.send_failed += 1


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
    flt = ListingFilter.from_config(ec)
    scorer = DealScorer.from_config(ec, baseline_cfg)

    http = baseline_cfg.get("http") or {}
    common = {
        "user_agent": http.get("user_agent", ""),
        "timeout": http.get("timeout", 30),
        "delay": http.get("delay_seconds", 2),
    }
    sources: List[BaseSource] = []
    for name, sconf in ec.enabled_source_configs(source_registry).items():
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
        parse_mode=ec.parse_mode(),
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


