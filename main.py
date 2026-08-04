import argparse
import logging
import sys
from pathlib import Path
from typing import List

import yaml

from scanner.filters import ListingFilter
from scanner.format import format_plain
from scanner.sources.base import BaseSource
from scanner.sources.bzp import BzpSource
from scanner.sources.komornik import KomornikSource
from scanner.sources.listaprzetargow import ListaPrzetargowSource
from scanner.sources.morizon import MorizonSource
from scanner.sources.olx import OlxSource
from scanner.sources.otodom import OtodomSource
from scanner.storage import SeenStore
from scanner.telegram import TelegramNotifier, discover_chats

SOURCE_REGISTRY = {
    "otodom": OtodomSource,
    "olx": OlxSource,
    "morizon": MorizonSource,
    "listaprzetargow": ListaPrzetargowSource,
    "bzp": BzpSource,
    "komornik": KomornikSource,
}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_sources(cfg: dict) -> List[BaseSource]:
    http = cfg.get("http") or {}
    common = {
        "user_agent": http.get("user_agent", ""),
        "timeout": http.get("timeout", 30),
        "delay": http.get("delay_seconds", 2),
    }
    sources: List[BaseSource] = []
    for key, sconf in (cfg.get("sources") or {}).items():
        if not sconf or not sconf.get("enabled", True):
            continue
        cls = SOURCE_REGISTRY.get(key)
        if not cls:
            logging.warning("unknown source in config: %s", key)
            continue
        params = {k: v for k, v in sconf.items() if k != "enabled"}
        params.update(common)
        sources.append(cls(**params))
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description="Kraków real-estate scanner")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't send Telegram messages and don't persist seen state",
    )
    parser.add_argument(
        "--print-chats",
        action="store_true",
        help="print chats the bot has recently seen (from getUpdates) and exit",
    )
    args = parser.parse_args()

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

    search = cfg["search"]
    flt = ListingFilter(
        min_area=search["min_area"],
        max_price=search["max_price"],
        min_build_year=search.get("min_build_year"),
        reject_keywords=(cfg.get("filters") or {}).get("reject_keywords", []),
    )
    tg_cfg = cfg.get("telegram") or {}
    bot_token = tg_cfg.get("bot_token", "")
    chat_id = tg_cfg.get("chat_id", "")

    if args.print_chats:
        return _print_chats(bot_token, log)

    # auto-discover chat_id if the config leaves it empty / placeholder
    if bot_token and not bot_token.startswith("REPLACE") and (
        not chat_id or chat_id.startswith("REPLACE")
    ):
        chats = _try_discover(bot_token, log)
        if len(chats) == 1:
            chat_id = str(chats[0]["id"])
            log.info("auto-discovered chat_id=%s (%s)", chat_id, chats[0]["title"])
        elif len(chats) > 1:
            log.error("multiple chats found — pin one in config.telegram.chat_id:")
            for c in chats:
                log.error("  %-16s  %-10s  %s", c["id"], c["type"], c["title"])
        else:
            log.warning(
                "no chats found via getUpdates — send /start to the bot in a "
                "direct chat, or add it to a group and post any message, then retry."
            )

    store = SeenStore((cfg.get("storage") or {}).get("db_path", "./data/seen.db"))
    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        parse_mode=tg_cfg.get("parse_mode", "HTML"),
    )
    sources = build_sources(cfg)

    if not notifier.is_configured():
        log.info("telegram not configured — matches will be printed to console only")

    stats = {"seen": 0, "already_seen": 0, "rejected": 0, "matched": 0, "sent": 0}
    for src in sources:
        log.info("=== scanning %s ===", src.name)
        try:
            for listing in src.scan():
                stats["seen"] += 1
                if store.has(listing.dedup_key):
                    stats["already_seen"] += 1
                    continue
                ok, reason = flt.accepts(listing)
                if not ok:
                    log.debug("reject %s: %s (%s)", listing.url, reason, listing.title)
                    stats["rejected"] += 1
                    if not args.dry_run:
                        store.add(listing, status="rejected", reject_reason=reason)
                    continue
                stats["matched"] += 1
                print(format_plain(listing))
                print("-" * 60)
                if not args.dry_run:
                    if notifier.send(listing):
                        stats["sent"] += 1
                    store.add(listing, status="matched")
        except Exception:
            log.exception("source %s crashed", src.name)

    store.close()
    log.info("done: %s", stats)
    return 0


def _try_discover(bot_token: str, log: logging.Logger) -> list:
    try:
        return discover_chats(bot_token)
    except Exception as e:
        log.warning("chat auto-discover failed: %s", e)
        return []


def _print_chats(bot_token: str, log: logging.Logger) -> int:
    if not bot_token or bot_token.startswith("REPLACE"):
        print("bot_token not set in config.yml", file=sys.stderr)
        return 2
    chats = _try_discover(bot_token, log)
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


if __name__ == "__main__":
    sys.exit(main())
