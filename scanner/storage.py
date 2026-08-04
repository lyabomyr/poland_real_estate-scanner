import sqlite3
from pathlib import Path
from typing import Optional

from .models import Listing


class SeenStore:
    """SQLite-backed dedup store.

    Records both matched and rejected listings so we don't re-evaluate them
    on subsequent runs. Keyed by ``<source>:<listing_id>``.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS seen (
        key           TEXT PRIMARY KEY,
        source        TEXT NOT NULL,
        listing_id    TEXT NOT NULL,
        url           TEXT,
        title         TEXT,
        price         INTEGER,
        area          REAL,
        status        TEXT NOT NULL,          -- 'matched' | 'rejected'
        reject_reason TEXT,
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS seen_source_idx ON seen(source);
    CREATE INDEX IF NOT EXISTS seen_status_idx ON seen(status);
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(self._SCHEMA)
        self.conn.commit()

    def has(self, key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE key = ? LIMIT 1", (key,))
        return cur.fetchone() is not None

    def add(self, listing: Listing, status: str, reject_reason: Optional[str] = None) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen
                (key, source, listing_id, url, title, price, area, status, reject_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
