"""Telegram delivery + a helper for auto-discovering the ``chat_id``.

The Bot API's per-group limit is ~20 messages/minute — a fresh scan easily
exceeds that, so :meth:`_post` handles ``429 Too Many Requests`` by sleeping
for ``retry_after`` seconds and retrying. The channel *never* misses a
message; the run just takes a bit longer.

Interactive UX
--------------
Command replies + the initial greeting attach a **persistent reply
keyboard** (buttons below the input field) so the user can trigger common
commands with a tap instead of typing. Listing notifications do NOT set
the keyboard — once shown, Telegram keeps it in the chat regardless of
subsequent message sources.
"""

import json
import logging
import time
from typing import List, Optional

import requests

from .aggregator import ListingGroup
from .format import format_group_html, format_html
from .models import Listing

log = logging.getLogger(__name__)


def default_reply_keyboard() -> dict:
    """Persistent reply keyboard shown below the chat's input field.

    Two rows of buttons; each button's *text is the exact command it fires*
    so the bot recognises them in the ``getUpdates`` message payload with
    zero extra parsing. `is_persistent: true` (Bot API 6.4+) means the
    keyboard stays open by default instead of hiding behind the tiny
    "keyboard" icon.
    """
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/help"},    {"text": "/stats"}],
            [{"text": "/kw list"}, {"text": "/pause"}, {"text": "/resume"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def find_new_chat_memberships(bot_token: str, timeout: int = 10) -> List[dict]:
    """Return chats where the bot has *become* a member/admin recently.

    Reads ``my_chat_member`` updates from getUpdates and keeps only
    transitions into an active state (``member`` / ``administrator``). Used
    by the auto-greet feature: the scanner announces the chat's id back to
    the group so the user can pin it in :envvar:`TG_CHAT_ID` without
    installing a helper bot.

    Only sees events from the last ~24 h — Telegram's default retention.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return []
    r = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params={"limit": 100},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []

    out = {}
    for upd in data.get("result", []) or []:
        mcm = upd.get("my_chat_member")
        if not mcm:
            continue
        new_status = (mcm.get("new_chat_member") or {}).get("status")
        if new_status not in ("member", "administrator"):
            continue
        chat = mcm.get("chat") or {}
        cid = chat.get("id")
        if cid is None or cid in out:
            continue
        out[cid] = {
            "id": cid,
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or "(no title)",
        }
    return list(out.values())


def send_greeting(bot_token: str, chat_id, title: Optional[str] = None) -> bool:
    """Post a "here's your chat_id" message to the given chat.

    The whole point of this helper is user-facing: a fresh group where the
    bot was just added gets a message with its ``chat_id`` in a copy-friendly
    format so the user can plug it into their :envvar:`TG_CHAT_ID` secret.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return False
    title_line = f"\n<i>{title}</i>" if title else ""
    text = (
        "👋 <b>Kraków flats scanner</b> is here."
        f"{title_line}\n\n"
        f"Chat ID: <code>{chat_id}</code>"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                # Greeting is the first thing users see — good moment to
                # surface the persistent button menu.
                "reply_markup": json.dumps(default_reply_keyboard()),
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("greet: send to %s failed: %s", chat_id, e)
        return False


def discover_chats(bot_token: str, timeout: int = 10) -> List[dict]:
    """Return chats the bot has *recently* seen activity in.

    Uses Telegram ``getUpdates``. Only sees events from ~the last 24 h, and
    only when no webhook is registered on the bot. Returns dicts with keys
    ``id`` / ``type`` / ``title``. Empty list on any error — main.py logs a
    friendly message asking the user to poke the bot to generate an update.

    Caveat: in a group with Privacy Mode ON (BotFather default) the bot only
    sees commands addressed to it, so a plain "hi" won't surface the chat.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return []
    r = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params={"limit": 100},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []

    # Same chat may appear across multiple update types (message, my_chat_member,
    # …). Dedup by chat.id, keep the first one we saw.
    seen: dict = {}
    for upd in data.get("result", []) or []:
        for key in ("message", "channel_post", "edited_message",
                    "edited_channel_post", "my_chat_member", "chat_member"):
            m = upd.get(key)
            if not m:
                continue
            chat = m.get("chat") or {}
            cid = chat.get("id")
            if cid is None or cid in seen:
                continue
            seen[cid] = {
                "id": cid,
                "type": chat.get("type"),
                "title": (
                    chat.get("title")
                    or chat.get("username")
                    or chat.get("first_name")
                    or "(no title)"
                ),
            }
    return list(seen.values())


class TelegramNotifier:
    """Thin wrapper around Bot API ``sendMessage``.

    Handles two output shapes — single :class:`Listing` and aggregated
    :class:`ListingGroup` — through a common :meth:`_post` that respects 429
    rate limits.
    """

    def __init__(self, bot_token: str, chat_id: str, parse_mode: str = "HTML"):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode

    def is_configured(self) -> bool:
        return (
            bool(self.bot_token)
            and not self.bot_token.startswith("REPLACE")
            and bool(self.chat_id)
            and not self.chat_id.startswith("REPLACE")
        )

    def send(self, l: Listing) -> bool:
        # Single listings get a link preview — nicer visually.
        return self._post(text=format_html(l), preview=True, tag=l.url)

    def send_group(self, g: ListingGroup) -> bool:
        # A group has many URLs; link previews would visually explode the message.
        return self._post(text=format_group_html(g), preview=False, tag=f"group:{g.label}")

    def _post(self, text: str, preview: bool, tag: str) -> bool:
        if not self.is_configured():
            log.debug("telegram not configured; skipping send for %s", tag)
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": self.parse_mode,
                    "disable_web_page_preview": "false" if preview else "true",
                },
                timeout=30,
            )
            if r.status_code == 429:
                # Telegram returns the exact backoff — sleep, then retry.
                # +1 s gives us a safety margin against clock drift.
                retry = int(r.json().get("parameters", {}).get("retry_after", 5))
                log.warning("telegram rate-limited; sleeping %ds", retry + 1)
                time.sleep(retry + 1)
                return self._post(text, preview, tag)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("telegram send failed for %s: %s", tag, e)
            return False
