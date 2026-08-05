"""Two bugs that put junk at the front of the queue.

1. Cross-source dedup ran on the scan results, which were then replaced by
   the delivery backlog — so the work was thrown away and 111 duplicate
   messages were queued: 52 of the same flat listed twice, plus 59 that were
   identical to something already sent from another portal.

2. Portals publish "ask for price" listings as 1 zł. The scorer rewards being
   below the median, so a 275 m² apartment at 1 zł scored ★75 and sat at the
   very top of the backlog, which is ordered by score.
"""

from __future__ import annotations

import unittest

import yaml
from pathlib import Path

from scanner.chat_config import ChatOverride, EffectiveConfig
from scanner.filters import ListingFilter
from scanner.models import Listing
from scanner.pipeline import ChatContext, MultiChatPipeline

_CONFIG = Path(__file__).resolve().parent.parent / "config.yml"


def _listing(source: str, i: int, price=500_000, area=45.0, location="Agatowa, Złocień, Kraków, małopolskie"):
    return Listing(source=source, id=str(i), url=f"https://{source}.test/{i}",
                   title=f"flat {i}", price=price, area=area, location=location)


class _Store:
    def is_swept(self, url, filters=""): return True
    def record_swept(self, url, filters=""): pass
    def has(self, key): return False
    def add(self, *a, **kw): pass
    def stored_price(self, key): return None
    def promote_rejected(self, key, fuzzy_key=None): return False


class _Repo:
    def __init__(self, backlog=(), emitted_fuzzy=()):
        self._backlog = list(backlog)
        self._emitted_fuzzy = set(emitted_fuzzy)
        self.recorded = []

    def emitted_price(self, chat_id, key): return None
    def has_emitted(self, chat_id, key): return False
    def undelivered(self, chat_id, limit=2000): return list(self._backlog)
    def emitted_fuzzy_keys(self, chat_id): return set(self._emitted_fuzzy)
    def record_emission(self, chat_id, key, price=None): self.recorded.append(key)


class _Notifier:
    def __init__(self): self.sent = []
    def send(self, l): self.sent.append(l.dedup_key); return True
    def send_group(self, g):
        self.sent.extend(l.dedup_key for l in g.items); return True


class BacklogIsDedupedTests(unittest.TestCase):
    """The backlog is raw database rows — it has had no dedup applied yet."""

    def _run(self, backlog, emitted_fuzzy=()):
        notifier = _Notifier()
        repo = _Repo(backlog=backlog, emitted_fuzzy=emitted_fuzzy)
        ctx = ChatContext(
            chat_id="-1", title="t",
            filter=ListingFilter(min_area=0, max_price=10**9),
            scorer=None, sources=[], min_group_size=99, notifier=notifier,
        )
        pipe = MultiChatPipeline([ctx], store=_Store(), repo=repo, dry_run=False)
        # sources=[] short-circuits run(), so drive the delivery path directly.
        matched = pipe._delivery_backlog(ctx, {})
        matched = pipe._cross_source_dedup(ctx, matched)
        pipe._emit(ctx, matched)
        return notifier.sent, pipe.stats

    def test_the_same_flat_on_two_portals_is_sent_once(self) -> None:
        twins = [_listing("otodom", 1), _listing("morizon", 1)]
        self.assertEqual(twins[0].fuzzy_key, twins[1].fuzzy_key, "test setup")

        sent, stats = self._run(twins)
        self.assertEqual(1, len(sent), "both twins were delivered")
        self.assertEqual(1, stats.cross_dup)

    def test_a_flat_already_sent_from_another_portal_is_not_resent(self) -> None:
        twin = _listing("morizon", 1)
        sent, stats = self._run([twin], emitted_fuzzy={twin.fuzzy_key})
        self.assertEqual([], sent)
        self.assertEqual(1, stats.cross_dup)

    def test_distinct_flats_are_all_delivered(self) -> None:
        """Dedup must not become over-eager — that would be the worse bug."""
        distinct = [
            _listing("morizon", 1, price=500_000),
            _listing("morizon", 2, price=520_000),
            _listing("morizon", 3, price=540_000),
        ]
        sent, stats = self._run(distinct)
        self.assertEqual(3, len(sent))
        self.assertEqual(0, stats.cross_dup)

    def test_a_listing_with_no_fuzzy_key_is_never_collapsed(self) -> None:
        """No price or area means we cannot prove identity — so don't guess."""
        vague = [
            Listing(source="komornik", id="1", url="u", title="lokal"),
            Listing(source="komornik", id="2", url="u", title="lokal"),
        ]
        self.assertIsNone(vague[0].fuzzy_key, "test setup")
        sent, _ = self._run(vague)
        self.assertEqual(2, len(sent))


class PlaceholderPriceTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = yaml.safe_load(_CONFIG.read_text())
        self.filter = ListingFilter.from_config(
            EffectiveConfig(baseline=cfg, override=ChatOverride())
        )

    def test_a_one_zloty_listing_is_rejected(self) -> None:
        junk = _listing("otodom", 1, price=1, area=275.0)
        ok, reason = self.filter.accepts(junk)
        self.assertFalse(ok)
        self.assertIn("not a real price", reason)

    def test_a_real_price_still_passes(self) -> None:
        self.assertTrue(self.filter.accepts(_listing("otodom", 2, price=550_000))[0])

    def test_no_price_at_all_still_passes(self) -> None:
        """Unknown is not the same as absurd — komornik rarely publishes one."""
        blank = Listing(source="komornik", id="1", url="u", title="lokal mieszkalny")
        self.assertTrue(self.filter.accepts(blank)[0])

    def test_the_floor_is_part_of_the_fingerprint(self) -> None:
        """Otherwise changing it would not retire completed sweeps."""
        loose = ListingFilter(min_area=39, max_price=610_000, min_price=0)
        strict = ListingFilter(min_area=39, max_price=610_000, min_price=50_000)
        self.assertNotEqual(loose.fingerprint(), strict.fingerprint())

    def test_the_floor_is_described_in_the_decision_tree(self) -> None:
        rules = " ".join(self.filter.describe())
        self.assertIn("50000", rules)


if __name__ == "__main__":
    unittest.main()
