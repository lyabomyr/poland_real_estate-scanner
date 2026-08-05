"""Vercel entrypoint for Telegram webhook delivery."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from scanner.webhook import handle_webhook_request


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - Vercel/BaseHTTPRequestHandler naming
        self._respond()

    def do_POST(self) -> None:  # noqa: N802 - Vercel/BaseHTTPRequestHandler naming
        self._respond()

    def do_PUT(self) -> None:  # noqa: N802 - Vercel/BaseHTTPRequestHandler naming
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, payload = handle_webhook_request(
            method=self.command,
            headers=self.headers,
            body=body,
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)
