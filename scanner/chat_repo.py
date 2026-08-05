"""CRUD for per-chat overrides stored in ``chat_configs``.

Thin wrapper around :class:`~scanner.storage.SeenStore` so the storage
module doesn't grow N-methods per feature. The store owns the connection;
this repository owns the shape/serialisation of chat-config rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .chat_config import ChatOverride
from .storage import SeenStore


@dataclass
class ChatRow:
    """One row from ``chat_configs``."""
    chat_id: str
    title: Optional[str]
    enabled: bool
    override: ChatOverride
    updated_at: Optional[str] = None


class ChatConfigRepo:
    def __init__(self, store: SeenStore):
        self.store = store

    # ── read ──────────────────────────────────────────────────────────

    def get(self, chat_id) -> Optional[ChatRow]:
        cur = self.store.conn.execute(
            "SELECT chat_id, title, enabled, config, updated_at "
            "FROM chat_configs WHERE chat_id = ?",
            (str(chat_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_chat(row)

    def list_enabled(self) -> List[ChatRow]:
        cur = self.store.conn.execute(
            "SELECT chat_id, title, enabled, config, updated_at "
            "FROM chat_configs WHERE enabled = 1 ORDER BY chat_id"
        )
        return [_row_to_chat(r) for r in cur.fetchall()]

    def list_all(self) -> List[ChatRow]:
        cur = self.store.conn.execute(
            "SELECT chat_id, title, enabled, config, updated_at "
            "FROM chat_configs ORDER BY chat_id"
        )
        return [_row_to_chat(r) for r in cur.fetchall()]

    # ── write ─────────────────────────────────────────────────────────

    def upsert(self, chat_id, title: Optional[str], override: ChatOverride,
               enabled: bool = True) -> None:
        self.store.conn.execute(
            """
            INSERT INTO chat_configs (chat_id, title, enabled, config, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET
                title      = excluded.title,
                enabled    = excluded.enabled,
                config     = excluded.config,
                updated_at = datetime('now')
            """,
            (str(chat_id), title, 1 if enabled else 0, override.to_json()),
        )
        self.store.conn.commit()

    def set_enabled(self, chat_id, enabled: bool) -> None:
        self.store.conn.execute(
            "UPDATE chat_configs SET enabled = ?, updated_at = datetime('now') "
            "WHERE chat_id = ?",
            (1 if enabled else 0, str(chat_id)),
        )
        self.store.conn.commit()

    def delete(self, chat_id) -> None:
        self.store.conn.execute("DELETE FROM chat_configs WHERE chat_id = ?", (str(chat_id),))
        self.store.conn.commit()

    def register_chat(self, chat_id, title: Optional[str]) -> int:
        """Ensure the chat exists as an enabled target; backfill on first create."""
        row = self.get(chat_id)
        if row is not None:
            new_title = title or row.title
            if new_title != row.title or not row.enabled:
                self.upsert(chat_id, new_title, row.override, enabled=True)
            return 0
        self.upsert(chat_id, title, ChatOverride(), enabled=True)
        return self.backfill_emissions_from_seen(chat_id)

    # ── emissions tracking (per-chat dedup) ───────────────────────────

    def has_emitted(self, chat_id, listing_key: str) -> bool:
        cur = self.store.conn.execute(
            "SELECT 1 FROM chat_emissions WHERE chat_id = ? AND listing_key = ? LIMIT 1",
            (str(chat_id), listing_key),
        )
        return cur.fetchone() is not None

    def record_emission(self, chat_id, listing_key: str) -> None:
        self.store.conn.execute(
            "INSERT OR IGNORE INTO chat_emissions (chat_id, listing_key) VALUES (?, ?)",
            (str(chat_id), listing_key),
        )
        self.store.conn.commit()

    def backfill_emissions_from_seen(self, chat_id) -> int:
        """Mark every ``status='matched'`` listing as already-emitted to this chat.

        Called when a chat is first registered — otherwise the *very first*
        scan for that chat would flood it with the whole historical backlog
        (100+ apartments). New chats should get *future* matches, not the
        archive.

        Returns approximate row count. libSQL doesn't report ``rowcount``
        reliably for INSERT-OR-IGNORE, so we count first and insert second.
        """
        cur = self.store.conn.execute(
            "SELECT COUNT(*) FROM seen WHERE status='matched'"
        )
        expected = int(cur.fetchone()[0])
        self.store.conn.execute(
            "INSERT OR IGNORE INTO chat_emissions (chat_id, listing_key) "
            "SELECT ?, key FROM seen WHERE status='matched'",
            (str(chat_id),),
        )
        self.store.conn.commit()
        return expected

    def emitted_fuzzy_keys(self, chat_id) -> set:
        """Fuzzy keys already emitted to this specific chat — for per-chat
        cross-source dedup (analogue of :meth:`SeenStore.emitted_fuzzy_keys`
        but scoped to one chat)."""
        cur = self.store.conn.execute(
            "SELECT DISTINCT s.fuzzy_key FROM chat_emissions e "
            "JOIN seen s ON s.key = e.listing_key "
            "WHERE e.chat_id = ? AND s.fuzzy_key IS NOT NULL",
            (str(chat_id),),
        )
        return {r[0] for r in cur.fetchall() if r[0]}

    # ── command deduplication (never process the same update twice) ───

    def is_update_processed(self, update_id: int) -> bool:
        cur = self.store.conn.execute(
            "SELECT 1 FROM command_updates WHERE update_id = ? LIMIT 1",
            (int(update_id),),
        )
        return cur.fetchone() is not None

    def mark_update_processed(self, update_id: int) -> None:
        self.store.conn.execute(
            "INSERT OR IGNORE INTO command_updates (update_id) VALUES (?)",
            (int(update_id),),
        )
        self.store.conn.commit()

    def claim_update(self, update_id: int) -> bool:
        self.store.conn.execute(
            "INSERT OR IGNORE INTO command_updates (update_id) VALUES (?)",
            (int(update_id),),
        )
        cur = self.store.conn.execute("SELECT changes()")
        self.store.conn.commit()
        row = cur.fetchone()
        return bool(row and int(row[0]) > 0)

    # ── /scan dispatch rate limiting ──────────────────────────────────

    def recent_scan_dispatch_count(self, within_seconds: int) -> int:
        """How many ``/scan`` dispatches happened in the last N seconds, globally.

        Global (not per-chat) on purpose: the GitHub Actions queue is a shared
        resource, so ten chats each dispatching once is just as disruptive as
        one chat dispatching ten times.
        """
        cur = self.store.conn.execute(
            "SELECT COUNT(*) FROM scan_dispatches "
            "WHERE dispatched_at >= datetime('now', ?)",
            (f"-{int(within_seconds)} seconds",),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def record_scan_dispatch(self, chat_id, user_id=None) -> None:
        self.store.conn.execute(
            "INSERT INTO scan_dispatches (chat_id, user_id) VALUES (?, ?)",
            (str(chat_id), None if user_id is None else str(user_id)),
        )
        self.store.conn.commit()

    # ── stats ─────────────────────────────────────────────────────────

    def stats_last_days(self, chat_id, days: int) -> Dict[str, int]:
        cur = self.store.conn.execute(
            """
            SELECT COUNT(*) FROM chat_emissions e
            JOIN seen s ON s.key = e.listing_key
            WHERE e.chat_id = ? AND e.sent_at >= datetime('now', ?)
            """,
            (str(chat_id), f"-{int(days)} days"),
        )
        emitted = cur.fetchone()[0]
        return {"emitted": int(emitted)}


def _row_to_chat(row) -> ChatRow:
    chat_id, title, enabled, config_blob, updated_at = row
    return ChatRow(
        chat_id=str(chat_id),
        title=title,
        enabled=bool(enabled),
        override=ChatOverride.from_json(config_blob),
        updated_at=updated_at,
    )
