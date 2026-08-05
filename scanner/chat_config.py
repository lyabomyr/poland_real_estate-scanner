"""Per-chat overrides on top of the YAML baseline config.

Multi-tenancy model
-------------------
The YAML file (``config.yml``) is the *baseline* — every chat starts with
those settings. Each chat then has an optional row in the ``chat_configs``
table with an :class:`ChatOverride` JSON blob describing the fields it
wants to override (max_price, disabled sources, extra keywords, per-source
URLs, etc.). Users tune this via Telegram commands (or the Streamlit UI);
the scanner reads the row at the start of every scan and produces one
effective config per chat.

Merging is intentionally shallow / explicit — no deep merge magic; each
override field replaces its baseline counterpart wholesale so behaviour is
predictable when you inspect the JSON. Keyword lists are the exception —
``extra_reject`` / ``extra_positive`` / ``extra_negative`` are *added* to
the baseline lists rather than replacing them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChatOverride:
    """Fields any chat may override on top of the YAML baseline.

    Any attribute left as ``None`` / empty means "inherit from baseline".
    The whole object is persisted as one JSON blob in ``chat_configs.config``.
    """

    # search.*
    city: Optional[str] = None      # e.g. "katowice" — see scanner.cities
    max_price: Optional[int] = None
    min_area: Optional[float] = None
    max_area: Optional[float] = None
    min_build_year: Optional[int] = None

    # sources.*
    disabled_sources: List[str] = field(default_factory=list)
    # {source_name: url} — replaces baseline URL for the named source
    source_urls: Dict[str, str] = field(default_factory=dict)

    # filters.reject_keywords → additions, not replacements
    extra_reject: List[str] = field(default_factory=list)

    # scoring.positive_keywords / negative_keywords → additions
    # Entries are either "name" strings (use default weight) or
    # {"name": "…", "weight": N} dicts.
    extra_positive: List[Any] = field(default_factory=list)
    extra_negative: List[Any] = field(default_factory=list)

    # scoring.weights.* — partial override
    weights: Dict[str, Any] = field(default_factory=dict)

    # notifications.min_group_size
    min_group_size: Optional[int] = None

    # if True, scanner skips sending to this chat entirely
    paused: bool = False

    def to_json(self) -> str:
        # ``sort_keys`` keeps DB rows diffable; ``ensure_ascii=False`` so
        # Polish chars survive round-trip.
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, blob: Optional[str]) -> "ChatOverride":
        if not blob:
            return cls()
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        # Ignore unknown keys — future-proof for schema evolution
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EffectiveConfig:
    """Baseline YAML config with one chat's override merged in.

    Passed to the pipeline instead of the raw YAML dict — every consumer
    (filter builder, scorer builder, source builder) reads from here.
    """

    baseline: dict
    override: ChatOverride

    # ── merged getters — everything else can just read plain dicts ─────

    def city_key(self) -> str:
        """Effective city slug. Drives dynamically built source URLs."""
        from .cities import get_city
        raw = self.override.city or (self.baseline.get("search") or {}).get("city")
        return get_city(raw).key

    def max_price(self) -> int:
        return self.override.max_price or self.baseline["search"]["max_price"]

    def min_area(self) -> float:
        return self.override.min_area or self.baseline["search"]["min_area"]

    def max_area(self) -> Optional[float]:
        return self.override.max_area  # baseline doesn't have this — chat-only

    def min_build_year(self) -> Optional[int]:
        v = self.override.min_build_year
        return v if v is not None else (self.baseline.get("search") or {}).get("min_build_year")

    def reject_keywords(self) -> List[str]:
        baseline_kw = (self.baseline.get("filters") or {}).get("reject_keywords", [])
        return list(baseline_kw) + list(self.override.extra_reject)

    def positive_keywords(self) -> List[Any]:
        baseline_kw = (self.baseline.get("scoring") or {}).get("positive_keywords", [])
        return list(baseline_kw) + list(self.override.extra_positive)

    def negative_keywords(self) -> List[Any]:
        baseline_kw = (self.baseline.get("scoring") or {}).get("negative_keywords", [])
        return list(baseline_kw) + list(self.override.extra_negative)

    def weights(self) -> Dict[str, Any]:
        baseline_w = ((self.baseline.get("scoring") or {}).get("weights") or {}).copy()
        baseline_w.update(self.override.weights)
        return baseline_w

    def min_group_size(self) -> int:
        return int(
            self.override.min_group_size
            or (self.baseline.get("notifications") or {}).get("min_group_size", 3)
        )

    def dashboard_url(self) -> Optional[str]:
        url = ((self.baseline.get("notifications") or {}).get("dashboard_url") or "").strip()
        return url or None

    def parse_mode(self) -> str:
        return ((self.baseline.get("telegram") or {}).get("parse_mode") or "HTML").strip() or "HTML"

    def enabled_source_configs(self, source_registry: Optional[dict] = None) -> Dict[str, dict]:
        """Return ``{source_name: source_config_dict}`` ready to instantiate.

        URL precedence, most specific first:

        1. the chat's ``source_urls[name]`` override — full manual control
        2. an explicit ``url`` in the YAML source block — pins one URL for all
        3. otherwise built from the effective city + price/area thresholds via
           ``SourceClass.build_url`` (requires ``source_registry``)

        Building by default is what makes ``city: katowice`` actually change
        what gets scanned; without it the hardcoded Kraków URLs would win and
        the setting would silently do nothing.
        """
        from .cities import get_city

        disabled = set(self.override.disabled_sources)
        city = get_city(self.city_key())
        out: Dict[str, dict] = {}
        for name, sconf in (self.baseline.get("sources") or {}).items():
            if not sconf or not sconf.get("enabled", True):
                continue
            if name in disabled:
                continue
            merged = dict(sconf)
            url = (
                self.override.source_urls.get(name)
                or sconf.get("url")
            )
            if not url and source_registry:
                cls = source_registry.get(name)
                if cls is not None:
                    url = cls.build_url(
                        city,
                        max_price=self.max_price(),
                        min_area=self.min_area(),
                    )
            if url:
                merged["url"] = url
            out[name] = merged
        return out

    def enabled_source_names(self) -> List[str]:
        return list(self.enabled_source_configs().keys())
