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

import yaml

from scanner.chat_config import ChatOverride
from scanner.chat_repo import ChatConfigRepo
from scanner.commands import CommandRouter
from scanner.pipeline import MultiChatPipeline, build_chat_context
from scanner.sources.bzp import BzpSource
from scanner.sources.komornik import KomornikSource
from scanner.sources.morizon import MorizonSource
from scanner.sources.olx import OlxSource
from scanner.sources.otodom import OtodomSource
from scanner.storage import SeenStore
from scanner.telegram import (
    discover_chats,
    find_new_chat_memberships,
    send_greeting,
)

# Config-key → source class. Add a new source here + a block under
# ``sources:`` in config.example.yml — nothing else needs to change.
SOURCE_REGISTRY = {
    "otodom": OtodomSource,
    "olx": OlxSource,
    "morizon": MorizonSource,
    "komornik": KomornikSource,
    "bzp": BzpSource,
}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── CLI ────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kraków real-estate scanner")
    p.add_argument("--config", default="config.yml")
    p.add_argument("--dry-run", action="store_true",
                   help="don't send Telegram messages, don't persist state")
    p.add_argument("--print-chats", action="store_true",
                   help="print chats the bot has recently seen (from getUpdates) and exit")
    p.add_argument("--prune", action="store_true",
                   help="archive + delete rejected rows older than storage.prune_rejected_days")
    p.add_argument("--greet-chats", action="store_true",
                   help="announce chat_id in newly-joined chats, then exit")
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    if not Path(args.config).exists():
        print(
            f"config file not found: {args.config}\n"
            f"copy config.example.yml → config.yml and fill in your Telegram creds.",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(args.config)
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

    storage_cfg = cfg.get("storage") or {}
    with SeenStore(storage_cfg.get("db_path", "./data/seen.db")) as store:
        repo = ChatConfigRepo(store)

        # 1) Greet newly-joined chats + auto-bootstrap chat_configs rows.
        if not args.dry_run or args.greet_chats:
            _greet_new_chats(bot_token, store, repo, log)
        if args.greet_chats:
            return 0

        # 2) Process pending Telegram commands (writes chat_configs overrides).
        if not args.dry_run:
            handled = CommandRouter(bot_token, repo, cfg).process_pending()
            if handled:
                log.info("commands: processed %d update(s)", handled)

        # 3) If chat_configs is empty (fresh install), seed from telegram.chat_id.
        _bootstrap_from_yaml_if_empty(cfg, repo, log)

        # 4) Build one ChatContext per enabled chat and run the pipeline.
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
    with SeenStore(storage_cfg.get("db_path", "./data/seen.db")) as store:
        n = store.prune_rejected(older_than_days=days, export_dir=archive_dir)
    log.info("pruned %d rejected rows older than %d days (archive: %s)", n, days, archive_dir)
    return 0


def _handle_print_chats(bot_token: str, log: logging.Logger) -> int:
    if not bot_token or bot_token.startswith("REPLACE"):
        print("bot_token not set in config.yml", file=sys.stderr)
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
        if send_greeting(bot_token, cid, c["title"]):
            store.record_greeted(cid, c["title"])
            _register_chat(cid, c["title"], repo, log)
            log.info("greet: announced chat_id=%s (%s)", cid, c["title"])


def _register_chat(chat_id, title, repo: ChatConfigRepo, log: logging.Logger) -> None:
    """Register a chat + backfill emissions.

    Backfilling ``chat_emissions`` from the current ``seen`` snapshot means
    the chat's *first* scan won't dump 100+ historical apartments on it —
    users adding the bot expect fresh matches going forward, not the
    archive. Idempotent: no-op if the chat is already registered.
    """
    if repo.get(chat_id):
        return
    repo.upsert(chat_id, title, ChatOverride(), enabled=True)
    n = repo.backfill_emissions_from_seen(chat_id)
    log.info(
        "registered chat_id=%s (%s) + backfilled %d historical emissions",
        chat_id, title, n,
    )


def _bootstrap_from_yaml_if_empty(cfg: dict, repo: ChatConfigRepo, log: logging.Logger) -> None:
    """Seed ``chat_configs`` from ``telegram.chat_id`` on first run.

    Prevents "why does the scanner do nothing on a fresh DB?" — if the user
    put a chat_id in YAML but never DMed the bot (no getUpdates event),
    still route matches to it. Uses :func:`_register_chat` so the historical
    backlog gets backfilled and the first scan doesn't spam.
    """
    if repo.list_all():
        return
    chat_id = ((cfg.get("telegram") or {}).get("chat_id") or "").strip()
    if not chat_id or chat_id.startswith("REPLACE"):
        return
    _register_chat(chat_id, "bootstrap (from YAML)", repo, log)


if __name__ == "__main__":
    sys.exit(main())
