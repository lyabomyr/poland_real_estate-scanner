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

#: Longest 429 backoff worth sitting through. Beyond this, giving up is
#: strictly better: the listing stays in the delivery backlog and goes out on
#: the next run, whereas sleeping burns the run's remaining time budget and
#: delivers nothing at all.
MAX_RATE_LIMIT_WAIT_SECONDS = 30

#: Attempts per message before it is left for the next run. Telegram can 429
#: repeatedly while a backlog drains, and one message must never be able to
#: monopolise the run.
RATE_LIMIT_ATTEMPTS = 3


def default_reply_keyboard() -> dict:
    """Persistent reply keyboard shown below the chat's input field.

    Each button's *text is the exact command it fires* so the bot
    recognises them in the ``getUpdates`` payload with zero extra parsing.
    `is_persistent: true` (Bot API 6.4+) keeps the keyboard open by default
    instead of hiding it behind the tiny "keyboard" icon.

    Tapping a button is not instant: commands are drained by the scheduled
    scan, and GitHub throttles those heavily — expect an hour or two.
    """
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/help"}, {"text": "/dashboard"}],
            [{"text": "/config"}, {"text": "/decision_tree"}, {"text": "/urls"}],
            [{"text": "/stats"}, {"text": "/kw list"}, {"text": "/grouping"}],
            [{"text": "/pause"}, {"text": "/resume"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def fetch_updates(bot_token: str, timeout: int = 10) -> list:
    """Raw ``getUpdates`` result, or an empty list when there is nothing to read.

    One place, because three callers need it — chat discovery, membership
    detection and the command router — and each had its own copy of the same
    token guard, request and ``ok`` check.

    Only surfaces the last ~24 h; that is Telegram's retention, not ours.
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
    return data.get("result", []) or [] if data.get("ok") else []


def find_new_chat_memberships(bot_token: str, timeout: int = 10) -> List[dict]:
    """Return chats where the bot has *become* a member/admin recently.

    Reads ``my_chat_member`` updates from getUpdates and keeps only
    transitions into an active state (``member`` / ``administrator``). Used
    by the local polling fallback: the scanner announces the chat's id back
    to the group so the user can optionally whitelist it for workflow
    dispatch or use it as a fallback bootstrap.

    Only sees events from the last ~24 h — Telegram's default retention.
    """
    updates = fetch_updates(bot_token, timeout)

    out = {}
    for upd in updates:
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


def send_greeting(
    bot_token: str,
    chat_id,
    title: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> bool:
    """Post a "here's your chat_id" message to the given chat.

    A fresh group where the bot was just added gets its ``chat_id`` in a
    copy-friendly format plus (if configured) a link to the Streamlit
    dashboard for GUI-side tuning.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return False
    title_line = f"\n<i>{title}</i>" if title else ""
    dashboard_line = (
        f"\n\n📊 Dashboard: {dashboard_url}"
        if dashboard_url else ""
    )
    text = (
        "👋 <b>Kraków flats scanner</b> is here."
        f"{title_line}\n\n"
        f"Chat ID: <code>{chat_id}</code>\n"
        "This chat is registered — new matches will start arriving here.\n\n"
        "⏱ <b>Commands are not instant — often an hour or two.</b>\n"
        "They are read during the scheduled scan, and GitHub throttles free "
        "scheduled runs. Nothing is lost; your command waits for the next "
        "one. Send /help for the command list."
        f"{dashboard_line}"
    )
    try:
        return send_message(
            bot_token,
            chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=default_reply_keyboard(),
        )
    except Exception as e:
        log.error("greet: send to %s failed: %s", chat_id, e)
        return False


def discover_chats(bot_token: str, timeout: int = 10) -> List[dict]:
    """Return chats the bot has *recently* seen activity in.

    Uses Telegram ``getUpdates``. Only sees events from ~the last 24 h, and
    only when no webhook is registered on the bot. This is primarily a local
    development / debugging helper. Returns dicts with keys ``id`` / ``type``
    / ``title``. Empty list on any error — main.py logs a friendly message
    asking the user to poke the bot to generate an update.

    Caveat: in a group with Privacy Mode ON (BotFather default) the bot only
    sees commands addressed to it, so a plain "hi" won't surface the chat.
    """
    updates = fetch_updates(bot_token, timeout)

    # Same chat may appear across multiple update types (message, my_chat_member,
    # …). Dedup by chat.id, keep the first one we saw.
    seen: dict = {}
    for upd in updates:
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


def chat_dashboard_url(dashboard_url: Optional[str], chat_id) -> Optional[str]:
    """Deep-link to this chat's config page: ``<base>/Chat_config?chat_id=…``.

    Streamlit exposes pages at ``/<Page_Name>`` and reads ``?chat_id=`` into
    ``st.query_params``, which the dashboard uses to preselect the chat. So a
    group can jump from Telegram straight to its own settings.
    """
    if not dashboard_url:
        return None
    return f"{dashboard_url.rstrip('/')}/Chat_config?chat_id={chat_id}"


def pin_message(bot_token: str, chat_id, message_id: int, timeout: int = 15) -> bool:
    """Pin a message. Best-effort: needs admin rights in groups.

    Returns False (and logs at debug) when the bot isn't an admin — pinning
    is a nicety and must never interrupt a scan.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/pinChatMessage",
            data={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "disable_notification": "true",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception as e:
        # Very common and harmless: "not enough rights to pin a message".
        log.debug("pin failed in %s: %s", chat_id, e)
        return False


def send_message(
    bot_token: str,
    chat_id,
    *,
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
    reply_markup: Optional[dict] = None,
    reply_to_message_id: Optional[int] = None,
    timeout: int = 15,
) -> bool:
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = str(reply_to_message_id)
    r = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        timeout=timeout,
    )
    r.raise_for_status()
    return True


def send_message_returning_id(
    bot_token: str,
    chat_id,
    *,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[dict] = None,
    timeout: int = 15,
) -> Optional[int]:
    """Send a message and return its ``message_id`` (needed to pin it)."""
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            timeout=timeout,
        )
        r.raise_for_status()
        return (r.json().get("result") or {}).get("message_id")
    except Exception as e:
        log.error("send failed to %s: %s", chat_id, e)
        return None


def get_chat_title(bot_token: str, chat_id, timeout: int = 15) -> Optional[str]:
    """Resolve a chat's human-readable name via ``getChat``.

    Used to replace placeholder titles (e.g. a chat seeded from
    ``telegram.chat_id`` before the bot ever saw a message there) so the
    dashboard's chat picker shows real names instead of internal labels.

    Returns ``None`` on any failure — a missing title is cosmetic and must
    never break a scan.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getChat",
            params={"chat_id": chat_id},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return None
        chat = data.get("result") or {}
        return (
            chat.get("title")
            or chat.get("username")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or None
        )
    except Exception as e:
        log.debug("getChat failed for %s: %s", chat_id, e)
        return None


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

    def _post(self, text: str, preview: bool, tag: str, attempt: int = 1) -> bool:
        if not self.is_configured():
            log.warning("telegram not configured; %s was not sent", tag)
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
                return self._wait_out_rate_limit(r, text, preview, tag, attempt)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("telegram send failed for %s: %s", tag, e)
            return False

    def _wait_out_rate_limit(self, response, text, preview, tag, attempt: int) -> bool:
        """Honour a 429, but only when waiting is actually cheaper than giving up.

        Telegram supplies the backoff, so the wait itself is never guessed.
        What needs a decision is when to stop waiting. Draining a backlog of
        several hundred messages means hitting the ~20-per-minute group limit
        constantly, and a run that spends its budget asleep delivers less than
        one that gives up early: an undelivered listing is not lost, it simply
        stays in the backlog and goes out on the next run.

        So: short waits are worth taking, long ones are not, and a message is
        never retried indefinitely. Bounded iteration rather than the
        recursion this used to do, which could nest a frame per retry.
        """
        retry = int((response.json().get("parameters") or {}).get("retry_after", 5))
        if retry > MAX_RATE_LIMIT_WAIT_SECONDS or attempt >= RATE_LIMIT_ATTEMPTS:
            log.warning(
                "telegram rate-limited for %ss on %s — leaving it in the "
                "backlog for the next run", retry, tag,
            )
            return False
        log.info("telegram rate-limited; sleeping %ds then retrying %s", retry + 1, tag)
        time.sleep(retry + 1)   # +1s margin against clock drift
        return self._post(text, preview, tag, attempt=attempt + 1)
