from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import requests

from scanner.sources.otodom import _STATUS_MARKER, OtodomSource

BLOCK_PAGE = (
    "<html><title>403 ERROR</title>"
    "<h2>The request could not be satisfied</h2></html>"
)


def _curl_result(stdout: str):
    return subprocess.CompletedProcess(args=["curl"], returncode=0, stdout=stdout, stderr="")


class OtodomFetchTests(unittest.TestCase):
    def test_fetch_falls_back_to_curl_after_cloudfront_403(self) -> None:
        response = requests.Response()
        response.status_code = 403
        response._content = (
            b"<html><title>403 ERROR</title><h2>The request could not be satisfied</h2></html>"
        )
        error = requests.HTTPError(response=response)
        source = OtodomSource(
            url="https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow",
            user_agent="Mozilla/5.0 test",
        )

        with patch("scanner.sources.base.BaseSource.fetch", side_effect=error):
            with patch("scanner.sources.otodom.shutil.which", return_value="/usr/bin/curl"):
                with patch(
                    "scanner.sources.otodom.subprocess.run",
                    return_value=_curl_result(f"<html>ok</html>{_STATUS_MARKER}200"),
                ) as run_mock:
                    html = source.fetch(source.url)

        self.assertEqual(html, "<html>ok</html>")
        cmd = run_mock.call_args.args[0]
        self.assertIn("/usr/bin/curl", cmd[0])
        self.assertIn("--compressed", cmd)
        self.assertIn(source.url, cmd)
        # The TLS pin is what makes the fallback work at all — CloudFront 403s
        # curl's TLS 1.3 handshake. Guard it so it can't be "cleaned up" away.
        self.assertIn("--tlsv1.2", cmd)
        self.assertEqual("1.2", cmd[cmd.index("--tls-max") + 1])

    def test_curl_403_raises_instead_of_returning_the_block_page(self) -> None:
        """curl exits 0 on an HTTP 403, so the status must be checked explicitly.

        Without this, the block page reaches _parse(), yields no listings and
        looks in the logs like "Otodom had nothing new" — a silent outage.
        """
        source = OtodomSource(url="https://www.otodom.pl/", user_agent="Mozilla/5.0 test")
        with patch("scanner.sources.otodom.shutil.which", return_value="/usr/bin/curl"):
            with patch(
                "scanner.sources.otodom.subprocess.run",
                return_value=_curl_result(f"{BLOCK_PAGE}{_STATUS_MARKER}403"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    source._fetch_via_curl(source.url)
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_curl_block_page_with_http_200_still_raises(self) -> None:
        """CloudFront also serves the block page with a 200, so check the body."""
        source = OtodomSource(url="https://www.otodom.pl/", user_agent="Mozilla/5.0 test")
        with patch("scanner.sources.otodom.shutil.which", return_value="/usr/bin/curl"):
            with patch(
                "scanner.sources.otodom.subprocess.run",
                return_value=_curl_result(f"{BLOCK_PAGE}{_STATUS_MARKER}200"),
            ):
                with self.assertRaises(RuntimeError):
                    source._fetch_via_curl(source.url)

    def test_missing_status_marker_is_treated_as_failure(self) -> None:
        """No marker means curl never reported a status — don't assume success."""
        source = OtodomSource(url="https://www.otodom.pl/", user_agent="Mozilla/5.0 test")
        with patch("scanner.sources.otodom.shutil.which", return_value="/usr/bin/curl"):
            with patch(
                "scanner.sources.otodom.subprocess.run",
                return_value=_curl_result("<html>ok</html>"),
            ):
                with self.assertRaises(RuntimeError):
                    source._fetch_via_curl(source.url)

    def test_fetch_re_raises_non_cloudfront_http_errors(self) -> None:
        response = requests.Response()
        response.status_code = 500
        response._content = b"server error"
        error = requests.HTTPError(response=response)
        source = OtodomSource(url="https://www.otodom.pl/", user_agent="Mozilla/5.0 test")

        with patch("scanner.sources.base.BaseSource.fetch", side_effect=error):
            with self.assertRaises(requests.HTTPError):
                source.fetch(source.url)
