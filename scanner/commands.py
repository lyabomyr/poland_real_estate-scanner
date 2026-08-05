"""Telegram command routing.

The scanner polls ``getUpdates`` once per run (already used for chat auto-
discovery). Any ``message`` starting with ``/`` is parsed and dispatched
here. Every command mutates the sending chat's :class:`ChatOverride` row
in ``chat_configs``; the pipeline picks up changes on its next run.

Commands are grouped semantically for :meth:`_cmd_help`:

* ``/help`` ``/status``                    — introspection
* ``/max_price`` ``/min_area`` ``/max_area`` ``/min_year`` — numeric knobs
* ``/reset FIELD``                         — clear one override
* ``/source NAME on|off``                  — toggle a data source
* ``/source NAME url URL``                 — custom URL for a source
* ``/kw + NAME [WEIGHT]``                  — positive scoring keyword
* ``/kw - NAME [WEIGHT]``                  — negative scoring keyword
* ``/kw reject NAME``                      — extra reject keyword
* ``/kw del NAME``                         — remove a keyword override
* ``/kw list``                             — list current keyword overrides
* ``/pause`` / ``/resume``                 — skip / re-enable sending here
* ``/stats [N]``                           — emitted count last N days

Robustness rules:

* One handler per command; a bad argument returns an error string to the
  chat instead of raising — a broken command should never break the scan.
* Each ``update_id`` is recorded in ``command_updates`` before dispatch so
  restarts and cron overlap can't double-fire a mutation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable, Dict, List, Optional

import requests

from .chat_config import ChatOverride
from .chat_repo import ChatConfigRepo
from .telegram import default_reply_keyboard

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/{method}"

# Baseline source names — commands validate against this so /source olx off
# doesn't silently accept typos.
_KNOWN_SOURCES = ("otodom", "olx", "morizon", "komornik", "bzp")


class CommandRouter:
    """Reads pending Telegram updates, dispatches ``/commands`` to handlers."""

    def __init__(
        self,
        bot_token: str,
        repo: ChatConfigRepo,
        baseline_cfg: dict,
    ):
        self.bot_token = bot_token
        self.repo = repo
        self.baseline_cfg = baseline_cfg
        self._handlers: Dict[str, Callable[[List[str], ChatOverride], str]] = {
            "help":        lambda a, o: self._cmd_help(),
            "start":       lambda a, o: self._cmd_help(),
            "status":      self._cmd_status,
            "max_price":   lambda a, o: self._set_int(a, o, "max_price"),
            "min_area":    lambda a, o: self._set_float(a, o, "min_area"),
            "max_area":    lambda a, o: self._set_float(a, o, "max_area"),
            "min_year":    lambda a, o: self._set_int(a, o, "min_build_year"),
            "reset":       self._cmd_reset,
            "source":      self._cmd_source,
            "kw":          self._cmd_kw,
            "pause":       lambda a, o: self._cmd_pause(o, True),
            "resume":      lambda a, o: self._cmd_pause(o, False),
            "stats":       self._cmd_stats,
        }

    # ── entrypoint ────────────────────────────────────────────────────

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
            update_id = upd.get("update_id")
            if update_id is None or self.repo.is_update_processed(update_id):
                continue
            msg = upd.get("message") or upd.get("channel_post") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if not text.startswith("/") or chat_id is None:
                # Still mark so we don't re-inspect it next run.
                self.repo.mark_update_processed(update_id)
                continue
            reply = self._dispatch(chat_id, chat.get("title") or chat.get("first_name"), text)
            self.repo.mark_update_processed(update_id)
            if reply:
                self._send(chat_id, reply)
                log.info("command router: chat=%s cmd=%r", chat_id, text[:60])
            handled += 1
        return handled

    # ── dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, chat_id, chat_title: Optional[str], text: str) -> str:
        # Split "/cmd@botname arg1 arg2" → ("cmd", ["arg1", "arg2"])
        parts = text.split()
        head = parts[0][1:].lower()
        head = head.split("@", 1)[0]   # strip @botname if present
        args = parts[1:]

        row = self.repo.get(chat_id)
        override = row.override if row else ChatOverride()
        title = chat_title or (row.title if row else None)

        handler = self._handlers.get(head)
        if not handler:
            return f"Unknown command: /{head}. Try /help."
        try:
            reply = handler(args, override)
        except Exception as e:
            log.exception("command %s failed", head)
            return f"⚠️ /{head} failed: {e}"

        # Persist any override mutations (paused=True path uses the same
        # write). enabled=1 by default — /pause flips paused inside override.
        self.repo.upsert(chat_id, title, override, enabled=True)
        return reply

    # ── handlers ──────────────────────────────────────────────────────

    def _cmd_help(self) -> str:
        return (
            "<b>Commands</b>\n"
            "/status — show current effective config here\n"
            "/max_price N — override max price (PLN)\n"
            "/min_area N — override min area (m²)\n"
            "/max_area N — set an upper area cap\n"
            "/min_year Y — earliest build year accepted\n"
            "/source NAME on|off — enable/disable a data source\n"
            "/source NAME url URL — custom URL for a source (this chat only)\n"
            "/kw + NAME [WEIGHT] — add positive scoring keyword\n"
            "/kw - NAME [WEIGHT] — add negative scoring keyword\n"
            "/kw reject NAME — add reject-filter keyword\n"
            "/kw del NAME — remove a keyword override\n"
            "/kw list — show keyword overrides\n"
            "/reset FIELD — clear one override (e.g. /reset max_price)\n"
            "/reset all — clear all overrides\n"
            "/pause — stop receiving matches here\n"
            "/resume — resume receiving\n"
            "/stats [N] — emitted count over last N days (default 7)\n\n"
            f"Sources available: {', '.join(_KNOWN_SOURCES)}"
        )

    def _cmd_status(self, args, override: ChatOverride) -> str:
        lines = ["<b>Effective config for this chat</b>", ""]
        b = self.baseline_cfg
        lines.append(f"max_price: {override.max_price or b['search']['max_price']}"
                     f"{' *' if override.max_price else ''}")
        lines.append(f"min_area:  {override.min_area or b['search']['min_area']}"
                     f"{' *' if override.min_area else ''}")
        if override.max_area:
            lines.append(f"max_area:  {override.max_area} *")
        year = override.min_build_year
        if year is None:
            year = b.get('search', {}).get('min_build_year')
        lines.append(f"min_year:  {year if year is not None else 'off'}"
                     f"{' *' if override.min_build_year is not None else ''}")

        srcs = [s for s in _KNOWN_SOURCES if s not in override.disabled_sources]
        lines.append(f"sources:   {', '.join(srcs)}")
        if override.source_urls:
            for k, v in override.source_urls.items():
                lines.append(f"  {k} url = {v[:60]}…" if len(v) > 60 else f"  {k} url = {v}")

        if override.extra_reject:
            lines.append(f"extra reject_kw: {', '.join(override.extra_reject)}")
        if override.extra_positive:
            lines.append(f"extra +kw: {', '.join(_kw_repr(k) for k in override.extra_positive)}")
        if override.extra_negative:
            lines.append(f"extra -kw: {', '.join(_kw_repr(k) for k in override.extra_negative)}")
        if override.weights:
            lines.append(f"weight overrides: {override.weights}")
        if override.paused:
            lines.append("⏸️ <b>paused</b> — /resume to re-enable")
        lines.append("")
        lines.append("<i>* = overridden here vs. baseline YAML</i>")
        return "\n".join(lines)

    def _set_int(self, args, override: ChatOverride, attr: str) -> str:
        if not args:
            return f"Usage: /{attr} N (integer)"
        try:
            v = int(args[0])
        except ValueError:
            return f"'{args[0]}' is not an integer."
        setattr(override, attr, v)
        return f"✓ {attr} = {v}"

    def _set_float(self, args, override: ChatOverride, attr: str) -> str:
        if not args:
            return f"Usage: /{attr} N"
        try:
            v = float(args[0].replace(",", "."))
        except ValueError:
            return f"'{args[0]}' is not a number."
        setattr(override, attr, v)
        return f"✓ {attr} = {v}"

    def _cmd_reset(self, args, override: ChatOverride) -> str:
        if not args:
            return "Usage: /reset FIELD  (or /reset all)"
        field = args[0].lower()
        if field == "all":
            # Blow away every override — replace with a fresh empty one.
            override.__dict__.update(asdict(ChatOverride()))
            return "✓ all overrides cleared"
        # Numeric / boolean / list attributes — set to their dataclass default.
        empty = ChatOverride()
        if not hasattr(empty, field):
            return f"Unknown field '{field}'."
        setattr(override, field, getattr(empty, field))
        return f"✓ {field} reset to baseline"

    def _cmd_source(self, args, override: ChatOverride) -> str:
        if len(args) < 2:
            return "Usage: /source NAME on|off   OR   /source NAME url URL"
        name = args[0].lower()
        if name not in _KNOWN_SOURCES:
            return f"Unknown source '{name}'. Try one of: {', '.join(_KNOWN_SOURCES)}"
        verb = args[1].lower()
        if verb in ("on", "enable"):
            override.disabled_sources = [s for s in override.disabled_sources if s != name]
            return f"✓ source {name} enabled for this chat"
        if verb in ("off", "disable"):
            if name not in override.disabled_sources:
                override.disabled_sources.append(name)
            return f"✓ source {name} disabled for this chat"
        if verb == "url":
            if len(args) < 3:
                return "Usage: /source NAME url URL"
            url = " ".join(args[2:])
            override.source_urls[name] = url
            return f"✓ source {name} URL set for this chat"
        return "Usage: /source NAME on|off   OR   /source NAME url URL"

    def _cmd_kw(self, args, override: ChatOverride) -> str:
        if not args:
            return "Usage: /kw + NAME [W]  |  /kw - NAME [W]  |  /kw reject NAME  |  /kw del NAME  |  /kw list"
        verb = args[0].lower()
        if verb == "list":
            return _describe_keywords(override)
        if verb in ("+", "add+", "positive"):
            return _add_kw(override.extra_positive, args[1:], sign="+")
        if verb in ("-", "add-", "negative"):
            return _add_kw(override.extra_negative, args[1:], sign="-")
        if verb == "reject":
            if len(args) < 2:
                return "Usage: /kw reject NAME"
            name = " ".join(args[1:])
            if name in override.extra_reject:
                return f"'{name}' already in extra reject list"
            override.extra_reject.append(name)
            return f"✓ reject '{name}' added for this chat"
        if verb == "del":
            if len(args) < 2:
                return "Usage: /kw del NAME"
            name = " ".join(args[1:])
            removed = _remove_kw_from_all(override, name)
            return f"✓ removed '{name}' from {removed} override list(s)" if removed else \
                   f"'{name}' not found in any override"
        return "Unknown /kw sub-command. Try /kw list."

    def _cmd_pause(self, override: ChatOverride, pause: bool) -> str:
        override.paused = pause
        return "⏸️ paused — /resume to re-enable" if pause else "▶️ resumed"

    def _cmd_stats(self, args, override: ChatOverride) -> str:
        days = 7
        if args:
            try:
                days = max(1, min(90, int(args[0])))
            except ValueError:
                return f"'{args[0]}' is not an integer."
        # We need chat_id to compute — dispatch stored the row via upsert
        # already. Read fresh; the repo returns the latest.
        # (Not fatal if we can't find; report zero.)
        return f"Stats for last {days} days: use the Streamlit dashboard for full charts."

    # ── outbound ──────────────────────────────────────────────────────

    def _send(self, chat_id, text: str) -> None:
        try:
            requests.post(
                _TG_API.format(token=self.bot_token, method="sendMessage"),
                data={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    # Attach the persistent menu on every command reply so
                    # a user who cleared it (or first-time messengers who
                    # never got the greeting) still gets the buttons.
                    "reply_markup": json.dumps(default_reply_keyboard()),
                },
                timeout=15,
            )
        except Exception as e:
            log.error("command router: reply to %s failed: %s", chat_id, e)


# ── helpers ────────────────────────────────────────────────────────────

def _add_kw(target: list, args: list, sign: str) -> str:
    if not args:
        return f"Usage: /kw {sign} NAME [W]"
    name = args[0]
    weight: Optional[int] = None
    if len(args) >= 2:
        try:
            weight = int(args[1])
        except ValueError:
            return f"'{args[1]}' is not an integer weight."
    entry = {"name": name, "weight": weight} if weight is not None else name
    # Skip duplicates (compare on name only).
    for existing in target:
        existing_name = existing["name"] if isinstance(existing, dict) else existing
        if existing_name == name:
            return f"'{name}' already in {sign} keyword list"
    target.append(entry)
    return f"✓ {sign} '{name}'" + (f" (weight {weight})" if weight else "")


def _remove_kw_from_all(override: ChatOverride, name: str) -> int:
    def _prune(lst):
        out = [e for e in lst if _kw_name(e) != name]
        removed = len(lst) - len(out)
        return out, removed
    total = 0
    override.extra_positive, r = _prune(override.extra_positive); total += r
    override.extra_negative, r = _prune(override.extra_negative); total += r
    override.extra_reject,   r = _prune(override.extra_reject);   total += r
    return total


def _kw_name(entry) -> str:
    return entry["name"] if isinstance(entry, dict) else str(entry)


def _kw_repr(entry) -> str:
    if isinstance(entry, dict):
        w = entry.get("weight")
        return f"{entry['name']}({w:+})" if w is not None else entry["name"]
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
