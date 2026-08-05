"""SQLite / Turso-backed dedup + archive store, keyed by ``<source>:<id>``.

Backend selection
-----------------
Set ``TURSO_URL`` **and** ``TURSO_AUTH_TOKEN`` in the environment to use
Turso (hosted libSQL). If either is missing we fall back to a local SQLite
file at ``storage.db_path``. Local dev typically leaves both unset so it
doesn't burn cloud reads while iterating.

Design notes
------------
* One row per distinct listing in ``seen``. No history — a listing only ever
  transitions "unseen → seen".
* We store matched, rejected AND cross-source-duplicate rows. Matched +
  duplicate are kept forever (that's the ML dataset); rejected can be pruned
  after N days by :meth:`prune_rejected` — CSV-archived first.
* ``fuzzy_key`` column powers cross-source dedup (Otodom+Morizon listings of
  the same physical apartment share a key — see :mod:`scanner.models`).
* ``INSERT OR IGNORE`` makes ``add()`` idempotent — safe to call twice.
* libSQL is wire-compatible with the sqlite3 dialect. We split the schema
  into individual statements (no ``executescript``) because remote libSQL
  doesn't support multi-statement execute.
"""

import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Set

from .models import Listing


def _connect(path: str):
    """Return a DB connection: Turso over HTTP if creds in env, else local sqlite3.

    We deliberately use Turso's HTTP API rather than the native
    ``libsql-experimental`` driver — see :mod:`scanner.turso_http` for why
    (compiled Rust extension, no wheels for newer CPython, breaks hosted
    deploys). For a remote DB it's the same network round trip anyway.
    """
    url = os.environ.get("TURSO_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if url and token:
        # Imported lazily so local-SQLite users don't pay for it.
        from .turso_http import TursoConnection
        return TursoConnection(url, token)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


# Individual statements so this works on both sqlite3 (batches fine but we
# don't rely on it) and libSQL remote (rejects multi-statement scripts).
_SCHEMA_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS seen (
        key           TEXT PRIMARY KEY,       -- "<source>:<id>"
        source        TEXT NOT NULL,
        listing_id    TEXT NOT NULL,
        url           TEXT,
        title         TEXT,
        price         INTEGER,
        area          REAL,
        status        TEXT NOT NULL,          -- 'matched' | 'rejected' | 'duplicate'
        reject_reason TEXT,                   -- populated only for status='rejected'
        fuzzy_key     TEXT,                   -- cross-source dedup — see models.py
        score         INTEGER,                -- 0-100 DealScore, written after scoring
        score_reasons TEXT,                   -- comma-joined reason tags for the score
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS seen_source_idx ON seen(source)",
    "CREATE INDEX IF NOT EXISTS seen_status_idx ON seen(status)",
    # NOTE: indexes on fuzzy_key / score live in SeenStore._migrate(), not
    # here. CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so
    # a column added in a later version only appears after the ALTER in
    # _migrate() — creating its index at this point would fail with
    # "no such column" on any pre-existing DB.
    # Which chats the bot has already announced its chat_id to. We use this
    # to avoid re-sending the "Hi, your chat_id is X" message every 15 min
    # for the rest of the update's 24h retention window.
    """
    CREATE TABLE IF NOT EXISTS greeted_chats (
        chat_id          TEXT PRIMARY KEY,
        title            TEXT,
        first_greeted_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Per-chat configuration overrides. Every enabled chat receives matches
    # filtered/scored/routed through its own effective config. See
    # scanner.chat_config for the merge semantics.
    """
    CREATE TABLE IF NOT EXISTS chat_configs (
        chat_id    TEXT PRIMARY KEY,
        title      TEXT,
        enabled    INTEGER NOT NULL DEFAULT 1,
        config     TEXT NOT NULL DEFAULT '{}',   -- JSON blob (ChatOverride)
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS chat_configs_enabled_idx ON chat_configs(enabled)",
    # Per-chat emission tracking — "have we already sent listing L to chat C?"
    # This is the multi-tenant analogue of ``seen``: seen tracks whether we
    # ever saw a listing, chat_emissions tracks whether each individual chat
    # was notified about it.
    """
    CREATE TABLE IF NOT EXISTS chat_emissions (
        chat_id       TEXT NOT NULL,
        listing_key   TEXT NOT NULL,
        emitted_price INTEGER,   -- price at the moment we notified this chat
        sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (chat_id, listing_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS chat_emissions_chat_idx ON chat_emissions(chat_id)",
    # Processed Telegram update ids (webhook or polling fallback). Storing
    # them here makes command + greeting handling idempotent across retries
    # and scanner restarts.
    """
    CREATE TABLE IF NOT EXISTS price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_key TEXT NOT NULL,
        old_price   INTEGER,
        new_price   INTEGER,
        changed_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS price_history_key_idx ON price_history(listing_key)",
    """
    CREATE TABLE IF NOT EXISTS command_updates (
        update_id    INTEGER PRIMARY KEY,
        processed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
]


class SeenStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.conn = _connect(str(self.path))
        for stmt in _SCHEMA_STMTS:
            self.conn.execute(stmt)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns/indexes missing on databases created by an earlier version.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` doesn't retroactively add
        columns to an existing table, so we introspect and ``ALTER TABLE``.

        We call ``fetchall()`` rather than iterating the cursor — libSQL's
        ``Cursor`` isn't iterable while ``sqlite3.Cursor`` is; ``fetchall``
        works on both.
        """
        rows = self.conn.execute("PRAGMA table_info(seen)").fetchall()
        existing_cols = {r[1] for r in rows}
        for column, ddl in (
            ("fuzzy_key", "ALTER TABLE seen ADD COLUMN fuzzy_key TEXT"),
            ("score", "ALTER TABLE seen ADD COLUMN score INTEGER"),
            ("score_reasons", "ALTER TABLE seen ADD COLUMN score_reasons TEXT"),
        ):
            if column not in existing_cols:
                self.conn.execute(ddl)

        # Safe on both fresh and migrated databases: the columns above are
        # guaranteed to exist by this point.
        self.conn.execute("CREATE INDEX IF NOT EXISTS seen_fuzzy_idx ON seen(fuzzy_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS seen_score_idx ON seen(score)")

        emission_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(chat_emissions)").fetchall()
        }
        if "emitted_price" not in emission_cols:
            self.conn.execute("ALTER TABLE chat_emissions ADD COLUMN emitted_price INTEGER")

    def has(self, key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE key = ? LIMIT 1", (key,))
        return cur.fetchone() is not None

    # ── greeted_chats ──────────────────────────────────────────────────

    def is_greeted(self, chat_id) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM greeted_chats WHERE chat_id = ? LIMIT 1", (str(chat_id),)
        )
        return cur.fetchone() is not None

    def record_greeted(self, chat_id, title: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO greeted_chats (chat_id, title) VALUES (?, ?)",
            (str(chat_id), title),
        )
        self.conn.commit()

    def matched_price_per_m2(self) -> Iterator[float]:
        """Yield price / area for every persisted matched listing.

        Consumed by the scoring phase to build a stable median across runs —
        keeps the "% vs median" signal meaningful even on quiet ticks that
        only surface a handful of new listings.
        """
        cur = self.conn.execute(
            "SELECT price, area FROM seen "
            "WHERE status='matched' AND price IS NOT NULL "
            "  AND area IS NOT NULL AND area > 0"
        )
        for price, area in cur.fetchall():
            yield price / area

    def emitted_fuzzy_keys(self) -> Set[str]:
        """Return every ``fuzzy_key`` that has ever been emitted (status='matched').

        Loaded once per run into a Python set — cross-source dedup is an O(1)
        set-lookup after that.
        """
        cur = self.conn.execute(
            "SELECT DISTINCT fuzzy_key FROM seen "
            "WHERE fuzzy_key IS NOT NULL AND status = 'matched'"
        )
        return {row[0] for row in cur.fetchall()}

    def add(
        self,
        listing: Listing,
        status: str,
        reject_reason: Optional[str] = None,
        fuzzy_key: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen
                (key, source, listing_id, url, title, price, area,
                 status, reject_reason, fuzzy_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.dedup_key,
                listing.source,
                listing.id,
                listing.url,
                listing.title,
                listing.price,
                listing.area,
                status,
                reject_reason,
                fuzzy_key,
            ),
        )
        self.conn.commit()

    def stored_price(self, key: str) -> Optional[int]:
        """Last price we recorded for a listing, or None if unknown."""
        cur = self.conn.execute("SELECT price FROM seen WHERE key = ?", (key,))
        row = cur.fetchone()
        return None if not row or row[0] is None else int(row[0])

    def record_price_change(
        self,
        key: str,
        old_price: Optional[int],
        new_price: int,
        fuzzy_key: Optional[str] = None,
    ) -> None:
        """Update the listing's current price and append to ``price_history``.

        ``add()`` uses INSERT OR IGNORE, so a re-seen listing never updates its
        row — which meant price movements were silently invisible. This is the
        explicit path for "same listing, new price".

        ``fuzzy_key`` is refreshed too when supplied: the key embeds the price,
        so leaving the old one behind would break cross-source dedup against
        the *new* price (the same flat relisted elsewhere at the new number
        would no longer collide).
        """
        if fuzzy_key is not None:
            self.conn.execute(
                "UPDATE seen SET price = ?, fuzzy_key = ? WHERE key = ?",
                (int(new_price), fuzzy_key, key),
            )
        else:
            self.conn.execute("UPDATE seen SET price = ? WHERE key = ?", (int(new_price), key))
        self.conn.execute(
            "INSERT INTO price_history (listing_key, old_price, new_price) VALUES (?, ?, ?)",
            (key, None if old_price is None else int(old_price), int(new_price)),
        )
        self.conn.commit()

    def update_score(self, key: str, score) -> None:
        """Persist a listing's :class:`~scanner.models.DealScore`.

        Scoring happens *after* the row is inserted (the median needs the
        whole run's price/m² sample), so this is a follow-up UPDATE rather
        than part of ``add()``. Storing it makes the score visible in the
        dashboard and sortable in SQL — otherwise it only ever existed
        inside one Telegram message.
        """
        if score is None:
            return
        self.conn.execute(
            "UPDATE seen SET score = ?, score_reasons = ? WHERE key = ?",
            (int(score.value), ", ".join(score.reasons) or None, key),
        )
        self.conn.commit()

    def prune_rejected(
        self,
        older_than_days: int,
        export_dir: Optional[Path] = None,
    ) -> int:
        """Delete ``status='rejected'`` rows older than ``older_than_days``.

        Rejected listings pile up faster than matches (Otodom returns ~180
        items every 15 min, most already past the filter). After a few weeks
        they're pure noise — the same listing won't come back, and if it did,
        the filter would reject it again. Pruning keeps the hot DB lean.

        We only touch ``rejected`` rows. ``matched`` and ``duplicate`` rows
        are kept forever — they're the actual dataset for later ML work.

        If ``export_dir`` is given, the doomed rows are dumped to
        ``<export_dir>/<YYYY-MM-DD>_pruned_rejected.csv`` (append) before the
        DELETE, so the data is recoverable.

        Returns the number of rows deleted.
        """
        cutoff = f"-{int(older_than_days)} days"
        cur = self.conn.execute(
            "SELECT * FROM seen "
            "WHERE status = 'rejected' AND first_seen_at < datetime('now', ?)",
            (cutoff,),
        )
        cols = [c[0] for c in cur.description]
        doomed = cur.fetchall()
        if not doomed:
            return 0

        if export_dir is not None:
            export_dir.mkdir(parents=True, exist_ok=True)
            out = export_dir / f"{datetime.now().strftime('%Y-%m-%d')}_pruned_rejected.csv"
            write_header = not out.exists()
            with out.open("a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(cols)
                w.writerows(doomed)

        self.conn.execute(
            "DELETE FROM seen "
            "WHERE status = 'rejected' AND first_seen_at < datetime('now', ?)",
            (cutoff,),
        )
        self.conn.commit()
        # VACUUM reclaims disk space on local SQLite; on remote libSQL/Turso
        # it's either a no-op or unsupported — silently swallow.
        try:
            self.conn.execute("VACUUM")
        except Exception:
            pass
        return len(doomed)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
