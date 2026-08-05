"""Every command, including its error paths, must answer without blowing up.

A handler that raises is not loud: the router catches it, logs, and the user
simply never gets a reply. `/grouping` shipped with a wrong attribute name and
failed exactly that way — it looked fine in review and did nothing in the chat.

Also asserts every reply fits Telegram's 4096-char limit, because an
over-length reply is rejected by the API and lost the same silent way.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from scanner.chat_config import ChatOverride
from scanner.commands import CommandContext, CommandRouter

TELEGRAM_LIMIT = 4096
_CONFIG = Path(__file__).resolve().parent.parent / "config.yml"

#: Real usage plus the ways a user gets it wrong: missing argument, wrong
#: type, unknown name, out-of-range value.
COMMANDS = [
    "/help", "/start", "/status", "/config", "/urls", "/decision_tree",
    "/dashboard",
    "/grouping", "/grouping 5", "/grouping 1", "/grouping 99",
    "/grouping 0", "/grouping -3", "/grouping abc",
    "/max_price 500000", "/max_price abc", "/max_price",
    "/min_area 45", "/min_area abc", "/min_area",
    "/max_area 70", "/min_year 1990",
    "/source otodom off", "/source otodom on", "/source nosuch on", "/source",
    "/kw + balkon 5", "/kw - parter", "/kw reject TBS", "/kw list",
    "/kw del balkon", "/kw",
    "/reset max_price", "/reset min_group_size", "/reset nosuchfield",
    "/reset all", "/reset",
    "/pause", "/resume",
    "/stats", "/stats 30", "/stats abc",
]


class EveryCommandAnswersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = yaml.safe_load(_CONFIG.read_text())
        repo = MagicMock()
        repo.get.return_value = None
        repo.stats_last_days.return_value = {"emitted": 42}
        self.router = CommandRouter("123:TEST", repo, self.cfg)
        self.ctx = CommandContext(
            chat_id="-1", chat_title="t", chat_type="group",
            user_id=1, user_name="u", message_id=1,
        )

    def _run(self, command: str):
        parts = command.split()
        head = parts[0][1:].lower().split("@", 1)[0]
        handler = self.router._handlers.get(head)
        self.assertIsNotNone(handler, f"{command}: no handler registered")
        return handler(parts[1:], ChatOverride(), self.ctx)

    def test_every_command_returns_at_least_one_reply(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                replies = self._run(command)
                self.assertTrue(replies, "handler returned nothing to say")
                self.assertTrue(all(r.text.strip() for r in replies))

    def test_no_reply_exceeds_the_telegram_limit(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                for reply in self._run(command):
                    self.assertLess(len(reply.text), TELEGRAM_LIMIT)

    def test_help_lists_every_registered_command(self) -> None:
        """A command nobody can discover may as well not exist.

        ``help`` and its ``start`` alias are exempt — you are already reading
        the output when the question arises.
        """
        help_text = self._run("/help")[0].text
        undocumented = [
            name for name in self.router._handlers
            if name not in ("help", "start") and f"/{name}" not in help_text
        ]
        self.assertEqual([], undocumented, "missing from /help")

    def test_grouping_explains_itself_when_given_no_argument(self) -> None:
        text = self._run("/grouping")[0].text
        self.assertIn("never fewer flats", text)
        self.assertIn("/grouping 0", text)        # how to switch it off
        self.assertIn(str(self.cfg["notifications"]["min_group_size"]), text)

    def test_grouping_zero_switches_it_off(self) -> None:
        """0 is a real off switch. A big threshold is not: Kraków produces a
        104-listing location bucket, so 99 still grouped."""
        override = ChatOverride()
        replies = self.router._handlers["grouping"](["0"], override, self.ctx)
        self.assertEqual(0, override.min_group_size)
        self.assertIn("Grouping off", replies[0].text)

    def test_grouping_refuses_a_negative_threshold(self) -> None:
        self.assertIn("/grouping 0", self._run("/grouping -3")[0].text)

    def test_grouping_with_a_number_sets_the_override(self) -> None:
        override = ChatOverride()
        self.router._handlers["grouping"](["7"], override, self.ctx)
        self.assertEqual(7, override.min_group_size)


if __name__ == "__main__":
    unittest.main()
