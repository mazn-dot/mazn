"""
يتابع الصفقات المفتوحة:
- وقف خسارة
- 3 أهداف تيك بروفت (بيع تدريجي)
- رفع وقف الخسارة بعد كل هدف (Trailing / Break-even)
- رسائل مختصرة وواضحة
"""
import asyncio
import logging
import time

import settings
import trades_db
import mexc_trade

log = logging.getLogger(__name__)

_no_price_count = {}  # trade_id -> consecutive failures


def _update_sl(trade_id, new_sl):
    with trades_db.db() as c:
        c.execute("UPDATE trades SET stop_loss=? WHERE id=?", (new_sl, trade_id))


def _pnl_pct(entry, exit_price):
    if not entry:
        return 0.0
    return ((exit_price - entry) / entry) * 100.0


def _pnl_usd(entry, exit_price, size_usd, fraction=1.0):
    return size_usd * fraction * (_pnl_pct(entry, exit_price) / 100.0)


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
            _no_price_count[trade_id] = _no_price_count.get(trade_id, 0) + 1
            # بعد 15 محاولة فاشلة (~5 دقايق) نقفل الصفقة كـ invalid
            if _no_price_count[trade_id] >= 15:
                trades_db.close_trade(trade_id, entry, note="invalid_symbol")
                await _notify(app, f"⚠️ <b>{symbol}</b> اتقفلت — الزوج غير متاح على MEXC")
                _no_price_count.pop(trade_id, None)
            continue
        else:
            _no_price_count.pop(trade_id, None)

        entry = t["entry_price"]
        qty = t["quantity"]
        trade_id = t["id"]
        sl = t["stop_loss"]
        size = float(t.get("size_usd") or 0)
        tp1_hit = t.get("tp1_hit") or 0
        tp2_hit = t.get("tp2_hit") or 0
        tp3_hit = t.get("tp3_hit") or 0
        targets_hit = int(tp1_hit) + int(tp2_hit) + int(tp3_hit)

        # ---------- Stop Loss ----------
        if sl and price <= sl:
            log.info("SL hit for #%s %s @ %s (SL=%s)", trade_id, symbol, price, sl)
            real_bal = mexc_trade.get_base_balance(pair)
            sell_qty = real_bal if real_bal > 0 else qty
            result = mexc_trade.market_sell(pair, sell_qty)
            pnl_p = _pnl_pct(entry, price)
            # تقدير الربح/الخسارة على المتبقي
            remaining_frac = max(0.15, 1.0 - (targets_hit / 3.0))
            pnl_u = _pnl_usd(entry, price, size, remaining_frac)
            trades_db.close_trade(trade_id, price, note="StopLoss")

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
            continue

        # ---------- Take Profits ----------
        levels = [
            (1, t["tp1"], tp1_hit),
            (2, t["tp2"], tp2_hit),
            (3, t["tp3"], tp3_hit),
        ]

        for level, tp_price, already_hit in levels:
            if already_hit or not tp_price:
                continue
            if price < tp_price:
                continue

            parts_left = 4 - level
            sell_qty = round(qty / parts_left, 6)
            if sell_qty <= 0:
                continue

            log.info("TP%s hit for #%s %s @ %s", level, trade_id, symbol, price)
            result = mexc_trade.market_sell(pair, sell_qty)
            trades_db.mark_tp(trade_id, level)

            frac = 1.0 / 3.0
            pnl_u = _pnl_usd(entry, price, size, frac)
            pnl_p = _pnl_pct(entry, price)

            # رفع SL
            if level == 1:
                new_sl = entry
            elif level == 2:
                new_sl = t["tp1"] if t["tp1"] else entry
            else:
                new_sl = None

            if level < 3 and new_sl:
                if sl is None or new_sl > sl:
                    _update_sl(trade_id, new_sl)
                    sl = new_sl

            msg = (
                f"مبروك 🎯 تم تحقيق هدف {level}\n"
                f"<b>{symbol}</b>\n"
                f"ربح صافي ≈ <b>+{pnl_u:.2f}$</b> ({pnl_p:.1f}%)"
            )
            await _notify(app, msg)

            if level == 3:
                trades_db.close_trade(trade_id, price, note="TP3 complete")
                await _notify(app, f"✅ <b>{symbol}</b> اتقفلت — كل الأهداف تحققت")

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
    log.info("Position manager started (clean messages)")
    await asyncio.sleep(15)
    while True:
        try:
            if settings.get("monitoring_enabled"):
                await check_positions(app)
        except Exception as e:
            log.exception("position_loop error: %s", e)
        await asyncio.sleep(20)
