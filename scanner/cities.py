"""Polish city registry used to build source URLs.

Each portal encodes location differently — Otodom needs a voivodeship slug in
the path, komornik.pl wants the Polish voivodeship *name* as a query param,
OLX and Morizon just take a city slug. Keeping those spellings in one place
means adding a city is a single entry here rather than four URL edits.

Verified live for Kraków and Katowice: all four sources return listings for
both. Any city added here should be spot-checked the same way — a wrong slug
yields an empty page rather than an error, which is easy to miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class City:
    key: str            # ascii slug — used by Otodom / OLX / Morizon paths
    label: str          # display + komornik's `city` query param ("Kraków")
    region_slug: str    # ascii voivodeship slug for Otodom's path ("malopolskie")
    region_name: str    # Polish voivodeship name for komornik ("małopolskie")


# Ordered: the ones most likely to be used first.
CITIES: List[City] = [
    City("krakow",    "Kraków",    "malopolskie",         "małopolskie"),
    City("katowice",  "Katowice",  "slaskie",             "śląskie"),
    City("warszawa",  "Warszawa",  "mazowieckie",         "mazowieckie"),
    City("wroclaw",   "Wrocław",   "dolnoslaskie",        "dolnośląskie"),
    City("poznan",    "Poznań",    "wielkopolskie",       "wielkopolskie"),
    City("gdansk",    "Gdańsk",    "pomorskie",           "pomorskie"),
    City("gdynia",    "Gdynia",    "pomorskie",           "pomorskie"),
    City("lodz",      "Łódź",      "lodzkie",             "łódzkie"),
    City("szczecin",  "Szczecin",  "zachodniopomorskie",  "zachodniopomorskie"),
    City("lublin",    "Lublin",    "lubelskie",           "lubelskie"),
    City("bydgoszcz", "Bydgoszcz", "kujawsko-pomorskie",  "kujawsko-pomorskie"),
    City("rzeszow",   "Rzeszów",   "podkarpackie",        "podkarpackie"),
]

_BY_KEY: Dict[str, City] = {c.key: c for c in CITIES}
# Accept the pretty spelling too, so `city: "Kraków"` works in YAML.
_BY_LABEL: Dict[str, City] = {c.label.lower(): c for c in CITIES}

DEFAULT_CITY_KEY = "krakow"


def get_city(name: Optional[str]) -> City:
    """Look up a city by slug or display name, case-insensitively.

    Falls back to the default city rather than raising: a typo in a chat's
    override should degrade to "scans Kraków" instead of killing the run.
    """
    if not name:
        return _BY_KEY[DEFAULT_CITY_KEY]
    needle = str(name).strip().lower()
    return _BY_KEY.get(needle) or _BY_LABEL.get(needle) or _BY_KEY[DEFAULT_CITY_KEY]


def city_keys() -> List[str]:
    return [c.key for c in CITIES]


def city_label(key: str) -> str:
    return get_city(key).label
