"""Set, inspect, or delete the Telegram webhook for this bot."""

from __future__ import annotations

import argparse
import json
import os

import requests


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("--url", required=True)
    set_cmd.add_argument("--secret", default=os.environ.get("TG_WEBHOOK_SECRET", ""))
    set_cmd.add_argument("--drop-pending-updates", action="store_true")

    delete_cmd = sub.add_parser("delete")
    delete_cmd.add_argument("--drop-pending-updates", action="store_true")

    sub.add_parser("info")
    return parser


def main() -> int:
    args = _parser().parse_args()
    token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("TG_BOT_TOKEN is required")

    if args.command == "set":
        payload = {
            "url": args.url,
            "allowed_updates": json.dumps(["message", "channel_post", "my_chat_member"]),
            "drop_pending_updates": "true" if args.drop_pending_updates else "false",
        }
        secret = (args.secret or "").strip()
        if secret:
            payload["secret_token"] = secret
        response = requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data=payload,
            timeout=20,
        )
        response.raise_for_status()
        print(response.text)
        return 0

    if args.command == "delete":
        response = requests.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            data={
                "drop_pending_updates": "true" if args.drop_pending_updates else "false",
            },
            timeout=20,
        )
        response.raise_for_status()
        print(response.text)
        return 0

    response = requests.get(
        f"https://api.telegram.org/bot{token}/getWebhookInfo",
        timeout=20,
    )
    response.raise_for_status()
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
