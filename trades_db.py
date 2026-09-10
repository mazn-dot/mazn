"""
حفظ مزدوج:
- لو موجود DATABASE_URL → PostgreSQL (ثابت على Railway)
- غير كده → SQLite ملف محلي
"""
import os
import time
import json
import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")

DB_SQLITE = os.path.join(os.path.dirname(__file__), "trades.db")

DEFAULT_SETTINGS = {
    "trade_size_usd": 20.0,
    "stop_loss_pct": -8.0,
    "tp1_pct": 5.0,
    "tp2_pct": 10.0,
    "tp3_pct": 15.0,
    "max_open_trades": 3,
    "monitoring_enabled": False,
    "auto_buy_enabled": False,
}

_pg = None


def _get_pg():
    global _pg
    if _pg is not None:
        return _pg
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        # Railway sometimes gives postgres:// which psycopg2 needs as postgresql://
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _pg = psycopg2.connect(url, cursor_factory=RealDictCursor)
        _pg.autocommit = True
        log.info("Connected to PostgreSQL")
        return _pg
    except Exception as e:
        log.error("Postgres connect failed: %s — fallback SQLite", e)
        return None


def _sqlite():
    import sqlite3
    c = sqlite3.connect(DB_SQLITE)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    if USE_PG:
        conn = _get_pg()
        if conn:
            cur = conn.cursor()
            try:
                yield ("pg", cur)
            finally:
                cur.close()
            return
    # sqlite
    conn = _sqlite()
    cur = conn.cursor()
    try:
        yield ("sqlite", cur)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def init():
    with db() as (kind, c):
        if kind == "pg":
            c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                contract TEXT,
                chain TEXT,
                side TEXT DEFAULT 'buy',
                entry_price DOUBLE PRECISION NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                size_usd DOUBLE PRECISION NOT NULL,
                stop_loss DOUBLE PRECISION,
                tp1 DOUBLE PRECISION,
                tp2 DOUBLE PRECISION,
                tp3 DOUBLE PRECISION,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                opened_at DOUBLE PRECISION,
                closed_at DOUBLE PRECISION,
                close_price DOUBLE PRECISION,
                pnl_pct DOUBLE PRECISION,
                note TEXT
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (k, json.dumps(v)),
                )
        else:
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
                tp1 REAL, tp2 REAL, tp3 REAL,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                opened_at REAL, closed_at REAL,
                close_price REAL, pnl_pct REAL, note TEXT
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )
    log.info("DB init done mode=%s", "postgres" if USE_PG else "sqlite")


def _ph(kind):
    """placeholder style"""
    return "%s" if kind == "pg" else "?"


# ---------- Settings ----------

def get_setting(key, default=None):
    init()
    with db() as (kind, c):
        ph = _ph(kind)
        c.execute(f"SELECT value FROM settings WHERE key={ph}", (key,))
        row = c.fetchone()
        if not row:
            return DEFAULT_SETTINGS.get(key) if default is None else default
        val = row["value"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        try:
            return json.loads(val)
        except Exception:
            return val


def set_setting(key, value):
    init()
    with db() as (kind, c):
        ph = _ph(kind)
        if kind == "pg":
            c.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, json.dumps(value)),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
    return value


def load_settings():
    init()
    out = dict(DEFAULT_SETTINGS)
    with db() as (kind, c):
        c.execute("SELECT key, value FROM settings")
        rows = c.fetchall()
        for r in rows:
            k = r["key"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
            v = r["value"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
    return out


def save_settings(data: dict):
    for k, v in data.items():
        set_setting(k, v)
    return load_settings()


# ---------- Trades ----------

def open_trade(symbol, contract, chain, entry_price, quantity, size_usd,
               stop_loss, tp1, tp2, tp3, note=""):
    init()
    with db() as (kind, c):
        if kind == "pg":
            c.execute(
                """INSERT INTO trades
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, opened_at, note, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
                RETURNING id""",
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, time.time(), note),
            )
            row = c.fetchone()
            return row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        else:
            cur = c.execute(
                """INSERT INTO trades
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, opened_at, note, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, time.time(), note),
            )
            return cur.lastrowid


def get_open_trades():
    init()
    with db() as (kind, c):
        c.execute("SELECT * FROM trades WHERE status='open' ORDER BY id")
        rows = c.fetchall()
        return [dict(r) for r in rows]


def count_open():
    return len(get_open_trades())


def close_trade(trade_id, close_price, note=""):
    init()
    with db() as (kind, c):
        ph = _ph(kind)
        c.execute(f"SELECT * FROM trades WHERE id={ph}", (trade_id,))
        row = c.fetchone()
        if not row:
            return None
        row = dict(row)
        entry = row["entry_price"]
        pnl = ((close_price - entry) / entry) * 100 if entry else 0
        old_note = row.get("note") or ""
        if kind == "pg":
            c.execute(
                """UPDATE trades SET status='closed', closed_at=%s, close_price=%s,
                   pnl_pct=%s, note=%s WHERE id=%s""",
                (time.time(), close_price, pnl, (old_note + " | " + note).strip(" |"), trade_id),
            )
        else:
            c.execute(
                """UPDATE trades SET status='closed', closed_at=?, close_price=?,
                   pnl_pct=?, note=? WHERE id=?""",
                (time.time(), close_price, pnl, (old_note + " | " + note).strip(" |"), trade_id),
            )
        return pnl


def mark_tp(trade_id, level):
    col = f"tp{level}_hit"
    with db() as (kind, c):
        ph = _ph(kind)
        c.execute(f"UPDATE trades SET {col}=1 WHERE id={ph}", (trade_id,))


def has_open_symbol(symbol):
    symbol = symbol.upper()
    return any(str(t["symbol"]).upper() == symbol for t in get_open_trades())


def list_open_text():
    """عرض مبسط: اسم | نسبة ربح/خسارة | حجم بالدولار"""
    trades = get_open_trades()
    if not trades:
        return "لا توجد صفقات مفتوحة."

    lines = ["📋 <b>الصفقات المفتوحة</b>", ""]
    try:
        import mexc_trade
    except Exception:
        mexc_trade = None

    for t in trades:
        sym = t.get("symbol") or "?"
        entry = float(t.get("entry_price") or 0)
        size = float(t.get("size_usd") or 0)
        pnl = 0.0
        if mexc_trade and entry > 0:
            try:
                pair = mexc_trade.resolve_symbol(sym)
                price = mexc_trade.get_price(pair)
                if price > 0:
                    pnl = ((price - entry) / entry) * 100
            except Exception:
                pass
        if pnl > 0.05:
            tag = f"ربح {pnl:.1f}%"
            icon = "✅"
        elif pnl < -0.05:
            tag = f"خسارة {pnl:.1f}%"
            icon = "❌"
        else:
            tag = f"{pnl:.1f}%"
            icon = "⏳"
        lines.append(f"{icon} <b>{sym}</b> | {tag} | {size:.0f}$")
    return "\n".join(lines)


def get_all_trades(limit=50):
    init()
    with db() as (kind, c):
        if kind == "pg":
            c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT %s", (limit,))
        else:
            c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in c.fetchall()]


def report_text(limit=40):
    trades = get_all_trades(limit)
    if not trades:
        return "لا يوجد سجل صفقات."
    lines = ["📊 <b>تقرير الصفقات</b>", ""]
    wins = losses = open_n = 0
    total_pnl = 0.0
    for t in trades:
        sym = t.get("symbol") or "?"
        status = t.get("status") or ""
        entry = float(t.get("entry_price") or 0)
        close_p = t.get("close_price")
        pnl = t.get("pnl_pct")
        size = float(t.get("size_usd") or 0)
        if status == "open":
            open_n += 1
            tp_hits = int(t.get("tp1_hit") or 0) + int(t.get("tp2_hit") or 0) + int(t.get("tp3_hit") or 0)
            extra = f" | أهداف {tp_hits}/3" if tp_hits else ""
            lines.append(f"⏳ <b>{sym}</b> — مفتوحة{extra}")
            continue
        if pnl is None and close_p and entry:
            pnl = ((float(close_p) - entry) / entry) * 100
        pnl = float(pnl or 0)
        usd = size * (pnl / 100.0)
        total_pnl += usd
        if pnl >= 0:
            wins += 1
            lines.append(f"✅ <b>{sym}</b> — ربح {pnl:.1f}% (~{usd:+.2f}$)")
        else:
            losses += 1
            lines.append(f"❌ <b>{sym}</b> — خسارة {pnl:.1f}% (~{usd:.2f}$)")
    lines += ["", "────────────",
              f"مفتوحة: {open_n} | رابحة: {wins} | خاسرة: {losses}",
              f"صافي تقديري (المقفلة): <b>{total_pnl:+.2f}$</b>"]
    mode = "PostgreSQL 🗄️" if USE_PG else "SQLite 📁"
    lines.append(f"التخزين: {mode}")
    return "\n".join(lines)
