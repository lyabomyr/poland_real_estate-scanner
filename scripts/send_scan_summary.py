"""Send a concise Telegram summary after a workflow-triggered scan.

Best-effort by design: this runs as an ``if: always()`` step *after* the
scan, purely to notify the requester. A failure here (transient Telegram
error, rate limit, bot removed from the chat) must NOT fail the workflow —
otherwise a perfectly good scan shows up red in the Actions UI. Every
failure path logs to stderr and exits 0; the scan step's own exit code is
what determines the job result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--stats-json", required=True)
    parser.add_argument("--command", default="scan")
    parser.add_argument("--run-url", default="")
    return parser


def _load_stats(path: Path) -> dict:
    """Read the stats JSON the scanner wrote. Missing/corrupt → empty dict.

    The scan step may have crashed before writing it; we still want to send
    a "status: failure" notification with the run URL.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: could not read stats from {path}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _build_message(args, stats: dict, status: str) -> str:
    lines = [
        f"{args.command.capitalize()} finished.",
        f"status: {status}",
    ]
    if stats:
        for key in ("seen", "already_seen", "rejected", "matched", "cross_dup", "sent"):
            lines.append(f"{key}: {stats.get(key, 0)}")
    if args.run_url:
        lines.append(args.run_url)
    return "\n".join(lines)


def main() -> int:
    args = _build_parser().parse_args()

    bot_token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    if not bot_token or bot_token.startswith("REPLACE"):
        print("warning: TG_BOT_TOKEN missing — skipping summary", file=sys.stderr)
        return 0

    status = (os.environ.get("SCAN_JOB_STATUS") or "success").strip().lower()
    text = _build_message(args, _load_stats(Path(args.stats_json)), status)

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={
                "chat_id": args.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
        response.raise_for_status()
    except Exception as e:
        # Notification is best-effort — never fail the workflow over it.
        print(f"warning: summary send to {args.chat_id} failed: {e}", file=sys.stderr)
        return 0

    print(f"summary sent to {args.chat_id} (status={status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
