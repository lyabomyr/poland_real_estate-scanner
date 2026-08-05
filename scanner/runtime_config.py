"""Runtime config loading + environment overrides.

Local development typically uses ``config.yml`` copied from
``config.example.yml``. Deployment targets can instead load the tracked
template and inject secrets / public URLs through environment variables, so
we never have to render credentials into a temporary file.
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

    ``fallback_path`` is useful for deployments where secrets come from env
    vars and we don't want to materialise a separate ``config.yml`` file.
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
    if "TG_WEBHOOK_ENABLED" in env_map:
        tg["webhook_enabled"] = _parse_bool(env_map.get("TG_WEBHOOK_ENABLED"))

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


def runtime_has_webhook(cfg: dict, *, env: Optional[Mapping[str, str]] = None) -> bool:
    env_map: Mapping[str, str] = os.environ if env is None else env
    if "TG_WEBHOOK_ENABLED" in env_map:
        return _parse_bool(env_map.get("TG_WEBHOOK_ENABLED"))
    return bool((cfg.get("telegram") or {}).get("webhook_enabled"))


def _env_value(env: Mapping[str, str], key: str) -> bool:
    value = env.get(key)
    return bool(value and value.strip())


def _parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
