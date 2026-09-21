"""SQLite state: نگاشت پیام تلگرام به تیکت‌های باز شده روی متاتریدر."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import Signal, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL,
    entry      REAL,
    sl         REAL,
    tps        TEXT    NOT NULL DEFAULT '[]',
    raw        TEXT,
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at TEXT    NOT NULL,
    UNIQUE (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS trades (
    ticket      INTEGER PRIMARY KEY,
    signal_id   INTEGER NOT NULL REFERENCES signals(id),
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    volume      REAL    NOT NULL,
    entry_price REAL,
    sl          REAL,
    tp          REAL,
    is_pending  INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'open',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = "state.db") -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --------------------------------------------------------------- signals

    def seen(self, chat_id: int, msg_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        ).fetchone()
        return row is not None

    def add_signal(self, signal: Signal, status: str = "pending") -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals"
            " (chat_id, msg_id, symbol, side, entry, sl, tps, raw, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal.chat_id, signal.msg_id, signal.symbol, signal.side.value,
             signal.entry, signal.sl, json.dumps(signal.tps), signal.raw_text,
             status, _now()),
        )
        self.conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = self.conn.execute(
            "SELECT id FROM signals WHERE chat_id = ? AND msg_id = ?",
            (signal.chat_id, signal.msg_id),
        ).fetchone()
        return int(row["id"])

    def set_signal_status(self, signal_id: int, status: str) -> None:
        self.conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
        self.conn.commit()

    def signal_by_msg(self, chat_id: int, msg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM signals WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        ).fetchone()

    def signal_by_id(self, signal_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()

    def active_signals(self, symbol: Optional[str] = None) -> list[sqlite3.Row]:
        """سیگنال‌هایی که هنوز حداقل یک معامله‌ی باز یا پندینگ دارند، جدیدترین اول."""
        query = (
            "SELECT DISTINCT s.* FROM signals s"
            " JOIN trades t ON t.signal_id = s.id"
            " WHERE t.status IN ('open', 'pending')"
        )
        params: tuple = ()
        if symbol:
            query += " AND s.symbol = ?"
            params = (symbol,)
        query += " ORDER BY s.id DESC"
        return list(self.conn.execute(query, params).fetchall())

    def load_signal(self, row: sqlite3.Row) -> Signal:
        return Signal(
            symbol=row["symbol"],
            side=Side(row["side"]),
            entry=row["entry"],
            sl=row["sl"],
            tps=json.loads(row["tps"]),
            raw_text=row["raw"] or "",
            msg_id=row["msg_id"],
            chat_id=row["chat_id"],
        )

    # ---------------------------------------------------------------- trades

    def add_trade(self, signal_id: int, execution: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trades"
            " (ticket, signal_id, symbol, side, volume, entry_price, sl, tp,"
            "  is_pending, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (execution.ticket, signal_id, execution.symbol, execution.side.value,
             execution.volume, execution.price, execution.sl, execution.tp,
             int(execution.is_pending), "pending" if execution.is_pending else "open", _now()),
        )
        self.conn.commit()

    def trades_for_signal(self, signal_id: int, only_active: bool = True) -> list[sqlite3.Row]:
        query = "SELECT * FROM trades WHERE signal_id = ?"
        if only_active:
            query += " AND status IN ('open', 'pending')"
        return list(self.conn.execute(query, (signal_id,)).fetchall())

    def set_trade_status(self, ticket: int, status: str) -> None:
        self.conn.execute("UPDATE trades SET status = ? WHERE ticket = ?", (status, ticket))
        self.conn.commit()

    def update_trade_sl(self, ticket: int, sl: float) -> None:
        self.conn.execute("UPDATE trades SET sl = ? WHERE ticket = ?", (sl, ticket))
        self.conn.commit()

    def active_trades(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM trades WHERE status IN ('open', 'pending')"
        ).fetchall())
