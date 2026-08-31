"""
نقطة تشغيل البوت - تداول SPOT فقط، بالاعتماد بالكامل على توصيات قنوات تليجرام.

مفيش استراتيجية داخلية (EMA/RSI/ATR) خالص - كل صفقة بتتفتح بس لما توصية
توصل من قناة مراقَبة (bot/signal_listener.py) وتتقبل الفلاتر (رمز صالح،
سعر قريب من المذكور، رصيد كافي...). الدور الوحيد للحلقة هنا هو:

1. مراقبة كل مركز مفتوح (مهما كان مصدره) مقابل الستوب لوس وهدف الربح
   المحسوبين وقت فتح الصفقة، وقفله تلقائياً (بيع) لو السعر وصل لأي منهم.
2. التأكد من حد الخسارة اليومي المسموح.

فتح الصفقات نفسه بيحصل في signal_listener.py (مسار منفصل شغال بالتوازي).
"""
import logging
import time
import sys

from .config import Config
from .exchange import BitgetExchange
from .risk import RiskManager
from .database import Database
from .state import shared_state
from .telegram_control import TelegramController
from .signal_listener import SignalListener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


def _check_open_trade_exit(exchange, db, risk, telegram, trade: dict, current_price: float):
    """يتأكد هل نقفل مركز مفتوح (نبيع) بناءً على SL/TP، ويرجع True لو اتقفل.
    لو اتقفل بسبب تحقيق هدف ربح (مش ستوب) وكان جزء من مجموعة 3 أهداف،
    بيرفع الستوب لوس للأجزاء الباقية (تعادل بعد أول هدف، سعر أول هدف بعد
    تاني هدف... وهكذا)."""
    entry = float(trade["entry_price"])
    sl = trade["stop_loss"]
    tp = trade["take_profit"]

    hit_sl = sl is not None and current_price <= float(sl)
    hit_tp = tp is not None and current_price >= float(tp)

    if not (hit_sl or hit_tp):
        return False

    amount = float(trade["amount"])
    symbol = trade["symbol"]

    # تأكد إن الكمية فعلاً متاحة في المحفظة قبل البيع (في وضع التداول الحقيقي)
    if not shared_state.is_dry_run():
        available = exchange.fetch_base_balance(symbol)
        # لو الرصيد الفعلي أقل من الكمية المسجلة (بنسبة 1% هامش تقريب)، يبقى
        # الصفقة دي "وهمية" - سجلها في DB مش له رصيد حقيقي (تجربة قديمة أو
        # سجل تالف) - بنقفلها من السجل من غير بيع عشان ما نكملش في حلقة أخطاء
        if available < amount * 0.99:
            db.close_fake_trade(
                trade["id"],
                f"سجل وهمي - الرصيد الفعلي في المحفظة ({available}) أقل من الكمية المسجلة ({amount})",
            )
            telegram.notify(
                f"🧹 تم تنظيف سجل وهمي: {symbol} صف #{trade['id']}\n"
                "الكمية المسجلة مش موجودة في المحفظة فعلًا - اتشالت من السجل."
            )
            logger.warning(f"{symbol}: تم إغلاق صفقة وهمية #{trade['id']} - مش موجودة بالمحفظة.")
            return False
        amount = min(amount, available)

    if amount <= 0:
        logger.warning(f"{symbol}: كمية غير صالحة للبيع عند القفل، تم التجاهل.")
        return False

    exchange.create_market_sell(symbol, amount)
    pnl_usdt = (current_price - entry) * amount
    db.close_trade(trade["id"], current_price, pnl_usdt)

    balance = exchange.fetch_balance_usdt()
    risk.register_trade_result(pnl_usdt, balance)

    # ---- تنظيف أوامر المنصة الخاصة بالـ leg اللي اتقفل ----
    _cancel_trade_plan_orders(exchange, db, symbol, trade)

    reason = "🎯 تيك بروفيت" if hit_tp else "🛑 ستوب لوس"
    telegram.notify(
        f"{reason} - تم قفل المركز #{trade['id']}\n"
        f"{symbol} | دخول={entry:.6g} | خروج={current_price:.6g}\n"
        f"الربح/الخسارة: {pnl_usdt:+.2f} USDT"
    )
    logger.info(f"{symbol}: تم قفل المركز #{trade['id']} ({reason}) | pnl={pnl_usdt:+.2f}")

    # ---- رفع الستوب لوس للأجزاء الباقية في نفس المجموعة بعد تحقيق هدف ----
    group_id = trade.get("group_id")
    leg_index = trade.get("leg_index")
    if hit_tp and group_id and leg_index:
        if leg_index == 1:
            new_stop = entry  # أول هدف تحقق -> رفع الستوب لسعر الدخول (تعادل)
            stop_desc = "سعر الدخول (تعادل)"
        else:
            # رفع الستوب لسعر الهدف اللي قبله (تأمين ربح إضافي مع كل هدف)
            new_stop = db.get_leg_take_profit(group_id, leg_index - 1)
            stop_desc = f"سعر الهدف {leg_index - 1}"

        if new_stop is not None:
            db.raise_group_stop_loss(group_id, exclude_trade_id=trade["id"], new_stop=new_stop)
            logger.info(f"{symbol}: تم رفع الستوب لوس للأجزاء الباقية في المجموعة إلى {new_stop:.6g} ({stop_desc})")
            telegram.notify(
                f"🔧 تم رفع الستوب لوس للأجزاء الباقية من {symbol}\n"
                f"القيمة الجديدة: {new_stop:.6g} ({stop_desc})"
            )
            # تحديث أمر SL على المنصة نفسها بالستوب الجديد (طبقة الحماية الخارجية)
            _sync_group_sl_on_platform(exchange, db, symbol, group_id, trade["id"], new_stop)

    return True


def _cancel_trade_plan_orders(exchange, db, symbol: str, trade: dict):
    """يلغي أوامر المنصة الشرطية المرتبطة بصفقة اتقفلت (أمر TP للـ leg + أمر SL المشترك)."""
    group_id = trade.get("group_id")
    if not group_id:
        return
    try:
        cancelled = set()
        for row in db.get_group_plan_order_ids(group_id):
            for oid in (row["plan_order_ids"] or []) or []:
                if oid and oid not in cancelled:
                    exchange.cancel_plan_order(symbol, oid)
                    cancelled.add(oid)
        # أمر SL المشترك (بيتسجل مع أكتر من leg - يُلغى مرة واحدة بس)
        for row in db.get_group_plan_order_ids(group_id):
            oid = row.get("sl_plan_order_id")
            if oid and oid not in cancelled:
                exchange.cancel_plan_order(symbol, oid)
                cancelled.add(oid)
    except Exception as e:
        logger.warning(f"{symbol}: فشل إلغاء أوامر المنصة للصفقة #{trade['id']}: {e}")


def _sync_group_sl_on_platform(exchange, db, symbol: str, group_id: str, exclude_trade_id: int, new_stop: float):
    """بعد رفع الستوب في قاعدة البيانات، يحط أمر SL جديد على المنصة بالستوب الجديد
    على الكمية المتبقية الفعلية في المحفظة، ويربطه بصفقة group.
    ملاحظة: كل أمر من المنصة بيرجع بـ status مختلف، فبنحفظه مع أول صفقات المجموعة المفتوحة."""
    try:
        # حساب الكمية المتبقية: كل الـ legs المفتوحة الباقية
        remaining = 0.0
        remaining_trade_id = None
        for t in db.open_trades():
            if t.get("group_id") == group_id and t["id"] != exclude_trade_id:
                remaining += float(t["amount"])
                remaining_trade_id = remaining_trade_id or t["id"]
        if remaining <= 0 or remaining_trade_id is None:
            return
        order, errors = exchange.replace_sl_order(symbol, None, new_stop, round(remaining, 6))
        order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
        if order_id:
            db.save_plan_order_ids(remaining_trade_id, [], str(order_id))
            logger.info(f"{symbol}: تم تحديث أمر SL على المنصة إلى {new_stop:.6g} للكمية {remaining:.6g}")
        if errors:
            logger.warning(f"{symbol}: مشاكل جزئية عند تحديث أمر SL: {errors}")
    except Exception as e:
        logger.error(f"{symbol}: فشل تحديث أمر SL على المنصة: {e}")


def run():
    Config.validate()
    logger.info(f"بدء البوت (SPOT فقط - توصيات القنوات فقط) | الإعدادات: {shared_state.get_all()}")

    # عداد أخطاء لكل رمز - لو رمز فشل بيعه كتير (مشكلته من المنصة/الرصيد)
    # البوت يتوقف عن محاولات البيع له ويبلغك بدل ما يسبب فيض أخطاء
    _sell_error_counts: dict[str, int] = {}
    _SELL_ERROR_LIMIT = 10

    exchange = BitgetExchange()
    risk = RiskManager()
    db = Database()

    telegram = TelegramController(exchange, db, risk)
    telegram.start_in_background()
    time.sleep(2)

    # مراقبة قنوات التوصيات - المصدر الوحيد لفتح صفقات جديدة (شوف .env.example)
    signal_listener = SignalListener(exchange, db, risk, telegram)
    signal_listener.start_in_background()

    telegram.notify(
        "🚀 البوت اشتغل (تداول SPOT فقط - بالاعتماد الكامل على توصيات القنوات).\n"
        f"الوضع: {'🧪 تجربة (Dry Run)' if shared_state.is_dry_run() else '💰 تداول حقيقي'}\n"
        "اكتب /menu لعرض لوحة التحكم، أو /help لعرض كل الأوامر."
    )

    exchange.load_markets()

    poll_interval = int(shared_state.get("poll_interval_seconds"))

    while True:
        try:
            poll_interval = int(shared_state.get("poll_interval_seconds"))
            balance = exchange.fetch_balance_usdt()

            if not risk.trading_allowed(balance):
                logger.info("🛑 تم تجاوز حد الخسارة اليومي المسموح - مفيش صفقات جديدة (المراكز المفتوحة لسه بتتراقب).")

            open_trades = db.open_trades()
            if not open_trades:
                time.sleep(poll_interval)
                continue

            open_by_symbol = {}
            for t in open_trades:
                open_by_symbol.setdefault(t["symbol"], []).append(t)

            # ---- مراقبة كل مركز مفتوح (بيع عند SL/TP) ----
            for symbol, trades_for_symbol in open_by_symbol.items():
                if _sell_error_counts.get(symbol, 0) >= _SELL_ERROR_LIMIT:
                    # توقفنا عن محاولات البيع للرمز ده - مش هنتابع مراقبته
                    continue
                try:
                    current_price = exchange.fetch_last_price(symbol)
                    for trade in list(trades_for_symbol):
                        _check_open_trade_exit(exchange, db, risk, telegram, trade, current_price)
                except Exception as e:
                    logger.error(f"{symbol}: خطأ أثناء مراقبة المركز المفتوح: {e}")
                    # عدّ الأخطاء: لو الرمز فشل كتير متتالية، توقف عن محاولات بيعه
                    # وابلغ المستخدم عشان ما يتحولش لحلقة أخطاء بلا نهاية
                    _sell_error_counts[symbol] = _sell_error_counts.get(symbol, 0) + 1
                    if _sell_error_counts[symbol] >= _SELL_ERROR_LIMIT:
                        telegram.notify(
                            f"⚠ تم إيقاف محاولات مراقبة/بيع {symbol}\n"
                            f"السبب: فشل البيع { _SELL_ERROR_LIMIT} مرات متتالية "
                            "(الأرجح مشكلة من المنصة أو رصيد - راجع صفقاتك على Bitget يدويًا)."
                        )
                        logger.error(
                            f"{symbol}: تجاوز حد الأخطاء - توقف عن محاولات البيع تلقائيًا."
                        )

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            logger.info("إيقاف يدوي للبوت.")
            sys.exit(0)
        except Exception as e:
            logger.exception(f"خطأ غير متوقع في الحلقة الرئيسية: {e}")
            telegram.notify(f"⚠️ خطأ في البوت: {e}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    run()
