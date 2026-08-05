"""Human-readable runtime introspection for Telegram bot commands."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from .chat_config import ChatOverride, EffectiveConfig
from .cities import city_label
from .registry import SOURCE_REGISTRY
from .filters import ListingFilter
from .scoring import DealScorer

REDACTED = "***REDACTED***"
_SENSITIVE_KEY_BITS = (
    "token",
    "secret",
    "password",
    "credential",
    "auth",
    "api_key",
    "apikey",
)
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "client_secret",
    "key",
    "password",
    "secret",
    "signature",
    "sig",
    "token",
}
_DEDUP_DESCRIPTION = {
    "same_source": "<source>:<listing_id>",
    "cross_source": "<price>|<round(area)>|<first non-city location token>",
    "cross_source_requires": ["price", "area", "location"],
    "aggregation_group_key": "source + first two comma-separated location parts",
}


def dashboard_url_from_cfg(cfg: dict) -> Optional[str]:
    url = ((cfg.get("notifications") or {}).get("dashboard_url") or "").strip()
    return url or None


def build_effective_snapshot(baseline_cfg: dict, override: ChatOverride) -> dict:
    """Return the effective runtime config for one chat, already sanitized."""
    ec = EffectiveConfig(baseline=baseline_cfg, override=override)
    scoring_cfg = deepcopy(baseline_cfg.get("scoring") or {})
    scoring_cfg["positive_keywords"] = ec.positive_keywords()
    scoring_cfg["negative_keywords"] = ec.negative_keywords()
    scoring_cfg["weights"] = ec.weights()

    notifications = deepcopy(baseline_cfg.get("notifications") or {})
    notifications["min_group_size"] = ec.min_group_size()
    notifications["dashboard_url"] = ec.dashboard_url()

    search = deepcopy(baseline_cfg.get("search") or {})
    search["max_price"] = ec.max_price()
    search["min_price"] = ec.min_price()
    search["min_area"] = ec.min_area()
    search["max_area"] = ec.max_area()
    search["min_build_year"] = ec.min_build_year()

    telegram = deepcopy(baseline_cfg.get("telegram") or {})
    telegram["chat_id"] = telegram.get("chat_id") or None

    snapshot = {
        "search": search,
        "filters": {
            "reject_keywords": ec.reject_keywords(),
        },
        "scoring": scoring_cfg,
        "notifications": notifications,
        "http": deepcopy(baseline_cfg.get("http") or {}),
        "telegram": {
            "parse_mode": telegram.get("parse_mode"),
            "webhook_enabled": bool(telegram.get("webhook_enabled")),
            "fallback_chat_id": telegram.get("chat_id"),
            "bot_token": telegram.get("bot_token"),
        },
        "sources": ec.enabled_source_configs(SOURCE_REGISTRY),
        "chat_override": _override_summary(override),
        "deduplication": {
            **_DEDUP_DESCRIPTION,
            "min_group_size": ec.min_group_size(),
        },
    }
    return sanitize_snapshot(snapshot)


def sanitize_snapshot(value: Any, *, key_hint: str = "") -> Any:
    """Recursively redact secrets and sensitive URL parameters."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            out[key] = sanitize_snapshot(child, key_hint=str(key))
        return out
    if isinstance(value, list):
        return [sanitize_snapshot(item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [sanitize_snapshot(item, key_hint=key_hint) for item in value]
    if value is None:
        return None
    if _looks_sensitive_key(key_hint):
        return REDACTED if str(value).strip() else None
    if isinstance(value, str) and _looks_like_url(value):
        return redact_url(value)
    return value


def redact_url(url: str) -> str:
    if not url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = REDACTED + "@" + netloc.rsplit("@", 1)[1]
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _looks_sensitive_key(key) or key.lower() in _SENSITIVE_QUERY_KEYS:
            query_pairs.append((key, REDACTED))
        else:
            query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True, safe="*:/")
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def public_urls(baseline_cfg: dict, override: ChatOverride) -> List[tuple[str, str]]:
    ec = EffectiveConfig(baseline=baseline_cfg, override=override)
    urls: List[tuple[str, str]] = []
    dashboard_url = ec.dashboard_url()
    if dashboard_url:
        urls.append(("dashboard", redact_url(dashboard_url)))
    for name, sconf in ec.enabled_source_configs(SOURCE_REGISTRY).items():
        url = sconf.get("url")
        if url:
            urls.append((f"source.{name}", redact_url(str(url))))
    return urls


def format_config_report(baseline_cfg: dict, override: ChatOverride) -> str:
    snapshot = build_effective_snapshot(baseline_cfg, override)
    rendered = yaml.safe_dump(
        snapshot,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    ).strip()
    return "Effective runtime config\n\n" + rendered


def format_urls_report(baseline_cfg: dict, override: ChatOverride) -> str:
    rows = ["Public runtime URLs", ""]
    urls = public_urls(baseline_cfg, override)
    if not urls:
        rows.append("dashboard: not configured")
    for label, url in urls:
        rows.append(f"- {label}: {url}")
    return "\n".join(rows)


def format_decision_tree(
    baseline_cfg: dict,
    override: ChatOverride,
) -> str:
    ec = EffectiveConfig(baseline=baseline_cfg, override=override)
    # Same factories the scanner uses — the tree describes the real thing.
    filter_model = ListingFilter.from_config(ec)
    scorer = DealScorer.from_config(ec, baseline_cfg)

    lines = [
        "Decision tree (effective runtime)",
        "",
        "0. Market",
        f"- City: {city_label(ec.city_key())} ({ec.city_key()})",
        "- Source URLs are generated from this city + max_price + min_area,",
        "  unless a URL is pinned explicitly for a source.",
        "",
        "1. Source gate",
        f"- Active sources: {', '.join(ec.enabled_source_names()) or '(none)'}",
        "- Disabled sources are skipped before fetch.",
        "",
        "2. Hard filters (in execution order)",
    ]
    for rule in filter_model.describe():
        lines.append(f"- {rule}")
    lines.extend(
        [
            "",
            "3. Per-chat delivery gates",
            "- Skip if this chat already emitted the exact listing key,",
            "  UNLESS the price changed since we told this chat — then re-notify",
            "  with the delta (this is how price cuts surface).",
            "- Skip if the fuzzy key was already emitted to this chat.",
            f"- Fuzzy key formula: {_DEDUP_DESCRIPTION['cross_source']}",
            "- Missing price / area / location disables fuzzy dedup for that listing.",
            "",
            "4. Scoring",
        ]
    )
    if not scorer:
        lines.append("- Scoring disabled: pass-through notifications with no score.")
    else:
        for rule in scorer.describe_model():
            lines.append(f"- {rule}")
    lines.extend(
        [
            "",
            "5. Final outcome",
            "- reject: any hard filter fails.",
            "- skip: already emitted, cross-source duplicate for this chat, or source disabled.",
            "- notify: passes filters and delivery gates.",
            f"- aggregate: if same-source listings share the group key and count >= {ec.min_group_size()}, send one grouped message.",
            "- accept: every notified listing is persisted and can later affect median price/m² scoring.",
        ]
    )
    return "\n".join(lines)


def split_telegram_text(text: str, limit: int = 4096) -> List[str]:
    """Split a long plain-text reply into Telegram-safe chunks."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = remaining.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        chunk = remaining[:cut].rstrip()
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def number_chunks(chunks: Iterable[str], title: str) -> List[str]:
    chunks = list(chunks)
    if len(chunks) == 1:
        return chunks
    total = len(chunks)
    return [f"{title} ({i}/{total})\n\n{chunk}" for i, chunk in enumerate(chunks, start=1)]


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(bit in lowered for bit in _SENSITIVE_KEY_BITS)


def _override_summary(override: ChatOverride) -> dict:
    active_fields = []
    for key, value in override.__dict__.items():
        if key == "paused":
            if value:
                active_fields.append(key)
            continue
        if value not in (None, [], {}, ""):
            active_fields.append(key)
    return {
        "paused": override.paused,
        "active_fields": active_fields,
    }
