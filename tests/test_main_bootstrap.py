from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from main import _bootstrap_from_yaml_if_empty
from scanner.chat_repo import ChatConfigRepo
from scanner.runtime_config import load_yaml_config
from scanner.storage import SeenStore


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "seen.db"
        self.store = SeenStore(local_path=str(self.db_path))
        self.repo = ChatConfigRepo(self.store)
        self.log = logging.getLogger("test-bootstrap")
        self.cfg = load_yaml_config("config.example.yml")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_missing_tg_chat_id_is_allowed(self) -> None:
        self.cfg["telegram"]["chat_id"] = ""
        _bootstrap_from_yaml_if_empty(self.cfg, self.repo, self.log)
        self.assertEqual(self.repo.list_all(), [])

    def test_fallback_chat_id_bootstraps_when_no_active_chats(self) -> None:
        self.cfg["telegram"]["chat_id"] = "-123456789"
        _bootstrap_from_yaml_if_empty(self.cfg, self.repo, self.log)
        row = self.repo.get("-123456789")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(row.enabled)
        self.assertEqual(row.title, "fallback (from YAML)")
