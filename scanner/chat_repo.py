"""CRUD for per-chat overrides stored in ``chat_configs``.

Thin wrapper around :class:`~scanner.storage.SeenStore` so the storage
module doesn't grow N-methods per feature. The store owns the connection;
this repository owns the shape/serialisation of chat-config rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .chat_config import ChatOverride
from .models import DealScore, Listing
from .storage import SeenStore


def _listing_from_row(row) -> Listing:
    """Rebuild a :class:`Listing` from a ``seen`` row.

    Only the fields a notification renders. The score was computed and
    persisted when the listing was first matched, so a backlog message shows
    the same number the listing has on the dashboard.
    """
    (source, listing_id, url, title, price, area, location, description,
     image_url, score, score_reasons, fuzzy_key) = row
    listing = Listing(
        source=source,
        id=listing_id,
        url=url,
        title=title,
        price=price,
        area=area,
        location=location,
        description=description,
        image_url=image_url,
    )
    if score is not None:
        # SeenStore.update_score joins them with ", " — mirror that exactly.
        reasons = [r for r in (score_reasons or "").split(", ") if r]
        listing.score = DealScore(value=int(score), reasons=reasons)
    return listing


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

    def emitted_price(self, chat_id, listing_key: str) -> Optional[int]:
        """Price this chat was last notified at, or None if never / unknown.

        Drives price-change re-notification: if the listing now costs
        something different from what we told this chat, it's news again.
        """
        cur = self.store.conn.execute(
            "SELECT emitted_price FROM chat_emissions "
            "WHERE chat_id = ? AND listing_key = ? LIMIT 1",
            (str(chat_id), listing_key),
        )
        row = cur.fetchone()
        return None if not row or row[0] is None else int(row[0])

    def record_emission(self, chat_id, listing_key: str, price: Optional[int] = None) -> None:
        """Mark a listing as delivered to a chat, remembering the price.

        Upserts rather than INSERT OR IGNORE so a re-emission after a price
        change refreshes both the price and the timestamp.
        """
        self.store.conn.execute(
            """
            INSERT INTO chat_emissions (chat_id, listing_key, emitted_price, sent_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(chat_id, listing_key) DO UPDATE SET
                emitted_price = excluded.emitted_price,
                sent_at       = excluded.sent_at
            """,
            (str(chat_id), listing_key, None if price is None else int(price)),
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

    def undelivered(self, chat_id, limit: int = 2000) -> List[Listing]:
        """Matched listings this chat has never been sent, best score first.

        This is the delivery backlog, and it exists because discovery and
        delivery run at wildly different speeds. A first sweep finds ~1000
        listings in six minutes; Telegram accepts about twenty messages a
        minute into a group, so delivering them takes over half an hour and
        the scheduled run is killed long before it finishes.

        Reading the backlog from the database instead of from "what this scan
        happened to see" means an interrupted run costs nothing: the next run
        picks up exactly where it stopped, without re-walking deep pages that
        the portal only surfaces on a full sweep.
        """
        cur = self.store.conn.execute(
            """
            SELECT s.source, s.listing_id, s.url, s.title, s.price, s.area,
                   s.location, s.description, s.image_url, s.score,
                   s.score_reasons, s.fuzzy_key
            FROM seen s
            WHERE s.status = 'matched'
              AND NOT EXISTS (
                  SELECT 1 FROM chat_emissions e
                  WHERE e.chat_id = ? AND e.listing_key = s.key
              )
            ORDER BY s.score DESC, s.price ASC
            LIMIT ?
            """,
            (str(chat_id), int(limit)),
        )
        return [_listing_from_row(r) for r in cur.fetchall()]

    # ── command deduplication (never process the same update twice) ───

    def claim_update(self, update_id: int) -> bool:
        """Atomically claim an update id. True only for the first caller.

        Uses the cursor's ``rowcount`` rather than ``SELECT changes()``: over
        Turso's HTTP API each statement is its own request, so connection-scoped
        ``changes()`` isn't reliable. ``rowcount`` is populated on both
        backends (sqlite3 natively, Turso from ``affected_row_count``).
        """
        cur = self.store.conn.execute(
            "INSERT OR IGNORE INTO command_updates (update_id) VALUES (?)",
            (int(update_id),),
        )
        self.store.conn.commit()
        return bool(cur.rowcount and cur.rowcount > 0)

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
