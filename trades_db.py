import sqlite3
import os
import time
from contextlib import contextmanager

DB = os.path.join(os.path.dirname(__file__), "trades.db")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            contract TEXT,
            chain TEXT,
            side TEXT DEFAULT 'buy',
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            size_usd REAL NOT NULL,
            stop_loss REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open',
            opened_at REAL,
            closed_at REAL,
            close_price REAL,
            pnl_pct REAL,
            note TEXT
        )
        """)
        c.commit()


@contextmanager
def db():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def open_trade(symbol, contract, chain, entry_price, quantity, size_usd,
               stop_loss, tp1, tp2, tp3, note=""):
    init()
    with db() as c:
        cur = c.execute(
            """INSERT INTO trades
            (symbol, contract, chain, entry_price, quantity, size_usd,
             stop_loss, tp1, tp2, tp3, opened_at, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, contract, chain, entry_price, quantity, size_usd,
             stop_loss, tp1, tp2, tp3, time.time(), note),
        )
        return cur.lastrowid


def get_open_trades():
    init()
    with db() as c:
        rows = c.execute("SELECT * FROM trades WHERE status='open' ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def count_open():
    return len(get_open_trades())


def close_trade(trade_id, close_price, note=""):
    init()
    with db() as c:
        row = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return None
        entry = row["entry_price"]
        pnl = ((close_price - entry) / entry) * 100 if entry else 0
        c.execute(
            """UPDATE trades SET status='closed', closed_at=?, close_price=?,
               pnl_pct=?, note=COALESCE(note,'') || ? WHERE id=?""",
            (time.time(), close_price, pnl, " | " + note, trade_id),
        )
        return pnl


def mark_tp(trade_id, level):
    """level = 1, 2, or 3"""
    col = f"tp{level}_hit"
    with db() as c:
        c.execute(f"UPDATE trades SET {col}=1 WHERE id=?", (trade_id,))


def has_open_symbol(symbol):
    symbol = symbol.upper()
    return any(t["symbol"].upper() == symbol for t in get_open_trades())


def list_open_text():
    trades = get_open_trades()
    if not trades:
        return "لا توجد صفقات مفتوحة."
    lines = []
    for t in trades:
        lines.append(
            f"#{t['id']} <b>{t['symbol']}</b> @ {t['entry_price']:.6g}\n"
            f"   حجم: {t['size_usd']}$ | SL: {t['stop_loss']:.6g}\n"
            f"   TP: {t['tp1']:.6g} / {t['tp2']:.6g} / {t['tp3']:.6g}"
        )
    return "\n\n".join(lines)
