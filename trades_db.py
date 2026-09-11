"""
حفظ على PostgreSQL إن وُجد اتصال حقيقي، وإلا SQLite.
يعرض سبب الفشل بوضوح.
"""
import os
import time
import json
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

log = logging.getLogger(__name__)

def _resolve_sqlite_path():
    """Prefer persistent volume paths so data survives bot restarts on Railway."""
    explicit = (os.getenv("SQLITE_DB_PATH") or "").strip()
    if explicit:
        return explicit
    # Common persistent mount points on Railway / Docker
    for candidate in ("/data/trades.db", "/app/data/trades.db"):
        parent = os.path.dirname(candidate)
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return candidate
    return os.path.join(os.path.dirname(__file__), "trades.db")


DB_SQLITE = _resolve_sqlite_path()
os.makedirs(os.path.dirname(os.path.abspath(DB_SQLITE)) or ".", exist_ok=True)

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

# حالة الاتصال الفعلية
_STATUS = {
    "mode": "sqlite",
    "reason": "لم يتم الفحص بعد",
    "url_found": False,
}

_pg_conn = None


def _find_database_url():
    """كل الأسماء الشائعة على Railway"""
    keys = [
        "DATABASE_URL",
        "DATABASE_PRIVATE_URL",
        "POSTGRES_URL",
        "POSTGRESQL_URL",
        "PGURL",
        "DB_URL",
    ]
    for k in keys:
        v = (os.getenv(k) or "").strip()
        if v and ("postgres" in v.lower() or v.startswith("postgresql")):
            return k, v
    # أي env فيه postgres
    for k, v in os.environ.items():
        if v and "postgres" in v.lower() and "://" in v and "url" in k.lower():
            return k, v.strip()
    return None, ""


def _get_pg():
    global _pg_conn
    if _pg_conn is not None:
        try:
            _pg_conn.cursor().execute("SELECT 1")
            return _pg_conn
        except Exception:
            _pg_conn = None

    key, url = _find_database_url()
    if not url:
        _STATUS.update(mode="sqlite", reason="مفيش DATABASE_URL في متغيرات البوت", url_found=False)
        return None

    _STATUS["url_found"] = True
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        url = url.replace("postgres://", "postgresql://", 1)
        # Railway عادة يحتاج SSL
        if "sslmode" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        conn = psycopg2.connect(url, cursor_factory=RealDictCursor, connect_timeout=15)
        conn.autocommit = True
        _pg_conn = conn
        _STATUS.update(mode="postgres", reason=f"متصل عبر {key}", url_found=True)
        log.info("Postgres OK via %s", key)
        return conn
    except Exception as e:
        # محاولة ثانية بدون SSL صارم
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            url2 = url.replace("postgres://", "postgresql://", 1)
            if "sslmode" in url2:
                # جرب prefer
                import re
                url2 = re.sub(r"sslmode=[^&]*", "sslmode=prefer", url2)
            else:
                url2 += ("&" if "?" in url2 else "?") + "sslmode=prefer"
            conn = psycopg2.connect(url2, cursor_factory=RealDictCursor, connect_timeout=15)
            conn.autocommit = True
            _pg_conn = conn
            _STATUS.update(mode="postgres", reason=f"متصل عبر {key} (ssl prefer)", url_found=True)
            log.info("Postgres OK via %s (prefer)", key)
            return conn
        except Exception as e2:
            _STATUS.update(mode="sqlite", reason=f"فشل الاتصال: {e2}", url_found=True)
            log.error("Postgres failed: %s | retry: %s", e, e2)
            return None


def storage_status_text():
    _get_pg()  # refresh
    if _STATUS["mode"] == "postgres":
        return f"PostgreSQL 🗄️ ثابت\n{_STATUS['reason']}"
    # SQLite: warn unless path is on a known persistent volume
    path = DB_SQLITE
    persistent = path.startswith("/data/") or path.startswith("/app/data/")
    if persistent:
        return f"SQLite 🗄️ على Volume\n{path}"
    return (
        f"SQLite ⚠️ مؤقت (يضيع عند إعادة التشغيل)\n"
        f"المسار: {path}\n"
        f"السبب: {_STATUS['reason']}\n"
        f"👉 أضف PostgreSQL أو Volume على /data"
    )


def use_postgres():
    return _get_pg() is not None


def _sqlite():
    import sqlite3
    c = sqlite3.connect(DB_SQLITE, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_trade_columns(kind, c):
    """Upgrade older deployments without deleting existing trade history."""
    expected = {
        "entry_price": ("DOUBLE PRECISION", "REAL"),
        "quantity": ("DOUBLE PRECISION", "REAL"),
        "size_usd": ("DOUBLE PRECISION", "REAL"),
        "stop_loss": ("DOUBLE PRECISION", "REAL"),
        "tp1": ("DOUBLE PRECISION", "REAL"),
        "tp2": ("DOUBLE PRECISION", "REAL"),
        "tp3": ("DOUBLE PRECISION", "REAL"),
        "side": ("TEXT DEFAULT 'buy'", "TEXT DEFAULT 'buy'"),
        "contract": ("TEXT", "TEXT"),
        "chain": ("TEXT", "TEXT"),
        "tp1_hit": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        "tp2_hit": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        "tp3_hit": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        "closed_at": ("DOUBLE PRECISION", "REAL"),
        "close_price": ("DOUBLE PRECISION", "REAL"),
        "pnl_pct": ("DOUBLE PRECISION", "REAL"),
        "note": ("TEXT", "TEXT"),
        "status": ("TEXT DEFAULT 'open'", "TEXT DEFAULT 'open'"),
        "opened_at": ("DOUBLE PRECISION", "REAL"),
    }
    if kind == "pg":
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s", ("trades",))
        present = {r["column_name"] if hasattr(r, "keys") else r[0] for r in c.fetchall()}
        type_index = 0
    else:
        c.execute("PRAGMA table_info(trades)")
        present = {r[1] for r in c.fetchall()}
        type_index = 1
    for column, types in expected.items():
        if column not in present:
            c.execute(f"ALTER TABLE trades ADD COLUMN {column} {types[type_index]}")
            log.warning("Added missing trades.%s column", column)


def _timestamp_value(kind, c, column):
    """Match the value type of legacy PostgreSQL timestamp columns."""
    if kind != "pg":
        return time.time()
    c.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s",
        ("trades", column),
    )
    row = c.fetchone()
    data_type = row["data_type"] if hasattr(row, "keys") else (row[0] if row else "")
    if data_type and "timestamp" in str(data_type):
        return datetime.now(timezone.utc)
    return time.time()


@contextmanager
def db():
    conn = _get_pg()
    if conn is not None:
        cur = conn.cursor()
        try:
            yield ("pg", cur)
        finally:
            cur.close()
        return
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
                tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION, tp3 DOUBLE PRECISION,
                tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, tp3_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                opened_at DOUBLE PRECISION, closed_at DOUBLE PRECISION,
                close_price DOUBLE PRECISION, pnl_pct DOUBLE PRECISION, note TEXT
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                label TEXT PRIMARY KEY,
                address TEXT NOT NULL
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
                contract TEXT, chain TEXT, side TEXT DEFAULT 'buy',
                entry_price REAL NOT NULL, quantity REAL NOT NULL, size_usd REAL NOT NULL,
                stop_loss REAL, tp1 REAL, tp2 REAL, tp3 REAL,
                tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, tp3_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                opened_at REAL, closed_at REAL, close_price REAL, pnl_pct REAL, note TEXT
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                label TEXT PRIMARY KEY,
                address TEXT NOT NULL
            )
            """)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )
        _ensure_trade_columns(kind, c)
        _seed_default_wallets(kind, c)
    # تشخيص: أسماء متغيرات DB الموجودة
    db_keys = [k for k in os.environ if "DATABASE" in k.upper() or "POSTGRES" in k.upper() or "PG" == k[:2].upper()]
    log.info("DB env keys present: %s", db_keys)
    log.info("DB init mode=%s reason=%s", _STATUS["mode"], _STATUS["reason"])


def _seed_default_wallets(kind, c):
    """Insert default wallets only when the wallets table is empty."""
    c.execute("SELECT COUNT(*) AS n FROM wallets")
    row = c.fetchone()
    count = row["n"] if hasattr(row, "keys") else row[0]
    if count and int(count) > 0:
        return
    try:
        from config import DEFAULT_WALLETS
    except Exception:
        DEFAULT_WALLETS = {}
    # Migrate legacy wallets.json if present
    legacy = {}
    try:
        legacy_path = os.path.join(os.path.dirname(__file__), "wallets.json")
        if os.path.isfile(legacy_path):
            with open(legacy_path, encoding="utf-8") as f:
                legacy = json.load(f) or {}
    except Exception:
        legacy = {}
    source = legacy if legacy else DEFAULT_WALLETS
    for label, address in (source or {}).items():
        if not label or not address:
            continue
        if kind == "pg":
            c.execute(
                "INSERT INTO wallets (label, address) VALUES (%s, %s) ON CONFLICT (label) DO NOTHING",
                (str(label), str(address)),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO wallets (label, address) VALUES (?, ?)",
                (str(label), str(address)),
            )
    if source:
        log.info("Seeded %s wallets into DB", len(source))


def get_setting(key, default=None):
    init()
    with db() as (kind, c):
        ph = "%s" if kind == "pg" else "?"
        c.execute(f"SELECT value FROM settings WHERE key={ph}", (key,))
        row = c.fetchone()
        if not row:
            return DEFAULT_SETTINGS.get(key) if default is None else default
        val = row["value"] if hasattr(row, "keys") else row[0]
        try:
            return json.loads(val)
        except Exception:
            return val


def set_setting(key, value):
    init()
    with db() as (kind, c):
        if kind == "pg":
            c.execute(
                "INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, json.dumps(value)),
            )
        else:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, json.dumps(value)))
    return value


def load_settings():
    init()
    out = dict(DEFAULT_SETTINGS)
    with db() as (kind, c):
        c.execute("SELECT key, value FROM settings")
        for r in c.fetchall():
            k = r["key"] if hasattr(r, "keys") else r[0]
            v = r["value"] if hasattr(r, "keys") else r[1]
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
    return out


def save_settings(data: dict):
    for k, v in data.items():
        set_setting(k, v)
    return load_settings()


def open_trade(symbol, contract, chain, entry_price, quantity, size_usd,
               stop_loss, tp1, tp2, tp3, note=""):
    init()
    with db() as (kind, c):
        if kind == "pg":
            c.execute(
                """INSERT INTO trades
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, opened_at, note, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open') RETURNING id""",
                (symbol, contract, chain, entry_price, quantity, size_usd,
                 stop_loss, tp1, tp2, tp3, _timestamp_value(kind, c, "opened_at"), note),
            )
            row = c.fetchone()
            return row["id"] if hasattr(row, "keys") else row[0]
        cur = c.execute(
            """INSERT INTO trades
            (symbol, contract, chain, entry_price, quantity, size_usd,
             stop_loss, tp1, tp2, tp3, opened_at, note, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
            (symbol, contract, chain, entry_price, quantity, size_usd,
             stop_loss, tp1, tp2, tp3, _timestamp_value(kind, c, "opened_at"), note),
        )
        return cur.lastrowid


def get_open_trades():
    init()
    with db() as (kind, c):
        c.execute("SELECT * FROM trades WHERE status='open' ORDER BY id")
        return [dict(r) for r in c.fetchall()]


def count_open():
    return len(get_open_trades())


def close_trade(trade_id, close_price, note=""):
    init()
    with db() as (kind, c):
        ph = "%s" if kind == "pg" else "?"
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
                (_timestamp_value(kind, c, "closed_at"), close_price, pnl, (old_note + " | " + note).strip(" |"), trade_id),
            )
        else:
            c.execute(
                """UPDATE trades SET status='closed', closed_at=?, close_price=?,
                   pnl_pct=?, note=? WHERE id=?""",
                (_timestamp_value(kind, c, "closed_at"), close_price, pnl, (old_note + " | " + note).strip(" |"), trade_id),
            )
        return pnl


def mark_tp(trade_id, level):
    col = f"tp{level}_hit"
    with db() as (kind, c):
        ph = "%s" if kind == "pg" else "?"
        c.execute(f"UPDATE trades SET {col}=1 WHERE id={ph}", (trade_id,))


def has_open_symbol(symbol):
    symbol = symbol.upper()
    return any(str(t["symbol"]).upper() == symbol for t in get_open_trades())


def list_open_text():
    trades = get_open_trades()
    if not trades:
        return "لا توجد صفقات مفتوحة.\n\n" + storage_status_text()
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
                price = mexc_trade.get_price(mexc_trade.resolve_symbol(sym))
                if price > 0:
                    pnl = ((price - entry) / entry) * 100
            except Exception:
                pass
        if pnl > 0.05:
            icon, tag = "✅", f"ربح {pnl:.1f}%"
        elif pnl < -0.05:
            icon, tag = "❌", f"خسارة {pnl:.1f}%"
        else:
            icon, tag = "⏳", f"{pnl:.1f}%"
        lines.append(f"{icon} <b>{sym}</b> | {tag} | {size:.0f}$")
    lines += ["", storage_status_text()]
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
    lines = ["📊 <b>تقرير الصفقات</b>", ""]
    if not trades:
        lines.append("لا يوجد سجل.")
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
            lines.append(f"⏳ <b>{sym}</b> — مفتوحة | {size:.0f}$")
            continue
        if pnl is None and close_p and entry:
            pnl = ((float(close_p) - entry) / entry) * 100
        pnl = float(pnl or 0)
        usd = size * (pnl / 100.0)
        total_pnl += usd
        if pnl >= 0:
            wins += 1
            lines.append(f"✅ <b>{sym}</b> | ربح {pnl:.1f}% | {size:.0f}$")
        else:
            losses += 1
            lines.append(f"❌ <b>{sym}</b> | خسارة {pnl:.1f}% | {size:.0f}$")
    lines += ["", "────────────",
              f"مفتوحة: {open_n} | رابحة: {wins} | خاسرة: {losses}",
              f"صافي: <b>{total_pnl:+.2f}$</b>",
              "", storage_status_text()]
    return "\n".join(lines)



# ---------- Wallets (persisted in same DB as trades/settings) ----------

def load_wallets():
    """Return {label: address} dict from the wallets table."""
    init()
    with db() as (kind, c):
        c.execute("SELECT label, address FROM wallets ORDER BY label")
        rows = c.fetchall()
        out = {}
        for r in rows:
            label = r["label"] if hasattr(r, "keys") else r[0]
            address = r["address"] if hasattr(r, "keys") else r[1]
            if label and address:
                out[str(label)] = str(address)
        return out


def save_wallets(data: dict):
    """Replace all wallets with the given dict (atomic enough for our use)."""
    init()
    with db() as (kind, c):
        c.execute("DELETE FROM wallets")
        for label, address in (data or {}).items():
            if not label or not address:
                continue
            if kind == "pg":
                c.execute(
                    "INSERT INTO wallets (label, address) VALUES (%s, %s) ON CONFLICT (label) DO UPDATE SET address=EXCLUDED.address",
                    (str(label), str(address)),
                )
            else:
                c.execute(
                    "INSERT OR REPLACE INTO wallets (label, address) VALUES (?, ?)",
                    (str(label), str(address)),
                )
    return load_wallets()


def upsert_wallet(label, address):
    init()
    with db() as (kind, c):
        if kind == "pg":
            c.execute(
                "INSERT INTO wallets (label, address) VALUES (%s, %s) ON CONFLICT (label) DO UPDATE SET address=EXCLUDED.address",
                (str(label), str(address)),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO wallets (label, address) VALUES (?, ?)",
                (str(label), str(address)),
            )


def delete_wallet(label):
    init()
    with db() as (kind, c):
        ph = "%s" if kind == "pg" else "?"
        c.execute(f"DELETE FROM wallets WHERE label={ph}", (str(label),))
        return c.rowcount if hasattr(c, "rowcount") else True


# Backward-compatible flag refreshed after init / reconnect attempts.
USE_PG = False


def refresh_use_pg():
    global USE_PG
    USE_PG = use_postgres()
    return USE_PG
