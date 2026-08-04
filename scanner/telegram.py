import logging
import time
from typing import List

import requests

from .format import format_html
from .models import Listing

log = logging.getLogger(__name__)


def discover_chats(bot_token: str, timeout: int = 10) -> List[dict]:
    """Return chats the bot has recently seen activity in.

    Uses Telegram ``getUpdates``. Only sees events from ~the last 24h and only
    when no webhook is configured on the bot. Returns dicts with keys
    ``id``, ``type``, ``title``.
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
    seen = {}
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
        if not self.is_configured():
            log.debug("telegram not configured; skipping send for %s", l.url)
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": format_html(l),
                    "parse_mode": self.parse_mode,
                    "disable_web_page_preview": "false",
                },
                timeout=30,
            )
            if r.status_code == 429:
                retry = int(r.json().get("parameters", {}).get("retry_after", 5))
                log.warning("telegram rate-limited; sleeping %ds", retry + 1)
                time.sleep(retry + 1)
                return self.send(l)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("telegram send failed for %s: %s", l.url, e)
            return False
