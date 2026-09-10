"""
يتابع الصفقات المفتوحة:
- وقف خسارة
- 3 أهداف تيك بروفت (بيع تدريجي)
- رفع وقف الخسارة بعد كل هدف (Trailing / Break-even)
"""
import asyncio
import logging
import time

import settings
import trades_db
import mexc_trade

log = logging.getLogger(__name__)


def _update_sl(trade_id, new_sl):
    """يرفع وقف الخسارة في قاعدة البيانات"""
    with trades_db.db() as c:
        c.execute("UPDATE trades SET stop_loss=? WHERE id=?", (new_sl, trade_id))


async def check_positions(app):
    trades = trades_db.get_open_trades()
    if not trades:
        return

    for t in trades:
        symbol = t["symbol"]
        pair = mexc_trade.resolve_symbol(symbol)
        price = mexc_trade.get_price(pair)
        if price <= 0:
            log.warning("No price for %s", pair)
            continue

        entry = t["entry_price"]
        qty = t["quantity"]
        trade_id = t["id"]
        sl = t["stop_loss"]

        # ---------- Stop Loss ----------
        if sl and price <= sl:
            log.info("SL hit for #%s %s @ %s (SL=%s)", trade_id, symbol, price, sl)
            # استخدم الرصيد الفعلي لو متاح عشان نتجنب quantity scale errors
            real_bal = mexc_trade.get_base_balance(pair)
            sell_qty = real_bal if real_bal > 0 else qty
            result = mexc_trade.market_sell(pair, sell_qty)
            trades_db.close_trade(trade_id, price, note="StopLoss")
            await _notify(
                app,
                f"🛑 <b>وقف خسارة</b>\n"
                f"#{trade_id} <b>{symbol}</b>\n"
                f"دخول: {entry:.6g} → خروج: {price:.6g}\n"
                f"{result}",
            )
            continue

        # ---------- Take Profits + رفع الـ SL ----------
        # المستوى: (رقم الهدف، سعر الهدف، هل اتحقق قبل كده)
        levels = [
            (1, t["tp1"], t["tp1_hit"]),
            (2, t["tp2"], t["tp2_hit"]),
            (3, t["tp3"], t["tp3_hit"]),
        ]

        for level, tp_price, already_hit in levels:
            if already_hit or not tp_price:
                continue
            if price < tp_price:
                continue

            # بيع جزء من الكمية (تقريباً ثلث)
            parts_left = 4 - level  # 3, 2, 1
            sell_qty = round(qty / parts_left, 6)
            if sell_qty <= 0:
                continue

            log.info("TP%s hit for #%s %s @ %s", level, trade_id, symbol, price)
            result = mexc_trade.market_sell(pair, sell_qty)
            trades_db.mark_tp(trade_id, level)

            # === رفع وقف الخسارة (الأهم) ===
            if level == 1:
                # بعد الهدف 1 → SL على سعر الدخول (Break Even)
                new_sl = entry
                reason = "رفع SL لسعر الدخول (Break Even)"
            elif level == 2:
                # بعد الهدف 2 → SL على سعر الهدف 1
                new_sl = t["tp1"] if t["tp1"] else entry
                reason = "رفع SL لمستوى الهدف 1"
            else:
                # بعد الهدف 3 → قفل الصفقة بالكامل
                new_sl = None
                reason = "كل الأهداف تحققت"

            if level < 3 and new_sl:
                # نتأكد إن الـ SL الجديد أعلى من القديم (نرفع فقط)
                if sl is None or new_sl > sl:
                    _update_sl(trade_id, new_sl)
                    sl = new_sl

            await _notify(
                app,
                f"🎯 <b>هدف {level} تحقق</b>\n"
                f"#{trade_id} <b>{symbol}</b> @ {price:.6g}\n"
                f"كمية مباعة: {sell_qty}\n"
                f"🔒 {reason}\n"
                f"وقف الخسارة الجديد: <b>{new_sl if new_sl else '— (صفقة مقفلة)'}</b>\n"
                f"{result}",
            )

            if level == 3:
                trades_db.close_trade(trade_id, price, note="TP3 complete")
                await _notify(app, f"✅ الصفقة #{trade_id} <b>{symbol}</b> اتقفلت بالكامل")

            # بعد ما نبيع جزء، نقلل الكمية المحلية للدورة دي
            qty = max(0, qty - sell_qty)


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
    log.info("Position manager started (with trailing SL)")
    await asyncio.sleep(15)
    while True:
        try:
            if settings.get("monitoring_enabled"):
                await check_positions(app)
        except Exception as e:
            log.exception("position_loop error: %s", e)
        await asyncio.sleep(20)
