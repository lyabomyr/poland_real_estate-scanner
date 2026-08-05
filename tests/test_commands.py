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
        cfg["sources"]["otodom"]["url"] = (
            "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/x?token=super-secret"
        )
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
        cfg["sources"]["otodom"]["url"] = (
            "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/x?api_key=shh"
        )
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

    def test_bzp_is_removed_from_active_command_surface(self) -> None:
        self.assertNotIn("bzp", _KNOWN_SOURCES)
        self.assertNotIn("bzp", self.cfg["sources"])
        keyboard = json.dumps(default_reply_keyboard(), ensure_ascii=False).lower()
        self.assertNotIn("bzp", keyboard)
