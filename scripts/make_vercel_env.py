"""Generate a filled-in .env.vercel from the template.

Values are collected from places that already hold them on *your* machine:

* ``telegram.bot_token`` is read out of ``config.yml``
* ``TURSO_URL`` / ``TURSO_AUTH_TOKEN`` are taken from the environment if
  exported, otherwise prompted for
* ``TG_WEBHOOK_SECRET`` is generated with :mod:`secrets` unless you pass one
* ``GITHUB_WORKFLOW_TOKEN`` is prompted for (create it at
  https://github.com/settings/personal-access-tokens/new with
  ``Actions: write`` on this repo). Optional — skip it and ``/scan`` stays
  disabled while every other command works.

Secret prompts use :func:`getpass.getpass`, so nothing is echoed to the
terminal or saved into shell history.

Usage::

    poetry run python scripts/make_vercel_env.py
    poetry run python scripts/make_vercel_env.py --output .env.vercel --force

Then import the file: Vercel dashboard -> project -> Settings ->
Environment Variables -> "Import .env".
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
from getpass import getpass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / ".env.vercel.example"

# Vars whose value the template already carries — never prompt for these.
_PREFILLED = (
    "TG_WEBHOOK_ENABLED",
    "TURSO_URL",
    "GITHUB_REPOSITORY_OWNER",
    "GITHUB_REPOSITORY_NAME",
    "GITHUB_SCAN_WORKFLOW_FILE",
    "GITHUB_SCAN_WORKFLOW_REF",
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output", default=".env.vercel", help="file to write (default: .env.vercel)")
    p.add_argument("--config", default="config.yml", help="where to read telegram.bot_token from")
    p.add_argument("--webhook-secret", default=None, help="use this instead of generating one")
    p.add_argument("--force", action="store_true", help="overwrite an existing output file")
    return p


def _bot_token_from_config(path: Path) -> str | None:
    """Pull ``telegram.bot_token`` out of config.yml without importing yaml.

    A regex keeps this script dependency-free so it runs even outside the
    Poetry venv.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r'^\s*bot_token:\s*["\']?([^"\'\s#]+)', text, re.MULTILINE)
    if not m:
        return None
    token = m.group(1)
    return None if token.startswith("REPLACE") else token


def _ask_secret(label: str, *, optional: bool = False) -> str:
    suffix = " (press Enter to skip)" if optional else ""
    while True:
        value = getpass(f"  {label}{suffix}: ").strip()
        if value:
            return value
        if optional:
            return ""
        print("    required — paste the value or Ctrl-C to abort", file=sys.stderr)


def _resolve_values(args) -> dict[str, str]:
    """Collect every REPLACE_ME value, preferring sources that already have it."""
    values: dict[str, str] = {}

    # 1. Bot token — config.yml usually already has it.
    cfg_path = REPO_ROOT / args.config
    token = _bot_token_from_config(cfg_path)
    if token:
        print(f"  TG_BOT_TOKEN: taken from {args.config}")
        values["TG_BOT_TOKEN"] = token
    else:
        print(f"  TG_BOT_TOKEN: not found in {args.config}")
        values["TG_BOT_TOKEN"] = _ask_secret("TG_BOT_TOKEN (from @BotFather)")

    # 2. Webhook secret — generate unless supplied. Must match the value you
    #    pass to manage_telegram_webhook.py.
    if args.webhook_secret:
        values["TG_WEBHOOK_SECRET"] = args.webhook_secret
        print("  TG_WEBHOOK_SECRET: using the one you passed")
    else:
        values["TG_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
        print("  TG_WEBHOOK_SECRET: generated")

    # 3. Turso token — env var if exported, else prompt.
    env_turso = (os.environ.get("TURSO_AUTH_TOKEN") or "").strip()
    if env_turso:
        print("  TURSO_AUTH_TOKEN: taken from $TURSO_AUTH_TOKEN")
        values["TURSO_AUTH_TOKEN"] = env_turso
    else:
        print("  TURSO_AUTH_TOKEN: not in env")
        values["TURSO_AUTH_TOKEN"] = _ask_secret(
            "TURSO_AUTH_TOKEN (turso db tokens create krakow-real-estate --expiration none)"
        )

    # 4. GitHub token — optional; only /scan needs it.
    env_gh = (os.environ.get("GITHUB_WORKFLOW_TOKEN") or "").strip()
    if env_gh:
        print("  GITHUB_WORKFLOW_TOKEN: taken from $GITHUB_WORKFLOW_TOKEN")
        values["GITHUB_WORKFLOW_TOKEN"] = env_gh
    else:
        print("  GITHUB_WORKFLOW_TOKEN: optional — needed only for /scan")
        values["GITHUB_WORKFLOW_TOKEN"] = _ask_secret(
            "GITHUB_WORKFLOW_TOKEN (fine-grained, Actions: write)", optional=True
        )

    return values


def _render(template_text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute REPLACE_ME placeholders. Returns (text, still_unset)."""
    out_lines: list[str] = []
    unset: list[str] = []
    for line in template_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        name, _, current = stripped.partition("=")
        name = name.strip()
        if current.strip() != "REPLACE_ME":
            out_lines.append(line)  # pre-filled by the template
            continue
        value = values.get(name, "")
        if not value:
            # Leave the placeholder so Vercel import surfaces it as obviously
            # incomplete rather than silently setting an empty variable.
            unset.append(name)
            out_lines.append(line)
        else:
            out_lines.append(f"{name}={value}")
    return "\n".join(out_lines) + "\n", unset


def main() -> int:
    args = _build_parser().parse_args()

    if not TEMPLATE.exists():
        print(f"template missing: {TEMPLATE}", file=sys.stderr)
        return 2

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    if out_path.exists() and not args.force:
        print(f"{out_path} already exists — pass --force to overwrite", file=sys.stderr)
        return 2

    print(f"Collecting values for {out_path.name}:")
    values = _resolve_values(args)
    text, unset = _render(TEMPLATE.read_text(encoding="utf-8"), values)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    # Owner-only: this file holds live tokens.
    out_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print(f"\nWrote {out_path} (chmod 600, gitignored)")
    if unset:
        print(f"Still REPLACE_ME: {', '.join(unset)}")
    print(
        "\nNext:\n"
        "  1. Vercel -> project -> Settings -> Environment Variables -> Import .env\n"
        "  2. Deploy, then open https://<project>.vercel.app/api/telegram_webhook\n"
        '     -> expect {"ok": true, "service": "telegram-webhook"}\n'
        "  3. Register the webhook with the SAME TG_WEBHOOK_SECRET:\n"
        "       poetry run python scripts/manage_telegram_webhook.py set \\\n"
        '         --url "https://<project>.vercel.app/api/telegram_webhook" \\\n'
        '         --secret "<TG_WEBHOOK_SECRET from the file>" --drop-pending-updates'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
