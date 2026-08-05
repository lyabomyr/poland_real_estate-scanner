"""Telegram command routing.

Commands are read by **polling**: the scanner calls ``getUpdates`` once per
run, so a command sent at 12:01 is answered by the run that starts at 12:15.
Replies therefore take **up to 15 minutes** — that is the scheduling
interval, not slow code. Users are told this in :meth:`CommandRouter._cmd_help`
and in the greeting, because otherwise a silent bot looks broken.

Mutations (``/max_price``, ``/kw``, ``/source`` …) land in ``chat_configs``
in the same run that answers them, so the very same scan already applies the
new setting.

Every command works against the same Turso-backed rows that the scanner and
the Streamlit dashboard read, so all three always agree on effective state.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

import requests

from .chat_config import ChatOverride, EffectiveConfig
from .chat_repo import ChatConfigRepo
from .registry import KNOWN_SOURCES
from .introspection import (
    dashboard_url_from_cfg,
    format_config_report,
    format_decision_tree,
    format_urls_report,
    number_chunks,
    split_telegram_text,
)
from .telegram import default_reply_keyboard, send_message

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/{method}"
# Single source of truth — see scanner/registry.py
_KNOWN_SOURCES = KNOWN_SOURCES


@dataclass
class BotReply:
    text: str
    parse_mode: Optional[str] = "HTML"
    disable_web_page_preview: bool = True


@dataclass
class CommandContext:
    chat_id: str
    chat_title: Optional[str]
    chat_type: Optional[str]
    user_id: Optional[int]
    user_name: Optional[str]
    message_id: Optional[int]


class CommandRouter:
    """Reads Telegram updates and dispatches ``/commands`` to handlers."""

    def __init__(
        self,
        bot_token: str,
        repo: ChatConfigRepo,
        baseline_cfg: dict,
        *,
        env: Optional[dict] = None,
    ):
        self.bot_token = bot_token
        self.repo = repo
        self.baseline_cfg = baseline_cfg
        self.env = os.environ if env is None else env
        self._handlers: Dict[str, Callable[[List[str], ChatOverride, CommandContext], List[BotReply]]] = {
            "help": lambda a, o, c: self._cmd_help(),
            "start": lambda a, o, c: self._cmd_help(),
            "status": self._cmd_status,
            "config": self._cmd_config,
            "urls": self._cmd_urls,
            "decision_tree": self._cmd_decision_tree,
            "dashboard": lambda a, o, c: self._cmd_dashboard(),
            "max_price": lambda a, o, c: self._set_int(a, o, "max_price"),
            "min_area": lambda a, o, c: self._set_float(a, o, "min_area"),
            "max_area": lambda a, o, c: self._set_float(a, o, "max_area"),
            "min_year": lambda a, o, c: self._set_int(a, o, "min_build_year"),
            "reset": self._cmd_reset,
            "source": self._cmd_source,
            "kw": self._cmd_kw,
            "pause": lambda a, o, c: self._cmd_pause(o, True),
            "resume": lambda a, o, c: self._cmd_pause(o, False),
            "stats": self._cmd_stats,
        }

    def process_pending(self) -> int:
        """Fetch new getUpdates, dispatch every ``/…`` message. Returns count."""
        if not self.bot_token or self.bot_token.startswith("REPLACE"):
            return 0
        try:
            r = requests.get(
                _TG_API.format(token=self.bot_token, method="getUpdates"),
                params={"limit": 100},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("command router: getUpdates failed: %s", e)
            return 0
        if not data.get("ok"):
            return 0

        handled = 0
        for upd in data.get("result", []) or []:
            handled += 1 if self.process_update(upd) else 0
        return handled

    def process_update(self, upd: dict) -> bool:
        """Process one Telegram update dict. Returns True if it was a command."""
        update_id = upd.get("update_id")
        if update_id is None or not self.repo.claim_update(update_id):
            return False

        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not text.startswith("/") or chat_id is None:
            return False

        ctx = CommandContext(
            chat_id=str(chat_id),
            chat_title=chat.get("title") or chat.get("first_name"),
            chat_type=chat.get("type"),
            user_id=(msg.get("from") or {}).get("id"),
            user_name=(msg.get("from") or {}).get("username")
            or (msg.get("from") or {}).get("first_name"),
            message_id=msg.get("message_id"),
        )
        self.repo.register_chat(ctx.chat_id, ctx.chat_title)
        replies = self._dispatch(ctx, text)
        if not replies:
            return True
        for reply in replies:
            self._send(ctx.chat_id, reply, reply_to_message_id=ctx.message_id)
        log.info("command router: chat=%s cmd=%r", ctx.chat_id, text[:80])
        return True

    def _dispatch(self, ctx: CommandContext, text: str) -> List[BotReply]:
        parts = text.split()
        head = parts[0][1:].lower().split("@", 1)[0]
        args = parts[1:]

        row = self.repo.get(ctx.chat_id)
        override = row.override if row else ChatOverride()
        title = ctx.chat_title or (row.title if row else None)

        handler = self._handlers.get(head)
        if not handler:
            return [BotReply(f"Unknown command: /{head}. Try /help.")]
        try:
            replies = handler(args, override, ctx)
        except Exception as e:
            log.exception("command %s failed", head)
            return [BotReply(f"⚠️ /{head} failed: {e}")]

        self.repo.upsert(ctx.chat_id, title, override, enabled=True)
        return replies

    def _cmd_help(self) -> List[BotReply]:
        lines = [
            "⏱ <b>Replies take up to 15 minutes.</b>",
            "The bot reads commands during its scheduled scan, which runs "
            "every 15 min — so your message waits for the next run. Nothing "
            "is lost, it just isn't instant.",
            "",
            "<b>Commands</b>",
            "/status — short summary for this chat",
            "/config — full effective runtime config (chunked if long)",
            "/urls — public runtime URLs",
            "/decision_tree — current accept/reject/notify logic",
            "/dashboard — link to the Streamlit dashboard",
            "/max_price N — override max price (PLN)",
            "/min_area N — override min area (m²)",
            "/max_area N — set an upper area cap",
            "/min_year Y — earliest build year accepted",
            "/source NAME on|off — enable/disable a data source",
            "/source NAME url URL — custom URL for a source (this chat only)",
            "/kw + NAME [WEIGHT] — add positive scoring keyword",
            "/kw - NAME [WEIGHT] — add negative scoring keyword",
            "/kw reject NAME — add reject-filter keyword",
            "/kw del NAME — remove a keyword override",
            "/kw list — show keyword overrides",
            "/reset FIELD — clear one override (e.g. /reset max_price)",
            "/reset all — clear all overrides",
            "/pause — stop receiving matches here",
            "/resume — resume receiving",
            "/stats [N] — emitted count over the last N days (default 7)",
            "",
            f"Sources: {', '.join(_KNOWN_SOURCES)}",
        ]
        url = self._dashboard_url()
        if url:
            lines.append(f"\n📊 Dashboard: {url}")
        return [BotReply("\n".join(lines))]

    def _cmd_dashboard(self) -> List[BotReply]:
        url = self._dashboard_url()
        if not url:
            return [
                BotReply(
                    "No dashboard URL configured yet.\n\n"
                    "Deploy the <code>dashboard/</code> app and set "
                    "<code>DASHBOARD_URL</code> as a GitHub Actions variable "
                    "(or <code>notifications.dashboard_url</code> locally)."
                )
            ]
        return [BotReply(f"📊 Dashboard: {url}")]

    def _cmd_status(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        ec = EffectiveConfig(baseline=self.baseline_cfg, override=override)
        lines = [
            "<b>Status for this chat</b>",
            "",
            f"max_price: {ec.max_price()} PLN",
            f"min_area: {ec.min_area():g} m²",
            f"max_area: {ec.max_area():g} m²" if ec.max_area() is not None else "max_area: off",
            (
                f"min_year: {ec.min_build_year()}"
                if ec.min_build_year() is not None
                else "min_year: off"
            ),
            f"sources: {', '.join(ec.enabled_source_names()) or '(none)'}",
            f"group notifications: {ec.min_group_size()}+ similar listings",
        ]
        if override.paused:
            lines.append("⏸️ paused — /resume to re-enable")
        stats = self.repo.stats_last_days(ctx.chat_id, 7)
        lines.append(f"delivered last 7 days: {stats['emitted']}")
        url = ec.dashboard_url()
        if url:
            lines.append(f"\n📊 Dashboard: {url}")
        lines.append("")
        lines.append("<i>Use /config for the full effective runtime config.</i>")
        return [BotReply("\n".join(lines))]

    def _cmd_config(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        return self._chunked_plain_reply(
            format_config_report(self.baseline_cfg, override),
            title="/config",
        )

    def _cmd_urls(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        return self._chunked_plain_reply(
            format_urls_report(self.baseline_cfg, override),
            title="/urls",
        )

    def _cmd_decision_tree(
        self,
        args,
        override: ChatOverride,
        ctx: CommandContext,
    ) -> List[BotReply]:
        return self._chunked_plain_reply(
            format_decision_tree(self.baseline_cfg, override),
            title="/decision_tree",
        )

    def _set_int(self, args, override: ChatOverride, attr: str) -> List[BotReply]:
        if not args:
            return [BotReply(f"Usage: /{attr} N (integer)", parse_mode=None)]
        try:
            value = int(args[0])
        except ValueError:
            return [BotReply(f"'{args[0]}' is not an integer.", parse_mode=None)]
        setattr(override, attr, value)
        return [BotReply(f"✓ {attr} = {value}", parse_mode=None)]

    def _set_float(self, args, override: ChatOverride, attr: str) -> List[BotReply]:
        if not args:
            return [BotReply(f"Usage: /{attr} N", parse_mode=None)]
        try:
            value = float(args[0].replace(",", "."))
        except ValueError:
            return [BotReply(f"'{args[0]}' is not a number.", parse_mode=None)]
        setattr(override, attr, value)
        return [BotReply(f"✓ {attr} = {value:g}", parse_mode=None)]

    def _cmd_reset(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        if not args:
            return [BotReply("Usage: /reset FIELD  (or /reset all)", parse_mode=None)]
        field = args[0].lower()
        if field == "all":
            override.__dict__.update(asdict(ChatOverride()))
            return [BotReply("✓ all overrides cleared", parse_mode=None)]
        empty = ChatOverride()
        if not hasattr(empty, field):
            return [BotReply(f"Unknown field '{field}'.", parse_mode=None)]
        setattr(override, field, getattr(empty, field))
        return [BotReply(f"✓ {field} reset to baseline", parse_mode=None)]

    def _cmd_source(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        if len(args) < 2:
            return [BotReply("Usage: /source NAME on|off   OR   /source NAME url URL", parse_mode=None)]
        name = args[0].lower()
        if name not in _KNOWN_SOURCES:
            return [
                BotReply(
                    f"Unknown source '{name}'. Try one of: {', '.join(_KNOWN_SOURCES)}",
                    parse_mode=None,
                )
            ]
        verb = args[1].lower()
        if verb in ("on", "enable"):
            override.disabled_sources = [s for s in override.disabled_sources if s != name]
            return [BotReply(f"✓ source {name} enabled for this chat", parse_mode=None)]
        if verb in ("off", "disable"):
            if name not in override.disabled_sources:
                override.disabled_sources.append(name)
            return [BotReply(f"✓ source {name} disabled for this chat", parse_mode=None)]
        if verb == "url":
            if len(args) < 3:
                return [BotReply("Usage: /source NAME url URL", parse_mode=None)]
            url = " ".join(args[2:])
            override.source_urls[name] = url
            return [BotReply(f"✓ source {name} URL set for this chat", parse_mode=None)]
        return [BotReply("Usage: /source NAME on|off   OR   /source NAME url URL", parse_mode=None)]

    def _cmd_kw(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        if not args:
            return [
                BotReply(
                    "Usage: /kw + NAME [W]  |  /kw - NAME [W]  |  /kw reject NAME  |  /kw del NAME  |  /kw list",
                    parse_mode=None,
                )
            ]
        verb = args[0].lower()
        if verb == "list":
            return [BotReply(_describe_keywords(override))]
        if verb in ("+", "add+", "positive"):
            return [_add_kw(override.extra_positive, args[1:], sign="+")]
        if verb in ("-", "add-", "negative"):
            return [_add_kw(override.extra_negative, args[1:], sign="-")]
        if verb == "reject":
            if len(args) < 2:
                return [BotReply("Usage: /kw reject NAME", parse_mode=None)]
            name = " ".join(args[1:])
            if name in override.extra_reject:
                return [BotReply(f"'{name}' already in extra reject list", parse_mode=None)]
            override.extra_reject.append(name)
            return [BotReply(f"✓ reject '{name}' added for this chat", parse_mode=None)]
        if verb == "del":
            if len(args) < 2:
                return [BotReply("Usage: /kw del NAME", parse_mode=None)]
            name = " ".join(args[1:])
            removed = _remove_kw_from_all(override, name)
            if removed:
                return [BotReply(f"✓ removed '{name}' from {removed} override list(s)", parse_mode=None)]
            return [BotReply(f"'{name}' not found in any override", parse_mode=None)]
        return [BotReply("Unknown /kw sub-command. Try /kw list.", parse_mode=None)]

    def _cmd_pause(self, override: ChatOverride, pause: bool) -> List[BotReply]:
        override.paused = pause
        return [BotReply("⏸️ paused — /resume to re-enable" if pause else "▶️ resumed", parse_mode=None)]

    def _cmd_stats(self, args, override: ChatOverride, ctx: CommandContext) -> List[BotReply]:
        days = 7
        if args:
            try:
                days = max(1, min(90, int(args[0])))
            except ValueError:
                return [BotReply(f"'{args[0]}' is not an integer.", parse_mode=None)]
        stats = self.repo.stats_last_days(ctx.chat_id, days)
        lines = [f"Delivered in the last {days} day(s): {stats['emitted']}"]
        url = self._dashboard_url()
        if url:
            lines.append(f"Dashboard: {url}")
        return [BotReply("\n".join(lines), parse_mode=None)]

    def _chunked_plain_reply(self, text: str, *, title: str) -> List[BotReply]:
        chunks = number_chunks(split_telegram_text(text, limit=3900), title)
        return [BotReply(chunk, parse_mode=None) for chunk in chunks]

    def _dashboard_url(self) -> Optional[str]:
        return dashboard_url_from_cfg(self.baseline_cfg)

    def _parse_csv_ids(self, raw: str) -> set[str]:
        return {part.strip() for part in (raw or "").split(",") if part.strip()}

    def _send(self, chat_id: str, reply: BotReply, *, reply_to_message_id: Optional[int] = None) -> None:
        try:
            send_message(
                self.bot_token,
                chat_id,
                text=reply.text,
                parse_mode=reply.parse_mode,
                disable_web_page_preview=reply.disable_web_page_preview,
                reply_markup=default_reply_keyboard(),
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as e:
            log.error("command router: reply to %s failed: %s", chat_id, e)


def _add_kw(target: list, args: list, sign: str) -> BotReply:
    if not args:
        return BotReply(f"Usage: /kw {sign} NAME [W]", parse_mode=None)
    name = args[0]
    weight: Optional[int] = None
    if len(args) >= 2:
        try:
            weight = int(args[1])
        except ValueError:
            return BotReply(f"'{args[1]}' is not an integer weight.", parse_mode=None)
    entry = {"name": name, "weight": weight} if weight is not None else name
    for existing in target:
        existing_name = existing["name"] if isinstance(existing, dict) else existing
        if existing_name == name:
            return BotReply(f"'{name}' already in {sign} keyword list", parse_mode=None)
    target.append(entry)
    tail = f" (weight {weight})" if weight is not None else ""
    return BotReply(f"✓ {sign} '{name}'{tail}", parse_mode=None)


def _remove_kw_from_all(override: ChatOverride, name: str) -> int:
    def _prune(values):
        out = [entry for entry in values if _kw_name(entry) != name]
        return out, len(values) - len(out)

    total = 0
    override.extra_positive, removed = _prune(override.extra_positive)
    total += removed
    override.extra_negative, removed = _prune(override.extra_negative)
    total += removed
    override.extra_reject, removed = _prune(override.extra_reject)
    total += removed
    return total


def _kw_name(entry) -> str:
    return entry["name"] if isinstance(entry, dict) else str(entry)


def _kw_repr(entry) -> str:
    if isinstance(entry, dict):
        weight = entry.get("weight")
        return f"{entry['name']}({weight:+})" if weight is not None else entry["name"]
    return str(entry)


def _describe_keywords(override: ChatOverride) -> str:
    lines = ["<b>Keyword overrides for this chat</b>"]
    if override.extra_positive:
        lines.append("+ " + ", ".join(_kw_repr(k) for k in override.extra_positive))
    if override.extra_negative:
        lines.append("− " + ", ".join(_kw_repr(k) for k in override.extra_negative))
    if override.extra_reject:
        lines.append("reject: " + ", ".join(override.extra_reject))
    if len(lines) == 1:
        lines.append("<i>no overrides yet — use /kw + NAME [WEIGHT] to add</i>")
    return "\n".join(lines)
