"""
تنفيذ الشراء التلقائي عند فرصة
"""
import logging
import settings
import trades_db
import mexc_trade

log = logging.getLogger(__name__)


def try_auto_buy(symbol, contract="", chain="", note="فرصة"):
    """
    يحاول يشتري لو الشروط متحققة.
    يرجع (ok: bool, message: str)
    """
    s = settings.load()
    if not s.get("auto_buy_enabled"):
        return False, "الشراء التلقائي معطّل"
    if not s.get("monitoring_enabled"):
        return False, "الرصد متوقف"

    max_open = int(s.get("max_open_trades") or 3)
    open_count = trades_db.count_open()
    if open_count >= max_open:
        return False, f"وصلت لحد الصفقات المفتوحة ({open_count}/{max_open})"

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
    if not isinstance(order, dict):
        return False, f"فشل أمر الشراء: {order}"
    if order.get("error"):
        return False, f"فشل أمر الشراء: {order}"
    if order.get("code") and int(order.get("code", 0)) not in (0, 200):
        return False, f"فشل أمر الشراء: {order}"

    # لازم يكون فيه تنفيذ فعلي
    executed = 0.0
    quote_filled = 0.0
    try:
        executed = float(order.get("executedQty") or 0)
        quote_filled = float(order.get("cummulativeQuoteQty") or 0)
    except (TypeError, ValueError):
        pass

    if executed <= 0 and quote_filled <= 0:
        # بعض الردود الناجحة بتكون status NEW ثم تتنفذ — لو status ملغي نرفض
        status = str(order.get("status", "")).upper()
        if status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
            return False, f"الأمر اتلغى بدون تنفيذ: {order}"
        # لو مفيش executedQty خالص اعتبره فشل
        if not order.get("orderId"):
            return False, f"فشل أمر الشراء (لا يوجد orderId): {order}"

    qty = executed if executed > 0 else (quote_filled / price if quote_filled > 0 else size / price)
    if qty <= 0:
        return False, f"كمية منفذة صفر: {order}"

    # استخدم متوسط السعر الفعلي لو موجود
    try:
        if quote_filled > 0 and executed > 0:
            price = quote_filled / executed
    except Exception:
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
        f"<b>{symbol}</b> | حجم {size:.0f}$"
    )
    log.info("Auto-buy success: %s", msg)
    return True, msg
