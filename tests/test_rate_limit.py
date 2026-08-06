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
    MAX_SEND_INTERVAL_SECONDS,
    RATE_LIMIT_ATTEMPTS,
    TelegramNotifier,
)


def _response(status: int, retry_after: int | None = None):
    """A stand-in that behaves like requests does — including raising."""
    import requests

    r = MagicMock()
    r.status_code = status
    r.json.return_value = (
        {"parameters": {"retry_after": retry_after}} if retry_after is not None else {}
    )
    if status >= 400 and status != 429:
        r.raise_for_status.side_effect = requests.HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        # send_interval=0: these tests assert 429 behaviour, not pacing.
        self.notifier = TelegramNotifier(
            bot_token="123:ABC", chat_id="-1", send_interval=0)

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
        notifier = TelegramNotifier(
            bot_token="REPLACE_WITH_BOT_TOKEN", chat_id="-1", send_interval=0)
        with self.assertLogs("scanner.telegram", level="WARNING"):
            self.assertFalse(notifier._post("body", preview=False, tag="t"))


class PacingTests(unittest.TestCase):
    """3 s between messages = 20/min, Telegram's documented group ceiling.

    Staying under it beats discovering it: without pacing, 2.4% of messages
    hit a 429 and paid a 30-44 s stall.
    """

    def _notifier(self, interval=3.0):
        return TelegramNotifier(bot_token="123:ABC", chat_id="-1", send_interval=interval)

    def _send_two(self, notifier, responses):
        with patch("scanner.telegram.requests.post", side_effect=responses):
            with patch("scanner.telegram.time.sleep") as sleep:
                with patch("scanner.telegram.time.monotonic", side_effect=[100.0, 101.0, 101.0]):
                    notifier._post("a", preview=False, tag="a")
                    notifier._post("b", preview=False, tag="b")
        return [c.args[0] for c in sleep.call_args_list]

    def test_the_first_message_is_not_delayed(self) -> None:
        """Nothing to pace against yet — a lone alert must go out at once."""
        with patch("scanner.telegram.requests.post", return_value=_response(200)):
            with patch("scanner.telegram.time.sleep") as sleep:
                self._notifier()._post("body", preview=False, tag="t")
        self.assertEqual([], sleep.call_args_list)

    def test_the_second_message_waits_out_the_interval(self) -> None:
        slept = self._send_two(self._notifier(3.0), [_response(200), _response(200)])
        self.assertEqual([2.0], slept, "1s elapsed of a 3s interval -> wait 2s")

    def test_pacing_can_be_switched_off(self) -> None:
        slept = self._send_two(self._notifier(0), [_response(200), _response(200)])
        self.assertEqual([], slept)

    def test_a_429_slows_the_pace_for_the_rest_of_the_run(self) -> None:
        """Telegram refused, so returning to the same rate just gets refused."""
        notifier = self._notifier(3.0)
        with patch("scanner.telegram.requests.post",
                   side_effect=[_response(429, 2), _response(200)]):
            with patch("scanner.telegram.time.sleep"):
                notifier._post("body", preview=False, tag="t")
        self.assertEqual(6.0, notifier._interval)

    def test_the_backoff_is_capped(self) -> None:
        notifier = self._notifier(3.0)
        for _ in range(10):
            with patch("scanner.telegram.requests.post",
                       side_effect=[_response(429, 1), _response(200)]):
                with patch("scanner.telegram.time.sleep"):
                    notifier._post("body", preview=False, tag="t")
        self.assertEqual(MAX_SEND_INTERVAL_SECONDS, notifier._interval)

    def test_a_failed_send_does_not_start_the_clock(self) -> None:
        """Otherwise a rejected message would delay the next real one."""
        notifier = self._notifier(3.0)
        with patch("scanner.telegram.requests.post", return_value=_response(500)):
            with patch("scanner.telegram.time.sleep"):
                notifier._post("body", preview=False, tag="t")
        self.assertIsNone(notifier._last_sent)


if __name__ == "__main__":
    unittest.main()
