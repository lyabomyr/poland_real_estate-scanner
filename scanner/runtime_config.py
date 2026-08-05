"""Runtime config loading + environment overrides.

``config.yml`` is the single baseline for every environment —
local, GitHub Actions and the dashboard all load the same file. Secrets and
public URLs are injected from environment variables on top, so credentials
never have to be written into a config file.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Mapping, Optional

import yaml


def load_yaml_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_runtime_config(
    path: str | Path = "config.yml",
    *,
    fallback_path: str | Path | None = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Load YAML config, optionally falling back to a tracked template.

    ``fallback_path`` covers callers that point at a custom config and want a
    graceful degrade to the tracked baseline.
    """
    cfg_path = Path(path)
    if cfg_path.exists():
        base = load_yaml_config(cfg_path)
    elif fallback_path is not None and Path(fallback_path).exists():
        base = load_yaml_config(fallback_path)
    else:
        raise FileNotFoundError(str(path))
    return apply_env_overrides(base, env=env)


def apply_env_overrides(
    cfg: dict,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Return a copy of ``cfg`` with supported env overrides applied."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    out = normalize_config(deepcopy(cfg))

    tg = out.setdefault("telegram", {})
    if _env_value(env_map, "TG_BOT_TOKEN"):
        tg["bot_token"] = env_map["TG_BOT_TOKEN"].strip()
    if "TG_CHAT_ID" in env_map:
        tg["chat_id"] = env_map.get("TG_CHAT_ID", "").strip()
    if _env_value(env_map, "TG_PARSE_MODE"):
        tg["parse_mode"] = env_map["TG_PARSE_MODE"].strip()

    notifications = out.setdefault("notifications", {})
    if "DASHBOARD_URL" in env_map:
        dashboard = env_map.get("DASHBOARD_URL", "").strip()
        notifications["dashboard_url"] = dashboard or None

    storage = out.setdefault("storage", {})
    if _env_value(env_map, "SCANNER_DB_PATH"):
        storage["db_path"] = env_map["SCANNER_DB_PATH"].strip()

    return out


def normalize_config(cfg: dict) -> dict:
    """Drop legacy config blocks that should no longer participate at runtime."""
    sources = cfg.get("sources")
    if isinstance(sources, dict):
        sources.pop("bzp", None)
    return cfg


def _env_value(env: Mapping[str, str], key: str) -> bool:
    value = env.get(key)
    return bool(value and value.strip())
