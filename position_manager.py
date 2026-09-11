"""
متابعة الصفقات + خروج طارئ عند أي خلل
"""
import asyncio
import logging
import time

import settings
import trades_db
import mexc_trade

log = logging.getLogger(__name__)

_no_price_count = {}       # trade_id -> consecutive failures
_sell_fail_count = {}      # trade_id -> consecutive sell failures
NO_PRICE_LIMIT = 8         # ~2-3 دقايق
SELL_FAIL_LIMIT = 3        # 3 محاولات بيع فاشلة


async def _exchange_call(fn, *args, **kwargs):
    """Run blocking exchange I/O away from Telegram's asyncio event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _update_sl(trade_id, new_sl):
    with trades_db.db() as (kind, c):
        placeholder = "%s" if kind == "pg" else "?"
        c.execute(
            f"UPDATE trades SET stop_loss={placeholder} WHERE id={placeholder}",
            (new_sl, trade_id),
        )


def _update_quantity(trade_id, new_qty):
    """Persist remaining quantity after a partial take-profit sell."""
    with trades_db.db() as (kind, c):
        placeholder = "%s" if kind == "pg" else "?"
        c.execute(
            f"UPDATE trades SET quantity={placeholder} WHERE id={placeholder}",
            (float(new_qty), trade_id),
        )


def _pnl_pct(entry, exit_price):
    if not entry:
        return 0.0
    return ((exit_price - entry) / entry) * 100.0


def _trade_quantity(trade):
    """Read quantity from current and legacy trade schemas."""
    return float(
        trade.get("quantity")
        or trade.get("qty")
        or trade.get("amount")
        or 0
    )


def _pnl_usd(entry, exit_price, size_usd, fraction=1.0):
    return size_usd * fraction * (_pnl_pct(entry, exit_price) / 100.0)


def _is_bad_order(result):
    if not isinstance(result, dict):
        return True, "رد غير مفهوم من المنصة"
    if result.get("error"):
        return True, str(result.get("error"))
    code = result.get("code")
    if code is not None and int(code) not in (0, 200):
        return True, f"{result.get('msg', code)}"
    return False, ""


async def emergency_close(app, t, reason, price=None):
    """خروج طارئ: بيع فوري + تقرير + قفل الصفقة.
    لو الرصيد الفعلي صفر (أو dust) حتى لو فشل الأمر → نقفل الصفقة في الداتابيز فوراً.
    """
    trade_id = t["id"]
    symbol = t["symbol"]
    entry = float(t["entry_price"] or 0)
    size = float(t.get("size_usd") or 0)
    pair = mexc_trade.resolve_symbol(symbol)

    if price is None or price <= 0:
        price = await _exchange_call(mexc_trade.get_price, pair) or entry

    sell_result = None
    sell_ok = False
    real_bal = 0.0
    try:
        real_bal = await _exchange_call(mexc_trade.get_base_balance, pair)
        qty = real_bal if real_bal > 0 else float(t.get("quantity") or 0)
        if qty > 0:
            sell_result = await _exchange_call(mexc_trade.market_sell, pair, qty)
            bad, _ = _is_bad_order(sell_result)
            sell_ok = not bad
            # بعد محاولة البيع نعيد قراءة الرصيد للتأكد
            if not sell_ok:
                real_bal = await _exchange_call(mexc_trade.get_base_balance, pair)
        else:
            sell_result = {"info": "no balance to sell"}
            sell_ok = True  # مفيش رصيد = اعتبرها اتقفلت
    except Exception as e:
        sell_result = {"error": str(e)}
        sell_ok = False
        log.exception("emergency sell failed for %s", symbol)
        try:
            real_bal = await _exchange_call(mexc_trade.get_base_balance, pair)
        except Exception:
            real_bal = -1

    # لو الرصيد الفعلي شبه صفر → نعتبر الصفقة مقفلة حتى لو الأمر فشل (Oversold / scale / dust)
    DUST_THRESHOLD = 1e-8
    if not sell_ok and real_bal is not None and real_bal <= DUST_THRESHOLD:
        sell_ok = True
        sell_result = sell_result or {}
        if isinstance(sell_result, dict):
            sell_result["forced_close"] = f"balance={real_bal} <= dust → closed in DB"
        log.info("Force-closing #%s %s because live balance is dust/zero (%.10f)", trade_id, symbol, real_bal)

    pnl_p = _pnl_pct(entry, price) if price and entry else 0
    pnl_u = _pnl_usd(entry, price, size, 1.0) if price and entry else 0

    if sell_ok:
        trades_db.close_trade(
            trade_id,
            price or entry,
            note=f"EMERGENCY: {reason}",
        )
        log.info("Trade #%s %s closed in DB (emergency)", trade_id, symbol)
    else:
        log.error("Keeping trade #%s open because emergency sell failed (balance=%.8f)", trade_id, real_bal)
    _no_price_count.pop(trade_id, None)
    _sell_fail_count.pop(trade_id, None)

    status = "تم البيع / اتقفلت" if sell_ok else "فشل البيع — راجع المنصة يدوي"
    msg = (
        f"🚨 <b>خروج طارئ</b>\n"
        f"<b>{symbol}</b>\n"
        f"السبب: {reason}\n"
        f"النتيجة: {status}\n"
        f"تقدير PnL: <b>{pnl_u:+.2f}$</b> ({pnl_p:+.1f}%)\n"
        f"<code>{sell_result}</code>"
    )
    await _notify(app, msg)
    log.warning("EMERGENCY close #%s %s reason=%s sell_ok=%s", trade_id, symbol, reason, sell_ok)
    return sell_ok


async def check_positions(app):
    trades = trades_db.get_open_trades()
    if not trades:
        return

    for t in trades:
        try:
            await _check_one(app, t)
        except Exception as e:
            log.exception("check error #%s: %s", t.get("id"), e)
            try:
                await emergency_close(app, t, f"استثناء غير متوقع: {e}")
            except Exception:
                log.exception("emergency close also failed")


async def _check_one(app, t):
    symbol = t["symbol"]
    pair = mexc_trade.resolve_symbol(symbol)
    trade_id = t["id"]
    entry = float(t["entry_price"] or 0)
    qty = _trade_quantity(t)
    sl = t["stop_loss"]
    size = float(t.get("size_usd") or 0)
    tp1_hit = int(t.get("tp1_hit") or 0)
    tp2_hit = int(t.get("tp2_hit") or 0)
    tp3_hit = int(t.get("tp3_hit") or 0)
    targets_hit = tp1_hit + tp2_hit + tp3_hit

    # ---- سعر ----
    try:
        price = await _exchange_call(mexc_trade.get_price, pair)
    except Exception as e:
        price = 0
        log.warning("price exception %s: %s", pair, e)

    if price <= 0:
        _no_price_count[trade_id] = _no_price_count.get(trade_id, 0) + 1
        if _no_price_count[trade_id] >= NO_PRICE_LIMIT:
            await emergency_close(
                app, t,
                f"لا يوجد سعر / زوج غير صالح ({pair}) بعد {NO_PRICE_LIMIT} محاولات",
                price=entry,
            )
        return
    else:
        _no_price_count.pop(trade_id, None)

    # ---- وقف الخسارة ----
    if sl and price <= float(sl):
        log.info("SL hit for #%s %s @ %s (SL=%s)", trade_id, symbol, price, sl)
        real_bal = await _exchange_call(mexc_trade.get_base_balance, pair)
        sell_qty = real_bal if real_bal > 0 else qty
        result = await _exchange_call(mexc_trade.market_sell, pair, sell_qty)
        bad, why = _is_bad_order(result)
        if bad:
            _sell_fail_count[trade_id] = _sell_fail_count.get(trade_id, 0) + 1
            try:
                bal_after = await _exchange_call(mexc_trade.get_base_balance, pair)
            except Exception:
                bal_after = -1
            if bal_after is not None and bal_after <= 1e-8:
                log.info("SL sell failed but balance dust → force close #%s", trade_id)
                trades_db.close_trade(trade_id, price, note=f"StopLoss forced (balance dust): {why}")
                _sell_fail_count.pop(trade_id, None)
                await _notify(app, f"تم إغلاق <b>{symbol}</b> (وقف خسارة + رصيد صفر)\n{why}")
                return
            if _sell_fail_count[trade_id] >= SELL_FAIL_LIMIT:
                await emergency_close(app, t, f"فشل بيع وقف الخسارة: {why}", price=price)
            return

        remaining_frac = max(0.15, 1.0 - (targets_hit / 3.0))
        pnl_p = _pnl_pct(entry, price)
        pnl_u = _pnl_usd(entry, price, size, remaining_frac)
        trades_db.close_trade(trade_id, price, note="StopLoss")
        _sell_fail_count.pop(trade_id, None)

        if targets_hit == 0:
            msg = (
                f"للأسف تم ضرب وقف الخسارة\n"
                f"<b>{symbol}</b>\n"
                f"الخسارة ≈ <b>{pnl_u:.2f}$</b> ({pnl_p:.1f}%)"
            )
        else:
            msg = (
                f"تم ضرب الاستوب بعد ما اتحقق <b>{targets_hit}</b> هدف\n"
                f"<b>{symbol}</b>\n"
                f"نتيجة الجزء المتبقي ≈ <b>{pnl_u:.2f}$</b> ({pnl_p:.1f}%)\n"
                f"الأهداف اللي اتحققت قبل الاستوب: {targets_hit}/3"
            )
        await _notify(app, msg)
        return

    # ---- أهداف ----
    levels = [
        (1, t["tp1"], tp1_hit),
        (2, t["tp2"], tp2_hit),
        (3, t["tp3"], tp3_hit),
    ]

    for level, tp_price, already_hit in levels:
        if already_hit or not tp_price:
            continue
        if price < float(tp_price):
            continue

        parts_left = 4 - level
        sell_qty = round(qty / parts_left, 6)
        if sell_qty <= 0:
            continue

        # Prefer live exchange balance so partial sells stay accurate after restarts.
        real_bal = await _exchange_call(mexc_trade.get_base_balance, pair)
        if real_bal > 0:
            sell_qty = round(min(sell_qty, real_bal / max(1, parts_left)), 6)
            if level == 3:
                sell_qty = real_bal  # final TP: dump whatever is left

        if sell_qty <= 0:
            continue

        log.info("TP%s hit for #%s %s @ %s qty=%s", level, trade_id, symbol, price, sell_qty)
        result = await _exchange_call(mexc_trade.market_sell, pair, sell_qty)
        bad, why = _is_bad_order(result)
        if bad:
            _sell_fail_count[trade_id] = _sell_fail_count.get(trade_id, 0) + 1
            # بعد الفشل نتحقق من الرصيد الفعلي: لو صفر → نقفل فوراً
            try:
                bal_after = await _exchange_call(mexc_trade.get_base_balance, pair)
            except Exception:
                bal_after = -1
            if bal_after is not None and bal_after <= 1e-8:
                log.info("TP%s sell failed but balance is dust → force close #%s", level, trade_id)
                trades_db.close_trade(trade_id, price, note=f"TP{level} forced (balance dust after sell fail)")
                _sell_fail_count.pop(trade_id, None)
                await _notify(
                    app,
                    f"تم إغلاق <b>{symbol}</b> تلقائياً بعد فشل البيع (رصيد صفر)\n"
                    f"الهدف {level} | السبب: {why}",
                )
                return
            if _sell_fail_count[trade_id] >= SELL_FAIL_LIMIT:
                await emergency_close(app, t, f"فشل بيع الهدف {level}: {why}", price=price)
            return

        trades_db.mark_tp(trade_id, level)
        _sell_fail_count.pop(trade_id, None)

        frac = 1.0 / 3.0
        pnl_u = _pnl_usd(entry, price, size, frac)
        pnl_p = _pnl_pct(entry, price)

        if level == 1:
            new_sl = entry
        elif level == 2:
            new_sl = t["tp1"] if t["tp1"] else entry
        else:
            new_sl = None

        if level < 3 and new_sl:
            if sl is None or float(new_sl) > float(sl or 0):
                _update_sl(trade_id, new_sl)
                sl = new_sl

        await _notify(
            app,
            f"مبروك 🎯 تم تحقيق هدف {level}\n"
            f"<b>{symbol}</b>\n"
            f"ربح صافي ≈ <b>+{pnl_u:.2f}$</b> ({pnl_p:.1f}%)",
        )

        if level == 3:
            trades_db.close_trade(trade_id, price, note="TP3 complete")
            await _notify(app, f"✅ <b>{symbol}</b> اتقفلت — كل الأهداف تحققت")
        else:
            # Keep remaining size in DB so restarts / next checks stay correct.
            # Prefer live balance after successful partial sell.
            try:
                bal_after = await _exchange_call(mexc_trade.get_base_balance, pair)
                remaining = max(0.0, bal_after)
            except Exception:
                remaining = max(0.0, (real_bal if real_bal > 0 else qty) - sell_qty)
            _update_quantity(trade_id, remaining)
            qty = remaining


async def _notify(app, text):
    from config import TELEGRAM_CHAT_ID
    try:
        chat = TELEGRAM_CHAT_ID
        if not chat:
            return
        await app.bot.send_message(chat_id=chat, text=text, parse_mode="HTML")
    except Exception as e:
        log.warning("notify failed: %s", e)


async def position_loop(app):
    log.info("Position manager started (emergency exit enabled)")
    await asyncio.sleep(15)
    while True:
        try:
            if settings.get("monitoring_enabled"):
                await check_positions(app)
        except Exception as e:
            log.exception("position_loop error: %s", e)
        await asyncio.sleep(20)
