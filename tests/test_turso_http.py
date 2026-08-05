from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scanner.turso_http import (
    TursoConnection,
    TursoError,
    _decode,
    _encode,
    http_url_from_libsql,
)


class UrlTests(unittest.TestCase):
    def test_libsql_scheme_is_rewritten_to_https(self) -> None:
        self.assertEqual(
            http_url_from_libsql("libsql://db-user.turso.io"),
            "https://db-user.turso.io",
        )

    def test_websocket_schemes_are_rewritten(self) -> None:
        self.assertEqual(http_url_from_libsql("wss://db.turso.io"), "https://db.turso.io")
        self.assertEqual(http_url_from_libsql("ws://db.turso.io"), "https://db.turso.io")

    def test_http_urls_pass_through(self) -> None:
        self.assertEqual(http_url_from_libsql("https://db.turso.io"), "https://db.turso.io")

    def test_bare_host_gets_https(self) -> None:
        self.assertEqual(http_url_from_libsql("db.turso.io"), "https://db.turso.io")


class CodecTests(unittest.TestCase):
    """The API sends integers as strings, so round-tripping must coerce."""

    def test_integers_encode_as_strings_and_decode_as_int(self) -> None:
        self.assertEqual(_encode(610000), {"type": "integer", "value": "610000"})
        self.assertEqual(_decode({"type": "integer", "value": "610000"}), 610000)
        self.assertIsInstance(_decode({"type": "integer", "value": "1"}), int)

    def test_floats_round_trip(self) -> None:
        self.assertEqual(_encode(42.5), {"type": "float", "value": 42.5})
        self.assertEqual(_decode({"type": "float", "value": 42.5}), 42.5)

    def test_none_maps_to_null_both_ways(self) -> None:
        self.assertEqual(_encode(None), {"type": "null"})
        self.assertIsNone(_decode({"type": "null"}))
        self.assertIsNone(_decode({"type": "text", "value": None}))

    def test_bool_encodes_as_integer(self) -> None:
        self.assertEqual(_encode(True), {"type": "integer", "value": "1"})
        self.assertEqual(_encode(False), {"type": "integer", "value": "0"})

    def test_text_and_blob(self) -> None:
        self.assertEqual(_encode("balkon"), {"type": "text", "value": "balkon"})
        self.assertEqual(_decode({"type": "text", "value": "balkon"}), "balkon")
        self.assertEqual(_encode(b"\x00\x01")["type"], "blob")
        self.assertEqual(_decode(_encode(b"\x00\x01")), b"\x00\x01")


def _fake_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class ConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = TursoConnection("libsql://db.turso.io", "token")

    def test_select_exposes_description_and_typed_rows(self) -> None:
        payload = {
            "results": [{
                "type": "ok",
                "response": {"result": {
                    "cols": [{"name": "price"}, {"name": "title"}],
                    "rows": [[
                        {"type": "integer", "value": "500000"},
                        {"type": "text", "value": "mieszkanie"},
                    ]],
                    "affected_row_count": 0,
                }},
            }]
        }
        with patch.object(self.conn._session, "post", return_value=_fake_response(payload)):
            cur = self.conn.execute("SELECT price, title FROM seen")
        self.assertEqual([c[0] for c in cur.description], ["price", "title"])
        self.assertEqual(cur.fetchall(), [(500000, "mieszkanie")])

    def test_rowcount_comes_from_affected_row_count(self) -> None:
        """claim_update() depends on this — changes() isn't reliable over HTTP."""
        payload = {
            "results": [{
                "type": "ok",
                "response": {"result": {"cols": [], "rows": [], "affected_row_count": 1}},
            }]
        }
        with patch.object(self.conn._session, "post", return_value=_fake_response(payload)):
            cur = self.conn.execute("INSERT OR IGNORE INTO command_updates VALUES (?)", (7,))
        self.assertEqual(cur.rowcount, 1)

    def test_fetchone_drains_then_returns_none(self) -> None:
        payload = {
            "results": [{
                "type": "ok",
                "response": {"result": {
                    "cols": [{"name": "n"}],
                    "rows": [[{"type": "integer", "value": "1"}]],
                    "affected_row_count": 0,
                }},
            }]
        }
        with patch.object(self.conn._session, "post", return_value=_fake_response(payload)):
            cur = self.conn.execute("SELECT 1")
        self.assertEqual(cur.fetchone(), (1,))
        self.assertIsNone(cur.fetchone())

    def test_statement_error_raises_with_context(self) -> None:
        payload = {
            "results": [{
                "type": "error",
                "error": {"message": "no such table: nope"},
            }]
        }
        with patch.object(self.conn._session, "post", return_value=_fake_response(payload)):
            with self.assertRaises(TursoError) as ctx:
                self.conn.execute("SELECT * FROM nope")
        self.assertIn("no such table", str(ctx.exception))
        self.assertIn("SELECT * FROM nope", str(ctx.exception))

    def test_params_are_encoded_into_the_request_body(self) -> None:
        payload = {
            "results": [{
                "type": "ok",
                "response": {"result": {"cols": [], "rows": [], "affected_row_count": 0}},
            }]
        }
        with patch.object(self.conn._session, "post", return_value=_fake_response(payload)) as post:
            self.conn.execute("SELECT 1 FROM seen WHERE key = ?", ("otodom:1",))
        body = post.call_args.kwargs["json"]
        self.assertEqual(
            body["requests"][0]["stmt"]["args"],
            [{"type": "text", "value": "otodom:1"}],
        )
