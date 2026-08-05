"""OLX puts the posting time inside the location field. It must not stay there.

Left in, the timestamp is shown to the user as part of the address and lands
in the cross-source dedup key, which truncates to 20 characters:

    577000|43|bieżanów-prokocim -

The date eats the district, so the same flat stops matching its Otodom twin
the day either ad is refreshed — and the user gets it twice.
"""

from __future__ import annotations

import unittest

from scanner.models import Listing
from scanner.sources.olx import _clean_location


class CleanLocationTests(unittest.TestCase):
    def test_strips_every_timestamp_shape_olx_uses(self) -> None:
        cases = {
            "Kraków, Mistrzejowice - Odświeżono dnia 03 sierpnia 2026": "Kraków, Mistrzejowice",
            "Kraków, Bieżanów-Prokocim - 20 lipca 2026": "Kraków, Bieżanów-Prokocim",
            "Kraków, Podgórze Duchackie - Dzisiaj o 09:12": "Kraków, Podgórze Duchackie",
            "Kraków, Stare Miasto - Wczoraj o 21:40": "Kraków, Stare Miasto",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, _clean_location(raw), raw)

    def test_leaves_a_plain_location_alone(self) -> None:
        self.assertEqual("Kraków, Grzegórzki", _clean_location("Kraków, Grzegórzki"))

    def test_keeps_a_hyphen_that_belongs_to_the_place_name(self) -> None:
        """Only a " - " followed by a date is a timestamp."""
        self.assertEqual(
            "Kraków, Bronowice - Wielka",
            _clean_location("Kraków, Bronowice - Wielka - 01 sierpnia 2026"),
        )

    def test_missing_location_stays_missing(self) -> None:
        self.assertIsNone(_clean_location(None))
        self.assertIsNone(_clean_location("   "))

    def test_a_clean_location_produces_a_usable_dedup_key(self) -> None:
        """The point of all this: the key must carry the district, not a date."""
        listing = Listing(
            source="olx", id="1", url="u", title="t",
            price=577_000, area=43.08,
            location=_clean_location("Kraków, Bieżanów-Prokocim - 20 lipca 2026"),
        )
        self.assertEqual("577000|43|bieżanów-prokocim", listing.fuzzy_key)


if __name__ == "__main__":
    unittest.main()
