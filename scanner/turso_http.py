"""Minimal Turso/libSQL client over the HTTP ``/v2/pipeline`` API.

Why HTTP instead of the native ``libsql-experimental`` driver
------------------------------------------------------------
The native driver is a compiled Rust extension. It only ships prebuilt
wheels for a fixed set of CPython versions (cp38-cp313 as of 0.0.55), and
on anything newer pip falls back to building from source — which needs
Rust + cmake and fails on hosts like Streamlit Community Cloud (it runs
CPython 3.14, so the build was attempted and died inside ``libsql-ffi``).

For a **remote** database the native driver buys us nothing: every query is
a network round trip either way. So we talk to Turso's documented HTTP API
with plain ``requests`` and drop the native dependency entirely. That makes
the project installable on any Python version, anywhere, with no compiler.

Interface
---------
:class:`TursoConnection` mimics just enough of :mod:`sqlite3` for
:class:`~scanner.storage.SeenStore` and :class:`~scanner.chat_repo.ChatConfigRepo`:
``execute()`` returning a cursor with ``description`` / ``fetchone()`` /
``fetchall()`` / ``rowcount``, plus ``commit()`` and ``close()``.

Notes / limitations:

* Each ``execute()`` is its own pipeline request, so connection-scoped SQL
  functions like ``changes()`` are **not** reliable — use the cursor's
  ``rowcount`` (fed from the API's ``affected_row_count``) instead.
* ``commit()`` is a no-op: every statement autocommits server-side. It
  exists so callers written against sqlite3 keep working unchanged.
"""

from __future__ import annotations

import base64
from typing import Any, List, Optional, Sequence, Tuple

import requests

_PIPELINE_PATH = "/v2/pipeline"


def http_url_from_libsql(url: str) -> str:
    """``libsql://host`` (or ``wss://host``) → ``https://host``."""
    for prefix in ("libsql://", "wss://", "ws://"):
        if url.startswith(prefix):
            return "https://" + url[len(prefix):]
    if url.startswith(("http://", "https://")):
        return url
    return "https://" + url


class TursoError(RuntimeError):
    """Raised when the pipeline API reports a statement-level error."""


class TursoCursor:
    """Result holder shaped like a :class:`sqlite3.Cursor`."""

    def __init__(
        self,
        rows: List[Tuple[Any, ...]],
        columns: List[str],
        rowcount: int,
    ):
        self._rows = rows
        self._pos = 0
        # sqlite3 exposes 7-tuples per column and callers only read [0].
        self.description = [(name, None, None, None, None, None, None) for name in columns] or None
        self.rowcount = rowcount

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> List[Tuple[Any, ...]]:
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class TursoConnection:
    """A thin, autocommitting connection to a Turso database over HTTP."""

    def __init__(self, url: str, auth_token: str, timeout: int = 30):
        self._endpoint = http_url_from_libsql(url).rstrip("/") + _PIPELINE_PATH
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        })
        self._timeout = timeout

    # ── sqlite3-compatible surface ─────────────────────────────────────

    def execute(self, sql: str, params: Sequence[Any] = ()) -> TursoCursor:
        stmt: dict = {"sql": sql}
        if params:
            stmt["args"] = [_encode(p) for p in params]
        payload = {"requests": [{"type": "execute", "stmt": stmt}]}

        response = self._session.post(self._endpoint, json=payload, timeout=self._timeout)
        response.raise_for_status()
        body = response.json()

        result = (body.get("results") or [{}])[0]
        if result.get("type") != "ok":
            message = ((result.get("error") or {}).get("message")) or "unknown error"
            raise TursoError(f"{message} — while executing: {sql.strip()[:120]}")

        payload_result = (result.get("response") or {}).get("result") or {}
        columns = [c.get("name") for c in (payload_result.get("cols") or [])]
        rows = [
            tuple(_decode(cell) for cell in row)
            for row in (payload_result.get("rows") or [])
        ]
        return TursoCursor(rows, columns, int(payload_result.get("affected_row_count") or 0))

    def commit(self) -> None:
        """No-op — the HTTP API autocommits each statement."""

    def close(self) -> None:
        self._session.close()


# ── value codecs ───────────────────────────────────────────────────────

def _encode(value: Any) -> dict:
    """Python value → Turso cell. Integers travel as strings (API contract)."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "value": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _decode(cell: dict) -> Any:
    """Turso cell → Python value. Integers arrive as strings, so coerce."""
    kind = cell.get("type")
    raw = cell.get("value")
    if kind == "null" or raw is None:
        return None
    if kind == "integer":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "blob":
        return base64.b64decode(raw)
    return raw
