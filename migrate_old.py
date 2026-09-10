"""
يستورد الصفقات من جداول البوت القديم (أسماء عربية)
شغّله مرة عند الإقلاع لو لقى جداول قديمة.
"""
import logging
import trades_db

log = logging.getLogger(__name__)


def _rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return [dict(r) for r in cur.fetchall()]


def import_old_trades():
    """يحاول يقرأ من جدول الصفقات القديم وينقل المفتوحة لجدول trades"""
    if not trades_db.use_postgres():
        log.info("migrate: no Postgres, skip")
        return 0

    imported = 0
    try:
        with trades_db.db() as (kind, cur):
            if kind != "pg":
                return 0

            # اكتشاف الجداول
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public'
            """)
            tables = [r["table_name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
            log.info("migrate: tables found: %s", tables)

            # جدول الصفقات بالعربي أو الإنجليزي القديم
            src = None
            for name in ("الصفقات", "trades", "Trades", "positions"):
                if name in tables:
                    src = name
                    break
            if not src:
                log.info("migrate: no old trades table")
                return 0

            # أعمدة الجدول
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
            """, (src,))
            cols = { (r["column_name"] if isinstance(r, dict) else r[0]) for r in cur.fetchall() }
            log.info("migrate: columns in %s: %s", src, cols)

            # نقرأ كل الصفوف
            cur.execute(f'SELECT * FROM "{src}"')
            rows = [dict(r) for r in cur.fetchall()]
            log.info("migrate: %s rows in old table", len(rows))

            # لو جدولنا trades فاضي من ناحية open، نملأه
            existing_open = trades_db.get_open_trades()
            existing_syms = {str(t.get("symbol", "")).upper() for t in existing_open}

            for r in rows:
                # تخمين الحقول الشائعة
                symbol = (
                    r.get("symbol") or r.get("العملة") or r.get("token")
                    or r.get("coin") or r.get("pair") or r.get("الاسم")
                )
                if not symbol:
                    continue
                symbol = str(symbol).replace("USDT", "").replace("/", "").strip().upper()

                status = str(r.get("status") or r.get("الحالة") or r.get("state") or "open").lower()
                # اعتبر المفتوح فقط
                if status in ("closed", "close", "مغلقة", "مغلق", "done", "sold"):
                    continue

                entry = r.get("entry_price") or r.get("entry") or r.get("سعر_الدخول") or r.get("price") or r.get("سعر")
                qty = r.get("quantity") or r.get("qty") or r.get("الكمية") or r.get("amount") or 0
                size = r.get("size_usd") or r.get("size") or r.get("الحجم") or r.get("usd") or r.get("المبلغ") or 0
                sl = r.get("stop_loss") or r.get("sl") or r.get("وقف") or r.get("stop")
                tp1 = r.get("tp1") or r.get("هدف1") or r.get("target1")
                tp2 = r.get("tp2") or r.get("هدف2") or r.get("target2")
                tp3 = r.get("tp3") or r.get("هدف3") or r.get("target3")
                contract = r.get("contract") or r.get("address") or r.get("العقد") or ""
                chain = r.get("chain") or r.get("الشبكة") or ""

                try:
                    entry = float(entry or 0)
                except Exception:
                    entry = 0
                try:
                    qty = float(qty or 0)
                except Exception:
                    qty = 0
                try:
                    size = float(size or 0)
                except Exception:
                    size = 0

                if entry <= 0:
                    continue
                if symbol in existing_syms:
                    continue

                # قيم افتراضية للأهداف لو ناقصة
                if not sl:
                    sl = entry * 0.92
                if not tp1:
                    tp1 = entry * 1.05
                if not tp2:
                    tp2 = entry * 1.10
                if not tp3:
                    tp3 = entry * 1.15
                if size <= 0 and qty > 0:
                    size = qty * entry
                if qty <= 0 and size > 0:
                    qty = size / entry

                tid = trades_db.open_trade(
                    symbol=symbol,
                    contract=str(contract or ""),
                    chain=str(chain or ""),
                    entry_price=entry,
                    quantity=qty,
                    size_usd=size or 5.0,
                    stop_loss=float(sl),
                    tp1=float(tp1),
                    tp2=float(tp2),
                    tp3=float(tp3),
                    note="imported_from_old",
                )
                existing_syms.add(symbol)
                imported += 1
                log.info("migrate: imported %s as #%s", symbol, tid)

    except Exception as e:
        log.exception("migrate failed: %s", e)
        return imported

    log.info("migrate: done imported=%s", imported)
    return imported
