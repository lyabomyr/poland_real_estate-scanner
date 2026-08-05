"""How the notifier behaves when Telegram says "slow down".

Telegram allows ~20 messages a minute into one group, so draining a backlog
of several hundred means being rate-limited constantly. Two things must hold:
a message is never retried forever, and the run never spends its whole time
budget asleep. An undelivered listing is not lost — it stays in the backlog
and goes out next run — so giving up early beats waiting.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scanner.telegram import (
    MAX_RATE_LIMIT_WAIT_SECONDS,
    RATE_LIMIT_ATTEMPTS,
    TelegramNotifier,
)


def _response(status: int, retry_after: int | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = (
        {"parameters": {"retry_after": retry_after}} if retry_after is not None else {}
    )
    r.raise_for_status.return_value = None
    return r


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notifier = TelegramNotifier(bot_token="123:ABC", chat_id="-1")

    def _post(self, responses):
        with patch("scanner.telegram.requests.post", side_effect=responses) as post:
            with patch("scanner.telegram.time.sleep") as sleep:
                ok = self.notifier._post("body", preview=False, tag="t")
        return ok, post.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_a_short_backoff_is_waited_out_and_the_message_lands(self) -> None:
        ok, calls, slept = self._post([_response(429, 3), _response(200)])
        self.assertTrue(ok)
        self.assertEqual(2, calls)
        self.assertEqual([4], slept, "sleeps retry_after + 1s of margin")

    def test_a_long_backoff_is_not_waited_out(self) -> None:
        """Sleeping a minute per message delivers less than moving on."""
        ok, calls, slept = self._post([_response(429, MAX_RATE_LIMIT_WAIT_SECONDS + 1)])
        self.assertFalse(ok, "should give up, leaving it in the backlog")
        self.assertEqual(1, calls)
        self.assertEqual([], slept, "must not sleep at all")

    def test_one_message_cannot_monopolise_the_run(self) -> None:
        """Persistent 429s stop at the attempt cap instead of looping."""
        ok, calls, _ = self._post([_response(429, 1)] * 10)
        self.assertFalse(ok)
        self.assertEqual(RATE_LIMIT_ATTEMPTS, calls)

    def test_a_missing_retry_after_still_backs_off(self) -> None:
        """Telegram usually sends it; assume a short wait when it doesn't."""
        ok, calls, slept = self._post([_response(429), _response(200)])
        self.assertTrue(ok)
        self.assertEqual([6], slept, "default 5s + 1s margin")

    def test_a_normal_send_never_sleeps(self) -> None:
        """There is no fixed pacing delay — throughput is Telegram's call."""
        ok, calls, slept = self._post([_response(200)])
        self.assertTrue(ok)
        self.assertEqual(1, calls)
        self.assertEqual([], slept)

    def test_an_unconfigured_notifier_reports_failure_loudly(self) -> None:
        """Silently skipping every send looks identical to "nothing matched"."""
        notifier = TelegramNotifier(bot_token="REPLACE_WITH_BOT_TOKEN", chat_id="-1")
        with self.assertLogs("scanner.telegram", level="WARNING"):
            self.assertFalse(notifier._post("body", preview=False, tag="t"))


if __name__ == "__main__":
    unittest.main()
