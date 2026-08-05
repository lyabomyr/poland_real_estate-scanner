"""Grouping must change how listings are packaged, never whether they arrive.

This is the invariant the whole feature rests on. It was broken in practice:
a 77-listing group rendered to 10 853 characters, Telegram rejects anything
over 4096, so sendMessage returned 400, the group was never recorded as
delivered, and every later run rebuilt and re-failed the same message. 220 of
1004 matched listings were unreachable — permanently, and silently.
"""

from __future__ import annotations

import unittest

from scanner.aggregator import MAX_PER_MESSAGE, ListingGroup, group_listings
from scanner.format import format_group_html, format_html
from scanner.models import Listing

#: Telegram's hard limit for a sendMessage body.
TELEGRAM_LIMIT = 4096


def _listing(i: int, location: str, source: str = "morizon") -> Listing:
    return Listing(
        source=source,
        id=f"{source}-{i}",
        # Realistic length: morizon slugs are long and they sit in every line
        # of a group message, which is what pushed the real one over the limit.
        url=f"https://www.morizon.pl/oferta/sprzedaz-mieszkanie-krakow-biezanow-prokocim-agatowa-{i}-52m2-mzn20478{i:05d}",
        title=f"Mieszkanie {i} z balkonem i osobną kuchnią, {i}-piętro",
        price=500_000 + i,
        area=45.0,
        location=location,
    )


def _all_ids(items) -> list:
    out = []
    for item in items:
        if isinstance(item, ListingGroup):
            out.extend(l.id for l in item.items)
        else:
            out.append(item.id)
    return out


class NothingIsLostTests(unittest.TestCase):
    def test_every_listing_is_emitted_exactly_once(self) -> None:
        listings = (
            [_listing(i, "Agatowa, Os. Złocień, Kraków, małopolskie") for i in range(50)]
            + [_listing(i, "Kraków", source="otodom") for i in range(100, 105)]
            + [_listing(i, "Pękowicka, Prądnik Biały, Kraków, małopolskie") for i in range(200, 202)]
        )
        ids = _all_ids(group_listings(listings, min_group_size=3))

        self.assertEqual(len(listings), len(ids), "count changed")
        self.assertEqual({l.id for l in listings}, set(ids), "ids changed")
        self.assertEqual(len(ids), len(set(ids)), "a listing was emitted twice")

    def test_a_big_group_is_split_not_truncated(self) -> None:
        listings = [_listing(i, "Agatowa, Os. Złocień, Kraków, małopolskie") for i in range(77)]
        groups = [g for g in group_listings(listings, min_group_size=3)]

        self.assertTrue(all(isinstance(g, ListingGroup) for g in groups))
        self.assertEqual(77, sum(g.size for g in groups))
        self.assertEqual(4, len(groups))                 # ceil(77 / 20)
        self.assertEqual([1, 2, 3, 4], [g.part for g in groups])
        self.assertTrue(all(g.parts == 4 for g in groups))
        self.assertIn("(3/4)", groups[2].label)

    def test_no_rendered_message_can_exceed_the_telegram_limit(self) -> None:
        """The failure this guards is silent: an over-long message is dropped."""
        listings = [_listing(i, "Agatowa, Os. Złocień, Kraków, małopolskie") for i in range(500)]
        for item in group_listings(listings, min_group_size=3):
            body = (
                format_group_html(item) if isinstance(item, ListingGroup)
                else format_html(item)
            )
            self.assertLess(
                len(body), TELEGRAM_LIMIT,
                f"message of {len(body)} chars would be rejected by Telegram",
            )

    def test_a_group_below_the_threshold_stays_individual(self) -> None:
        listings = [_listing(i, "Agatowa, Os. Złocień, Kraków, małopolskie") for i in range(2)]
        items = list(group_listings(listings, min_group_size=3))
        self.assertTrue(all(isinstance(i, Listing) for i in items))

    def test_sources_are_never_mixed_in_one_group(self) -> None:
        """Same street, two portals — still two messages, because the URLs and
        ids differ and a roll-up implies "one seller, one building"."""
        loc = "Agatowa, Os. Złocień, Kraków, małopolskie"
        listings = (
            [_listing(i, loc, source="morizon") for i in range(3)]
            + [_listing(i, loc, source="olx") for i in range(3)]
        )
        groups = [g for g in group_listings(listings, min_group_size=3)
                  if isinstance(g, ListingGroup)]
        self.assertEqual(2, len(groups))
        for g in groups:
            self.assertEqual(1, len({l.source for l in g.items}))

    def test_max_per_message_default_keeps_headroom(self) -> None:
        """Sanity-check the constant itself against a worst-case line length."""
        self.assertLessEqual(MAX_PER_MESSAGE * 180, TELEGRAM_LIMIT * 2)


if __name__ == "__main__":
    unittest.main()
