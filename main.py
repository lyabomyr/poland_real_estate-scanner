"""Kraków real-estate scanner — CLI entrypoint.

The pipeline itself is multi-tenant: one run scans every enabled chat's
configuration (see :mod:`scanner.pipeline`). This module just wires it up.

CLI shape::

    python main.py                 # scan every enabled chat
    python main.py --dry-run       # scan + print, no persistence, no Telegram
    python main.py --prune         # archive + delete old rejected rows and exit
    python main.py --print-chats   # list chats the bot has recently seen
    python main.py --greet-chats   # announce chat_id in newly-joined chats and exit
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from scanner.chat_repo import ChatConfigRepo
from scanner.env import load_dotenv
from scanner.commands import CommandRouter
from scanner.pipeline import MultiChatPipeline, build_chat_context
from scanner.registry import SOURCE_REGISTRY
from scanner.runtime_config import load_runtime_config
from scanner.storage import MissingCredentialsError, SeenStore
from scanner.telegram import (
    chat_dashboard_url,
    default_reply_keyboard,
    discover_chats,
    find_new_chat_memberships,
    get_chat_title,
    pin_message,
    send_greeting,
    send_message_returning_id,
)



# ── CLI ────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kraków real-estate scanner")
    # One tracked config file for every environment. There used to be two —
    # a gitignored local config.yml and a deployed config.example.yml — and
    # they silently drifted: local runs hard-rejected "z lat 60", pinned
    # every source URL (so changing `city` did nothing) and enabled a source
    # that no longer exists. Secrets come from the environment, so a private
    # copy buys nothing and costs correctness.
    p.add_argument("--config", default="config.yml",
                   help="YAML baseline config (default: config.yml)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't send Telegram messages, don't persist state")
    p.add_argument("--print-chats", action="store_true",
                   help="print chats the bot has recently seen (from getUpdates) and exit")
    p.add_argument("--prune", action="store_true",
                   help="archive + delete rejected rows older than storage.prune_rejected_days")
    p.add_argument("--greet-chats", action="store_true",
                   help="announce chat_id in newly-joined chats, then exit")
    p.add_argument("--pin-dashboard", action="store_true",
                   help="re-post and pin the dashboard link in every chat, then exit")
    return p


def main() -> int:
    # Before anything touches TURSO_* / TG_* — real env vars still win.
    load_dotenv()
    args = _build_arg_parser().parse_args()

    if not Path(args.config).exists():
        print(
            f"config file not found: {args.config}\n"
            f"run from the repo root, or pass --config <path>.",
            file=sys.stderr,
        )
        return 2

    cfg = load_runtime_config(args.config)
    logging.basicConfig(
        level=(cfg.get("logging") or {}).get("level", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("main")

    tg_cfg = cfg.get("telegram") or {}
    bot_token = tg_cfg.get("bot_token", "")

    if args.print_chats:
        return _handle_print_chats(bot_token, log)
    if args.prune:
        return _handle_prune(cfg, log)

    try:
        store_cm = SeenStore()
    except MissingCredentialsError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2
    with store_cm as store:
        repo = ChatConfigRepo(store)

        # 1) Greet newly-joined chats + auto-register them as scan targets.
        dashboard_url: Optional[str] = (
            (cfg.get("notifications") or {}).get("dashboard_url") or None
        )
        if not args.dry_run or args.greet_chats:
            _greet_new_chats(bot_token, store, repo, log, dashboard_url=dashboard_url)
        if args.greet_chats:
            return 0

        # 2) Drain pending Telegram commands. This is the *only* place commands
        #    are read, which is why a reply can take up to one scan interval
        #    (15 min on the default cron) — see scanner.commands.
        if not args.dry_run:
            handled = CommandRouter(bot_token, repo, cfg).process_pending()
            if handled:
                log.info("commands: processed %d update(s)", handled)

        # 3) If chat_configs is empty (fresh install), seed from telegram.chat_id.
        _bootstrap_from_yaml_if_empty(cfg, repo, log)

        # 4) Build one ChatContext per enabled chat and run the pipeline.
        _resolve_placeholder_titles(bot_token, repo, log)

        # Self-healing: any chat still missing the link gets it now, including
        # ones registered before DASHBOARD_URL was set.
        if not args.dry_run or args.pin_dashboard:
            n = _sync_dashboard_pins(
                bot_token, store, repo, dashboard_url, log,
                force=args.pin_dashboard,
            )
            if n:
                log.info("dashboard link delivered to %d chat(s)", n)
        if args.pin_dashboard:
            return 0

        chats = [c for c in repo.list_enabled() if not c.override.paused]
        if not chats:
            log.info("no active chats — set telegram.chat_id in config.yml or add the bot to a group")
            return 0

        contexts = [
            build_chat_context(row, cfg, bot_token, SOURCE_REGISTRY) for row in chats
        ]
        stats = MultiChatPipeline(contexts, store, repo, dry_run=args.dry_run).run()

    log.info("done: %s", stats.as_dict())
    return 0


# ── one-shot CLI handlers ──────────────────────────────────────────────

def _handle_prune(cfg: dict, log: logging.Logger) -> int:
    storage_cfg = cfg.get("storage") or {}
    days = int(storage_cfg.get("prune_rejected_days", 90))
    archive_dir = Path(storage_cfg.get("archive_dir", "./datasets"))
    with SeenStore() as store:
        n = store.prune_rejected(older_than_days=days, export_dir=archive_dir)
    log.info("pruned %d rejected rows older than %d days (archive: %s)", n, days, archive_dir)
    return 0


def _handle_print_chats(bot_token: str, log: logging.Logger) -> int:
    if not bot_token or bot_token.startswith("REPLACE"):
        print("bot_token not set — export TG_BOT_TOKEN", file=sys.stderr)
        return 2
    try:
        chats = discover_chats(bot_token)
    except Exception as e:
        log.warning("chat auto-discover failed: %s", e)
        chats = []
    if not chats:
        print(
            "no chats found. Send any message to the bot in a direct chat, "
            "or add the bot to a group and post there, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"{'ID':<16}  {'TYPE':<10}  TITLE")
    for c in chats:
        print(f"{c['id']:<16}  {c['type']:<10}  {c['title']}")
    return 0


# ── helpers ────────────────────────────────────────────────────────────

def _greet_new_chats(
    bot_token: str,
    store: SeenStore,
    repo: ChatConfigRepo,
    log: logging.Logger,
    dashboard_url: Optional[str] = None,
) -> None:
    """Post chat_id back to freshly-joined chats + register them in ``chat_configs``.

    Two side-effects intentionally live together:

    * :meth:`SeenStore.record_greeted` — one greeting per chat, ever.
    * :meth:`ChatConfigRepo.upsert` — makes new groups automatically appear
      as enabled scan targets. No manual "add chat" step needed.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return
    try:
        chats = find_new_chat_memberships(bot_token)
    except Exception as e:
        log.warning("greet: getUpdates failed: %s", e)
        return
    for c in chats:
        cid = c["id"]
        if store.is_greeted(cid):
            continue
        if send_greeting(bot_token, cid, c["title"], dashboard_url=dashboard_url):
            store.record_greeted(cid, c["title"])
            _register_chat(cid, c["title"], repo, log)
            log.info("greet: announced chat_id=%s (%s)", cid, c["title"])


def _sync_dashboard_pins(
    bot_token: str,
    store: SeenStore,
    repo: ChatConfigRepo,
    dashboard_url: Optional[str],
    log: logging.Logger,
    force: bool = False,
) -> int:
    """Post + pin the dashboard link in every chat that doesn't have it yet.

    Runs on every scan rather than only when a chat is first greeted. That
    matters: chats registered before ``DASHBOARD_URL`` was configured would
    otherwise never get a link, because greeting happens exactly once. Keying
    off the pinned URL also means changing DASHBOARD_URL re-pins automatically.

    Best-effort throughout — no dashboard configured, or the bot not being an
    admin, just skips. Returns how many chats were pinned.
    """
    if not dashboard_url or not bot_token or bot_token.startswith("REPLACE"):
        return 0

    pinned = 0
    for chat in repo.list_enabled():
        url = chat_dashboard_url(dashboard_url, chat.chat_id)
        if not url:
            continue
        if not force and store.dashboard_pinned_url(chat.chat_id) == url:
            continue
        text = (
            "⚙️ <b>Settings for this chat</b>\n"
            f'<a href="{url}">Open the dashboard</a>\n\n'
            "Price, area, city, sources and keywords — change them there and "
            "the next scan picks it up. Commands work too: /help"
        )
        message_id = send_message_returning_id(
            bot_token, chat.chat_id, text=text, parse_mode="HTML",
            reply_markup=default_reply_keyboard(),
        )
        if not message_id:
            continue
        # Record even when pinning is refused (bot isn't admin) — the message
        # itself was delivered, and retrying every 15 min would be spam.
        if pin_message(bot_token, chat.chat_id, message_id):
            log.info("pinned dashboard link in chat %s", chat.chat_id)
        else:
            log.info(
                "posted dashboard link in chat %s (pin needs admin rights)",
                chat.chat_id,
            )
        store.record_dashboard_pin(chat.chat_id, url)
        pinned += 1
    return pinned


def _register_chat(chat_id, title, repo: ChatConfigRepo, log: logging.Logger) -> None:
    """Register a chat + backfill emissions.

    Backfilling ``chat_emissions`` from the current ``seen`` snapshot means
    the chat's *first* scan won't dump 100+ historical apartments on it —
    users adding the bot expect fresh matches going forward, not the
    archive. Idempotent: no-op if the chat is already registered.
    """
    n = repo.register_chat(chat_id, title)
    if n:
        log.info(
            "registered chat_id=%s (%s) + backfilled %d historical emissions",
            chat_id, title, n,
        )


# Titles we generate ourselves rather than read from Telegram. Worth one
# getChat call each so the dashboard shows the real chat name.
_PLACEHOLDER_TITLES = ("fallback (from YAML)", "bootstrap (from YAML)", "(no title)", "")


def _resolve_placeholder_titles(
    bot_token: str,
    repo: ChatConfigRepo,
    log: logging.Logger,
) -> None:
    """Replace self-generated chat titles with the real ones from Telegram.

    Only touches rows whose title is a known placeholder, so it costs at most
    one ``getChat`` per such chat and then never again.
    """
    if not bot_token or bot_token.startswith("REPLACE"):
        return
    for chat in repo.list_all():
        if (chat.title or "") not in _PLACEHOLDER_TITLES:
            continue
        title = get_chat_title(bot_token, chat.chat_id)
        if title and title != chat.title:
            repo.upsert(chat.chat_id, title, chat.override, enabled=chat.enabled)
            log.info("resolved title for chat %s: %r", chat.chat_id, title)


def _bootstrap_from_yaml_if_empty(cfg: dict, repo: ChatConfigRepo, log: logging.Logger) -> None:
    """Guarantee at least one destination.

    Priority order:

    1. If **any** row in ``chat_configs`` is ``enabled=1`` and not ``paused`` —
       do nothing. Multi-tenant setup wins.
    2. Otherwise fall back to ``telegram.chat_id`` from YAML (which comes
       from ``TG_CHAT_ID`` in the workflow secrets). Semantics:

       * Row missing → register it and backfill emissions (so no spam).
       * Row exists but ``enabled=0`` or ``paused`` → re-enable + un-pause
         so the fallback actually receives matches.

    This keeps the pre-multi-tenant behaviour available: even if every
    per-chat row is disabled, the "canonical" chat from secrets still gets
    the notifications.
    """
    active = [
        c for c in repo.list_all() if c.enabled and not c.override.paused
    ]
    if active:
        return

    chat_id = ((cfg.get("telegram") or {}).get("chat_id") or "").strip()
    if not chat_id or chat_id.startswith("REPLACE"):
        return

    row = repo.get(chat_id)
    if row is None:
        _register_chat(chat_id, "fallback (from YAML)", repo, log)
        return

    # Row exists but nobody's listening — nudge it back on.
    override = row.override
    changed = []
    if override.paused:
        override.paused = False
        changed.append("un-paused")
    repo.upsert(chat_id, row.title, override, enabled=True)
    if not row.enabled:
        changed.append("re-enabled")
    log.info(
        "fallback: chat_id=%s reactivated from YAML (%s)",
        chat_id, ", ".join(changed) or "already active",
    )


if __name__ == "__main__":
    sys.exit(main())
