"""Delivery must survive being interrupted, and must react to filter edits.

Two failures this pins down, both observed in the live database:

1. 647 listings sat matched-but-unsent. Discovery finds ~1000 in six minutes;
   Telegram accepts ~20 messages a minute into a group. The scheduled run was
   killed mid-delivery, and because later runs only walk the first two pages,
   everything the killed run never reached was never seen again.

2. Deleting a phrase from ``reject_keywords`` let a listing pass the filter
   again, but its stored row still said ``rejected`` — invisible to both the
   dashboard and the backlog. Accepted, then silently dropped.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scanner.chat_repo import ChatConfigRepo
from scanner.models import DealScore, Listing
from scanner.storage import SeenStore


def _listing(i: int, price: int = 500_000) -> Listing:
    return Listing(
        source="morizon", id=str(i), url=f"https://example.test/{i}",
        title=f"flat {i}", price=price, area=45.0,
        location="Agatowa, Os. Złocień, Kraków, małopolskie",
    )


class BacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SeenStore(local_path=str(Path(self.tmp.name) / "t.db"))
        self.addCleanup(self.store.close)
        self.repo = ChatConfigRepo(self.store)

    def test_matched_but_unsent_listings_are_the_backlog(self) -> None:
        for i in range(3):
            self.store.add(_listing(i), status="matched")
        self.assertEqual(3, len(self.repo.undelivered("-1")))

    def test_delivered_listings_leave_the_backlog(self) -> None:
        for i in range(3):
            self.store.add(_listing(i), status="matched")
        self.repo.record_emission("-1", "morizon:1", 500_000)

        remaining = {l.id for l in self.repo.undelivered("-1")}
        self.assertEqual({"0", "2"}, remaining)

    def test_the_backlog_is_per_chat(self) -> None:
        self.store.add(_listing(0), status="matched")
        self.repo.record_emission("-1", "morizon:0", 500_000)

        self.assertEqual(0, len(self.repo.undelivered("-1")))
        self.assertEqual(1, len(self.repo.undelivered("-2")), "other chat is still owed it")

    def test_rejected_listings_are_never_in_the_backlog(self) -> None:
        self.store.add(_listing(0), status="rejected", reject_reason="keyword 'TBS'")
        self.assertEqual(0, len(self.repo.undelivered("-1")))

    def test_backlog_is_ordered_best_score_then_cheapest(self) -> None:
        """A killed run should deliver the good ones first, not the newest."""
        for i, (price, score) in enumerate([(600_000, 40), (500_000, 90), (400_000, 90)]):
            self.store.add(_listing(i, price), status="matched")
            self.store.update_score(f"morizon:{i}", DealScore(value=score, reasons=["x"]))
        self.assertEqual(["2", "1", "0"], [l.id for l in self.repo.undelivered("-1")])

    def test_backlog_carries_the_stored_score_into_the_message(self) -> None:
        self.store.add(_listing(0), status="matched")
        self.store.update_score(
            "morizon:0", DealScore(value=74, reasons=["-15% vs median", "+balkon"])
        )
        restored = self.repo.undelivered("-1")[0]
        self.assertEqual(74, restored.score.value)
        self.assertEqual(["-15% vs median", "+balkon"], restored.score.reasons)

    def test_a_limit_caps_how_much_one_run_takes_on(self) -> None:
        for i in range(10):
            self.store.add(_listing(i), status="matched")
        self.assertEqual(4, len(self.repo.undelivered("-1", limit=4)))


class PromoteRejectedTests(unittest.TestCase):
    """Relaxing a filter has to reach rows that were already stored."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SeenStore(local_path=str(Path(self.tmp.name) / "t.db"))
        self.addCleanup(self.store.close)
        self.repo = ChatConfigRepo(self.store)

    def test_promoting_makes_it_visible_and_deliverable(self) -> None:
        self.store.add(_listing(0), status="rejected", reject_reason="keyword 'z lat 60'")
        self.assertEqual(0, len(self.repo.undelivered("-1")))

        self.assertTrue(self.store.promote_rejected("morizon:0", "500000|45|agatowa"))

        self.assertEqual(1, len(self.repo.undelivered("-1")))
        row = self.store.conn.execute(
            "SELECT status, reject_reason, fuzzy_key FROM seen WHERE key = 'morizon:0'"
        ).fetchall()[0]
        self.assertEqual("matched", row[0])
        self.assertIsNone(row[1], "the stale reason must be cleared")
        self.assertEqual("500000|45|agatowa", row[2])

    def test_promoting_an_already_matched_row_is_a_no_op(self) -> None:
        self.store.add(_listing(0), status="matched")
        self.assertFalse(self.store.promote_rejected("morizon:0"))

    def test_promoting_an_unknown_key_is_a_no_op(self) -> None:
        self.assertFalse(self.store.promote_rejected("morizon:nope"))


if __name__ == "__main__":
    unittest.main()
