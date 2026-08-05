from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scanner.webhook import handle_webhook_request


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "seen.db"
        self.env = {
            "TG_BOT_TOKEN": "TOKEN",
            "TG_WEBHOOK_SECRET": "top-secret",
            "TG_WORKFLOW_ALLOWED_CHAT_IDS": "-1001",
            "TG_WEBHOOK_ENABLED": "true",
            "SCANNER_DB_PATH": str(self.db_path),
            "TURSO_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _headers(self, secret: str) -> dict[str, str]:
        return {"X-Telegram-Bot-Api-Secret-Token": secret}

    def _body(self, text: str = "/help", update_id: int = 1) -> bytes:
        return (
            "{"
            f"\"update_id\": {update_id},"
            "\"message\": {"
            "\"message_id\": 10,"
            f"\"text\": \"{text}\","
            "\"chat\": {\"id\": -1001, \"type\": \"supergroup\", \"title\": \"Webhook chat\"},"
            "\"from\": {\"id\": 100, \"username\": \"alice\"}"
            "}"
            "}"
        ).encode("utf-8")

    def test_webhook_secret_validation(self) -> None:
        status, _, _ = handle_webhook_request(
            method="POST",
            headers=self._headers("wrong"),
            body=self._body(),
            env=self.env,
        )
        self.assertEqual(status, 403)

    def test_duplicate_update_is_ignored(self) -> None:
        sent = []
        with patch("scanner.commands.send_message", side_effect=lambda *a, **kw: sent.append(kw) or True):
            first = handle_webhook_request(
                method="POST",
                headers=self._headers("top-secret"),
                body=self._body(update_id=33),
                env={**self.env, "TG_CHAT_ID": "", "DASHBOARD_URL": ""},
            )
            second = handle_webhook_request(
                method="POST",
                headers=self._headers("top-secret"),
                body=self._body(update_id=33),
                env={**self.env, "TG_CHAT_ID": "", "DASHBOARD_URL": ""},
            )
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(sent), 1)

    def test_webhook_processes_scan_dispatch_command(self) -> None:
        dispatcher = type(
            "Dispatcher",
            (),
            {
                "dispatch_scan": lambda self, **kwargs: type(
                    "Result", (), {"workflow_run_id": 77, "html_url": "https://github.com/example/repo/actions/runs/77"}
                )()
            },
        )()
        sent = []
        env = {
            **self.env,
            "TG_CHAT_ID": "",
            "DASHBOARD_URL": "https://scanner.streamlit.app",
            "GITHUB_WORKFLOW_TOKEN": "token",
            "GITHUB_REPOSITORY_OWNER": "owner",
            "GITHUB_REPOSITORY_NAME": "repo",
            "GITHUB_SCAN_WORKFLOW_FILE": "scan.yml",
            "GITHUB_SCAN_WORKFLOW_REF": "main",
        }
        with patch("scanner.commands.GitHubWorkflowDispatcher.from_env", return_value=dispatcher), patch(
            "scanner.commands.get_chat_member_status",
            return_value="administrator",
        ), patch("scanner.commands.send_message", side_effect=lambda *a, **kw: sent.append(kw) or True):
            status, _, _ = handle_webhook_request(
                method="POST",
                headers=self._headers("top-secret"),
                body=self._body(text="/scan", update_id=44),
                env=env,
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(sent), 1)
        self.assertIn("Scan queued.", sent[0]["text"])
