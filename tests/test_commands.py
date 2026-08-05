from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scanner.chat_config import ChatOverride
from scanner.chat_repo import ChatConfigRepo
from scanner.commands import CommandRouter, _KNOWN_SOURCES
from scanner.runtime_config import load_yaml_config
from scanner.storage import SeenStore
from scanner.telegram import default_reply_keyboard, send_greeting


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls = []

    def dispatch_scan(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "DispatchResult",
            (),
            {"workflow_run_id": 42, "html_url": "https://github.com/example/repo/actions/runs/42"},
        )()


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "seen.db"
        self.store = SeenStore(str(self.db_path))
        self.repo = ChatConfigRepo(self.store)
        self.cfg = load_yaml_config("config.example.yml")
        self.cfg["notifications"]["dashboard_url"] = "https://scanner.streamlit.app"
        self.cfg["telegram"]["bot_token"] = "SECRET_TOKEN"
        self.cfg["telegram"]["chat_id"] = "-999"

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _update(self, text: str, *, update_id: int = 1, chat_id: str = "-1001") -> dict:
        return {
            "update_id": update_id,
            "message": {
                "message_id": 17,
                "text": text,
                "chat": {"id": chat_id, "type": "supergroup", "title": "Test chat"},
                "from": {"id": 100, "username": "alice"},
            },
        }

    def _run_command(self, text: str, *, cfg: dict | None = None, update_id: int = 1, env: dict | None = None):
        sent = []
        router = CommandRouter("TOKEN", self.repo, deepcopy(cfg or self.cfg), env=env or {})
        with patch("scanner.commands.send_message", side_effect=lambda *a, **kw: sent.append(kw) or True):
            router.process_update(self._update(text, update_id=update_id))
        return sent

    def test_greeting_attaches_persistent_keyboard(self) -> None:
        sent = []
        with patch("scanner.telegram.send_message", side_effect=lambda *a, **kw: sent.append(kw) or True):
            ok = send_greeting("TOKEN", "-200", "New group", dashboard_url="https://scanner.streamlit.app")
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        payload = sent[0]
        self.assertIn("reply_markup", payload)
        keyboard = payload["reply_markup"]
        self.assertTrue(keyboard["is_persistent"])
        buttons = json.dumps(keyboard, ensure_ascii=False)
        self.assertIn("/dashboard", buttons)
        self.assertIn("/config", buttons)
        self.assertIn("/decision_tree", buttons)
        self.assertIn("/urls", buttons)
        self.assertNotIn("bzp", buttons.lower())

    def test_dashboard_command_with_and_without_url(self) -> None:
        with_url = self._run_command("/dashboard")
        self.assertEqual(with_url[0]["text"], "📊 Dashboard: https://scanner.streamlit.app")

        cfg = deepcopy(self.cfg)
        cfg["notifications"]["dashboard_url"] = None
        without_url = self._run_command("/dashboard", cfg=cfg, update_id=2)
        self.assertIn("No dashboard URL configured yet.", without_url[0]["text"])

    def test_config_command_contains_keywords_urls_and_redacts_secrets(self) -> None:
        cfg = deepcopy(self.cfg)
        cfg["sources"]["otodom"]["url"] += "&token=super-secret"
        sent = self._run_command("/config", cfg=cfg)
        text = "\n".join(item["text"] for item in sent)
        self.assertIn("reject_keywords:", text)
        self.assertIn("TBS", text)
        self.assertIn("dashboard_url: https://scanner.streamlit.app", text)
        self.assertIn("***REDACTED***", text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("SECRET_TOKEN", text)

    def test_decision_tree_command_uses_effective_rules(self) -> None:
        sent = self._run_command("/decision_tree")
        text = "\n".join(item["text"] for item in sent)
        self.assertIn("Hard filters", text)
        self.assertIn("reject if price is known", text)
        self.assertIn("positive keywords:", text)
        self.assertIn("final score = clamp(0..100)", text)

    def test_urls_command_lists_public_urls(self) -> None:
        cfg = deepcopy(self.cfg)
        cfg["sources"]["otodom"]["url"] += "&api_key=shh"
        sent = self._run_command("/urls", cfg=cfg)
        text = "\n".join(item["text"] for item in sent)
        self.assertIn("dashboard", text)
        self.assertIn("source.otodom", text)
        self.assertIn("***REDACTED***", text)
        self.assertNotIn("shh", text)

    def test_long_config_reply_is_split_safely(self) -> None:
        override = ChatOverride(extra_reject=[f"very-long-keyword-{i:03d}" for i in range(500)])
        self.repo.upsert("-1001", "Test chat", override, enabled=True)
        sent = self._run_command("/config", update_id=4)
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(item["text"]) < 4096 for item in sent))

    def test_scan_command_dispatches_workflow(self) -> None:
        dispatcher = FakeDispatcher()
        env = {
            "TG_WORKFLOW_ALLOWED_CHAT_IDS": "-1001",
            "GITHUB_WORKFLOW_TOKEN": "token",
            "GITHUB_REPOSITORY_OWNER": "owner",
            "GITHUB_REPOSITORY_NAME": "repo",
            "GITHUB_SCAN_WORKFLOW_FILE": "scan.yml",
            "GITHUB_SCAN_WORKFLOW_REF": "main",
        }
        with patch("scanner.commands.GitHubWorkflowDispatcher.from_env", return_value=dispatcher), patch(
            "scanner.commands.get_chat_member_status",
            return_value="administrator",
        ):
            sent = self._run_command("/scan", env=env, update_id=5)
        self.assertEqual(len(dispatcher.calls), 1)
        self.assertIn("Scan queued.", sent[0]["text"])
        self.assertIn("workflow_run_id: 42", sent[0]["text"])

    def test_bzp_is_removed_from_active_command_surface(self) -> None:
        self.assertNotIn("bzp", _KNOWN_SOURCES)
        self.assertNotIn("bzp", self.cfg["sources"])
        keyboard = json.dumps(default_reply_keyboard(), ensure_ascii=False).lower()
        self.assertNotIn("bzp", keyboard)


class ScanAuthorizationTests(unittest.TestCase):
    """/scan chat gate: auto-allowlist by registration, env var as tightening."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SeenStore(str(Path(self.tempdir.name) / "seen.db"))
        self.repo = ChatConfigRepo(self.store)
        self.cfg = load_yaml_config("config.example.yml")
        self.cfg["telegram"]["bot_token"] = "TOKEN"

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _ctx(self, chat_id: str, chat_type: str = "private"):
        from scanner.commands import CommandContext

        return CommandContext(
            chat_id=chat_id, chat_title="t", chat_type=chat_type,
            user_id=1, user_name="u", message_id=1,
        )

    def _router(self, env: dict | None = None) -> CommandRouter:
        return CommandRouter("TOKEN", self.repo, deepcopy(self.cfg), env=env or {})

    def test_registered_chat_is_allowed_without_any_env_allowlist(self) -> None:
        self.repo.register_chat("-100777", "auto-registered")
        self.assertIsNone(self._router()._scan_permission_error(self._ctx("-100777")))

    def test_unregistered_chat_is_denied(self) -> None:
        error = self._router()._scan_permission_error(self._ctx("-100999"))
        self.assertIsNotNone(error)
        self.assertIn("not registered", error)

    def test_disabled_chat_is_denied(self) -> None:
        self.repo.register_chat("-100777", "auto-registered")
        self.repo.set_enabled("-100777", False)
        self.assertIsNotNone(self._router()._scan_permission_error(self._ctx("-100777")))

    def test_env_allowlist_overrides_registration(self) -> None:
        self.repo.register_chat("-100777", "auto-registered")
        router = self._router({"TG_WORKFLOW_ALLOWED_CHAT_IDS": "-100555"})
        # Registered but not listed -> denied.
        self.assertIsNotNone(router._scan_permission_error(self._ctx("-100777")))
        # Listed but never registered -> allowed (explicit beats auto).
        self.assertIsNone(router._scan_permission_error(self._ctx("-100555")))

    def test_group_admin_check_fails_closed_on_api_error(self) -> None:
        self.repo.register_chat("-100777", "auto-registered")
        router = self._router()
        with patch(
            "scanner.commands.get_chat_member_status",
            side_effect=RuntimeError("telegram down"),
        ):
            error = router._scan_permission_error(self._ctx("-100777", chat_type="supergroup"))
        self.assertIsNotNone(error)
        self.assertIn("Could not verify", error)

    def test_cooldown_blocks_after_limit_reached(self) -> None:
        router = self._router()
        limit = int(self.cfg["notifications"]["scan_max_per_window"])
        for _ in range(limit):
            self.assertIsNone(router._scan_cooldown_error())
            self.repo.record_scan_dispatch("-100777", 1)
        error = router._scan_cooldown_error()
        self.assertIsNotNone(error)
        self.assertIn("throttled", error.lower())

    def test_cooldown_disabled_when_limit_is_zero(self) -> None:
        router = self._router({"TG_SCAN_MAX_PER_WINDOW": "0"})
        for _ in range(10):
            self.repo.record_scan_dispatch("-100777", 1)
        self.assertIsNone(router._scan_cooldown_error())
