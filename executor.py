"""
تنفيذ الشراء التلقائي عند فرصة سحب جماعي
"""
import logging
import settings
import trades_db
import mexc_trade

log = logging.getLogger(__name__)


def try_auto_buy(symbol, contract="", chain="", note="سحب جماعي"):
    """
    يحاول يشتري لو الشروط متحققة.
    يرجع (ok: bool, message: str)
    """
    s = settings.load()
    if not s.get("auto_buy_enabled"):
        return False, "الشراء التلقائي معطّل"
    if not s.get("monitoring_enabled"):
        return False, "الرصد متوقف"

    if trades_db.count_open() >= s["max_open_trades"]:
        return False, f"وصلت لأقصى صفقات مفتوحة ({s['max_open_trades']})"

    # لو في صفقة مفتوحة على نفس العملة — نشتري برضو حسب طلبك
    # (مش هنمنع)

    pair = mexc_trade.resolve_symbol(symbol)
    price = mexc_trade.get_price(pair)
    if price <= 0:
        return False, f"مفيش سعر لـ {pair} على MEXC (قد تكون مش مدرجة)"

    size = float(s["trade_size_usd"])
    balance = mexc_trade.get_balance("USDT")
    if balance < size:
        return False, f"رصيد USDT غير كافٍ ({balance:.2f}$ < {size}$)"

    # حساب الأسعار
    sl_pct = float(s["stop_loss_pct"]) / 100.0
    tp1_pct = float(s["tp1_pct"]) / 100.0
    tp2_pct = float(s["tp2_pct"]) / 100.0
    tp3_pct = float(s["tp3_pct"]) / 100.0

    stop_loss = price * (1 + sl_pct)
    tp1 = price * (1 + tp1_pct)
    tp2 = price * (1 + tp2_pct)
    tp3 = price * (1 + tp3_pct)

    # تنفيذ الشراء
    order = mexc_trade.market_buy(pair, size)
    if order.get("error") or (order.get("code") and order.get("code") != 200):
        return False, f"فشل أمر الشراء: {order}"

    # تقدير الكمية
    qty = size / price
    # لو الـ API رجّع executedQty نستخدمه
    try:
        if order.get("executedQty"):
            qty = float(order["executedQty"])
        elif order.get("cummulativeQuoteQty"):
            qty = float(order["cummulativeQuoteQty"]) / price
    except (TypeError, ValueError):
        pass

    trade_id = trades_db.open_trade(
        symbol=symbol.upper(),
        contract=contract,
        chain=chain,
        entry_price=price,
        quantity=qty,
        size_usd=size,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        note=note,
    )

    msg = (
        f"✅ تم الشراء #{trade_id}\n"
        f"<b>{symbol}</b> @ {price:.6g}\n"
        f"حجم: {size}$ | كمية ≈ {qty:.4f}\n"
        f"SL: {stop_loss:.6g}\n"
        f"TP: {tp1:.6g} / {tp2:.6g} / {tp3:.6g}"
    )
    log.info("Auto-buy success: %s", msg)
    return True, msg
