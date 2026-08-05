"""Serverless Telegram webhook handling."""

from __future__ import annotations

import json
import logging
import os
from typing import Mapping, Optional

from .chat_repo import ChatConfigRepo
from .commands import CommandRouter
from .runtime_config import load_runtime_config
from .storage import SeenStore
from .telegram import send_greeting

log = logging.getLogger(__name__)


def handle_webhook_request(
    *,
    method: str,
    headers: Mapping[str, str],
    body: bytes,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[int, dict[str, str], bytes]:
    env_map = os.environ if env is None else env
    if method == "GET":
        return _json_response(200, {"ok": True, "service": "telegram-webhook"})
    if method != "POST":
        return _json_response(405, {"ok": False, "error": "method_not_allowed"})

    expected_secret = (env_map.get("TG_WEBHOOK_SECRET") or "").strip()
    if expected_secret:
        actual_secret = (
            headers.get("X-Telegram-Bot-Api-Secret-Token")
            or headers.get("x-telegram-bot-api-secret-token")
            or ""
        ).strip()
        if actual_secret != expected_secret:
            return _json_response(403, {"ok": False, "error": "invalid_webhook_secret"})

    try:
        update = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return _json_response(400, {"ok": False, "error": "invalid_json"})

    cfg = load_runtime_config("config.example.yml", env=env_map)
    bot_token = ((cfg.get("telegram") or {}).get("bot_token") or "").strip()
    storage_cfg = cfg.get("storage") or {}
    with SeenStore(storage_cfg.get("db_path", "./data/seen.db")) as store:
        repo = ChatConfigRepo(store)
        if _is_membership_update(update):
            _handle_membership_update(update, bot_token, store, repo, cfg)
        else:
            _register_group_chat_from_message(update, repo)
            CommandRouter(bot_token, repo, cfg, env=dict(env_map)).process_update(update)
    return _json_response(200, {"ok": True})


def _handle_membership_update(update: dict, bot_token: str, store: SeenStore, repo: ChatConfigRepo, cfg: dict) -> None:
    update_id = update.get("update_id")
    if update_id is None or not repo.claim_update(update_id):
        return
    chat = _joined_chat(update)
    if not chat:
        return
    repo.register_chat(chat["id"], chat["title"])
    if store.is_greeted(chat["id"]):
        return
    dashboard_url = ((cfg.get("notifications") or {}).get("dashboard_url") or None)
    if send_greeting(bot_token, chat["id"], chat["title"], dashboard_url=dashboard_url):
        store.record_greeted(chat["id"], chat["title"])
        log.info("webhook greet: announced chat_id=%s (%s)", chat["id"], chat["title"])


def _register_group_chat_from_message(update: dict, repo: ChatConfigRepo) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_type = (chat.get("type") or "").strip().lower()
    if chat_type not in {"group", "supergroup"}:
        return
    chat_id = chat.get("id")
    if chat_id is None:
        return
    title = chat.get("title") or chat.get("username") or "(no title)"
    repo.register_chat(chat_id, title)


def _joined_chat(update: dict) -> Optional[dict]:
    mcm = update.get("my_chat_member") or {}
    new_member = mcm.get("new_chat_member") or {}
    old_member = mcm.get("old_chat_member") or {}
    new_status = (new_member.get("status") or "").strip().lower()
    old_status = (old_member.get("status") or "").strip().lower()
    if new_status not in {"member", "administrator"}:
        return None
    if old_status in {"member", "administrator"}:
        return None
    chat = mcm.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    return {
        "id": str(chat_id),
        "title": chat.get("title") or chat.get("username") or "(no title)",
        "type": chat.get("type"),
    }


def _is_membership_update(update: dict) -> bool:
    return bool(update.get("my_chat_member"))


def _json_response(status: int, payload: dict) -> tuple[int, dict[str, str], bytes]:
    return (
        status,
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
