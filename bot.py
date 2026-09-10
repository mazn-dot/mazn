import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import wallets as store
import settings
import trades_db
from executor import try_auto_buy
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIME_PERIODS
from formatter import (
    format_best_opportunities,
    format_clean_opportunity,
    format_discovery,
    format_opportunity,
    format_report,
    format_whales,
)
from tracker import (
    find_whales_for_token,
    get_best_opportunities,
    get_clean_opportunity,
    get_opportunity,
    get_report,
    get_top_counterparties,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
KW = {"parse_mode": "HTML", "disable_web_page_preview": True}

ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def authorized(update):
    if not TELEGRAM_CHAT_ID:
        return True
    chat = update.effective_chat
    return bool(chat and str(chat.id) == TELEGRAM_CHAT_ID)


def order_failed(order):
    """Return True when an exchange response cannot be treated as a filled order."""
    if not isinstance(order, dict):
        return True
    if order.get("error"):
        return True
    code = order.get("code")
    if code is not None:
        try:
            if int(code) not in (0, 200):
                return True
        except (TypeError, ValueError):
            return True
    status = str(order.get("status", "")).upper()
    return status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


def main_keyboard():
    mon = settings.get("monitoring_enabled")
    mon_btn = "⏹ إيقاف الرصد" if mon else "▶️ تشغيل الرصد"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 الدخول", callback_data="type_in"),
                InlineKeyboardButton("📤 الخروج", callback_data="type_out"),
            ],
            [
                InlineKeyboardButton("🎯 الفرصة", callback_data="type_opportunity"),
                InlineKeyboardButton("🚫 النقية", callback_data="type_clean"),
            ],
            [
                InlineKeyboardButton("🏆 الأفضل", callback_data="run_best"),
                InlineKeyboardButton("🔭 اكتشاف", callback_data="type_discovery"),
            ],
            [
                InlineKeyboardButton("🐋 حيتان توكن", callback_data="whale_search"),
                InlineKeyboardButton("⚙️ المحافظ", callback_data="wallets"),
            ],
            [
                InlineKeyboardButton(mon_btn, callback_data="toggle_monitor"),
                InlineKeyboardButton("⚙️ إعدادات التداول", callback_data="trade_settings"),
            ],
            [
                InlineKeyboardButton("📋 صفقاتي", callback_data="my_trades"),
                InlineKeyboardButton("📊 تقرير", callback_data="pnl_report"),
            ],
            [
                InlineKeyboardButton("💰 رصيدي", callback_data="show_balance"),
            ],
            [
                InlineKeyboardButton("✅ اقفل الربحان", callback_data="close_winners"),
                InlineKeyboardButton("❌ اقفل الخسران", callback_data="close_losers"),
            ],
            [
                InlineKeyboardButton("🛑 بيع كل الصفقات", callback_data="sell_all"),
            ],
        ]
    )



def _settings_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💵 حجم +", callback_data="set_size_up"),
                InlineKeyboardButton("💵 حجم -", callback_data="set_size_down"),
            ],
            [
                InlineKeyboardButton("🛑 وقف أضيق", callback_data="set_sl_up"),
                InlineKeyboardButton("🛑 وقف أوسع", callback_data="set_sl_down"),
            ],
            [
                InlineKeyboardButton("🎯1 +", callback_data="set_tp1_up"),
                InlineKeyboardButton("🎯1 -", callback_data="set_tp1_down"),
                InlineKeyboardButton("🎯2 +", callback_data="set_tp2_up"),
                InlineKeyboardButton("🎯2 -", callback_data="set_tp2_down"),
            ],
            [
                InlineKeyboardButton("🎯3 +", callback_data="set_tp3_up"),
                InlineKeyboardButton("🎯3 -", callback_data="set_tp3_down"),
            ],
            [
                InlineKeyboardButton("📊 حد صفقات +", callback_data="set_max_up"),
                InlineKeyboardButton("📊 حد صفقات -", callback_data="set_max_down"),
            ],
            [
                InlineKeyboardButton("🤖 تفعيل/إيقاف شراء", callback_data="toggle_autobuy"),
            ],
            [
                InlineKeyboardButton("🔙 رجوع", callback_data="back"),
            ],
        ]
    )


def periods(prefix):
    rows, row = [], []
    for label, minutes in TIME_PERIODS:
        row.append(InlineKeyboardButton(label, callback_data="report_%s_%s" % (prefix, minutes)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def result_keyboard(url=""):
    rows = []
    if url:
        rows.append([InlineKeyboardButton("📊 افتح الشارت", url=url)])
    rows.append(
        [
            InlineKeyboardButton("🔄 تحديث", callback_data="refresh"),
            InlineKeyboardButton("🔙 رجوع", callback_data="back"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def wallets_keyboard():
    rows = []
    for label, address in store.get_all().items():
        rows.append(
            [
                InlineKeyboardButton(
                    "🏦 %s (%s…%s)" % (label, address[:6], address[-4:]),
                    callback_data="noop",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("➕ إضافة", callback_data="wallet_add_help"),
            InlineKeyboardButton("🗑 حذف", callback_data="wallet_del_help"),
        ]
    )
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
    return InlineKeyboardMarkup(rows)


async def fetch(prefix, minutes):
    wallets = store.get_all()
    loop = asyncio.get_running_loop()
    if prefix in ("in", "out"):
        data = await loop.run_in_executor(None, get_report, minutes, prefix, wallets)
        return format_report(data, prefix, minutes), result_keyboard()
    if prefix == "opportunity":
        data = await loop.run_in_executor(None, get_opportunity, minutes, wallets)
        text, url = format_opportunity(data, minutes)
        return text, result_keyboard(url)
    if prefix == "clean":
        data = await loop.run_in_executor(None, get_clean_opportunity, minutes, wallets)
        text, url = format_clean_opportunity(data, minutes)
        return text, result_keyboard(url)
    if prefix == "best":
        data = await loop.run_in_executor(None, get_best_opportunities, wallets)
        text, url = format_best_opportunities(data)
        return text, result_keyboard(url)
    data = await loop.run_in_executor(None, get_top_counterparties, minutes, wallets)
    return format_discovery(data, minutes), result_keyboard()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    import trades_db
    trades_db.init()
    trades_db.refresh_use_pg()
    open_n = trades_db.count_open()
    s = settings.load()
    mon = "شغال" if s.get("monitoring_enabled") else "متوقف"
    message = (
        f"👋 <b>بوت التداول</b>\n\n"
        f"{trades_db.storage_status_text()}\n\n"
        f"صفقات مفتوحة: <b>{open_n}</b>\n"
        f"الرصد: <b>{mon}</b>\n"
        f"حجم الصفقة: <b>{s.get('trade_size_usd')}$</b>\n\n"
        f"اختر من القائمة:"
    )
    await update.message.reply_text(message, reply_markup=main_keyboard(), **KW)


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return
    if data == "back":
        context.user_data.pop("awaiting", None)
        await query.edit_message_text("اختر التقرير:", reply_markup=main_keyboard(), **KW)
        return

    if data in ("type_in", "type_out", "type_opportunity", "type_clean", "type_discovery"):
        await query.edit_message_text("⏱ اختر الفترة:", reply_markup=periods(data[5:]), **KW)
        return

    if data == "whale_search":
        context.user_data["awaiting"] = "whale_token"
        await query.edit_message_text(
            "🐋 <b>بحث حيتان توكن</b>\n\n"
            "أرسل عنوان التوكن (0x...) أو الصق رابط Dexscreener/BscScan.\n"
            "سيتم البحث عن أكبر المحافظ النشطة خلال آخر ساعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back")]]),
            **KW,
        )
        return

    if data == "wallets":
        text = "⚙️ <b>المحافظ الحالية</b>\n\n" + store.list_text()
        await query.edit_message_text(text, reply_markup=wallets_keyboard(), **KW)
        return

    if data == "wallet_add_help":
        context.user_data["awaiting"] = "add_wallet"
        await query.edit_message_text(
            "➕ <b>إضافة محفظة</b>\n\n"
            "أرسل بالصيغة:\n"
            "<code>إضافة اسم 0x...</code>\n"
            "أو:\n"
            "<code>إضافة حوت اسم 0x...</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="wallets")]]),
            **KW,
        )
        return

    if data == "wallet_del_help":
        context.user_data["awaiting"] = "del_wallet"
        labels = list(store.get_all().keys())
        if not labels:
            await query.edit_message_text("لا توجد محافظ للحذف.", reply_markup=main_keyboard(), **KW)
            return
        rows = [[InlineKeyboardButton("🗑 " + lb, callback_data="del_" + lb)] for lb in labels]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="wallets")])
        await query.edit_message_text("اختر المحفظة للحذف:", reply_markup=InlineKeyboardMarkup(rows), **KW)
        return

    if data.startswith("del_"):
        label = data[4:]
        ok = store.remove(label)
        msg = "✅ تم حذف: " + label if ok else "❌ تعذر الحذف (يجب الإبقاء على محفظة واحدة على الأقل)"
        await query.edit_message_text(msg + "\n\n" + store.list_text(), reply_markup=wallets_keyboard(), **KW)
        return


    # --- Trading controls ---
    if data == "toggle_monitor":
        cur = settings.get("monitoring_enabled")
        settings.set_value("monitoring_enabled", not cur)
        # لو شغلنا الرصد نشغل الشراء التلقائي كمان افتراضياً
        if not cur:
            settings.set_value("auto_buy_enabled", True)
        else:
            settings.set_value("auto_buy_enabled", False)
        state = "🟢 تم تشغيل الرصد + الشراء التلقائي" if not cur else "🔴 تم إيقاف الرصد"
        await query.edit_message_text(state + "\n\n" + settings.text(), reply_markup=main_keyboard(), **KW)
        return

    if data == "trade_settings":
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return

    if data == "toggle_autobuy":
        cur = settings.get("auto_buy_enabled")
        settings.set_value("auto_buy_enabled", not cur)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return

    if data in ("close_winners", "close_losers"):
        import mexc_trade
        mode = "winners" if data == "close_winners" else "losers"
        trades = trades_db.get_open_trades()
        if not trades:
            await query.edit_message_text("لا توجد صفقات مفتوحة.", reply_markup=main_keyboard(), **KW)
            return

        winners, losers = [], []
        for t in trades:
            pair = mexc_trade.resolve_symbol(t["symbol"])
            price = await asyncio.to_thread(mexc_trade.get_price, pair)
            entry = float(t["entry_price"] or 0)
            if price <= 0 or entry <= 0:
                continue
            pnl = ((price - entry) / entry) * 100
            item = (t, price, pnl)
            if pnl >= 0:
                winners.append(item)
            else:
                losers.append(item)

        targets = winners if mode == "winners" else losers
        label = "الربحان" if mode == "winners" else "الخسران"

        if not targets:
            await query.edit_message_text(
                f"مفيش صفقات {label} حالياً.\n\n"
                f"✅ رابحة: {len(winners)}\n❌ خاسرة: {len(losers)}",
                reply_markup=main_keyboard(),
                **KW,
            )
            return

        results = []
        closed = 0
        for t, price, pnl in targets:
            pair = mexc_trade.resolve_symbol(t["symbol"])
            real_bal = await asyncio.to_thread(mexc_trade.get_base_balance, pair)
            qty = real_bal if real_bal > 0 else float(t["quantity"] or 0)
            order = await asyncio.to_thread(mexc_trade.market_sell, pair, qty)
            if order_failed(order):
                results.append(f"⚠️ <b>{t['symbol']}</b> فشل البيع ولم تُغلق في السجل")
                continue
            trades_db.close_trade(t["id"], price, note=f"Close{label}")
            closed += 1
            sign = "+" if pnl >= 0 else ""
            results.append(f"{'✅' if pnl>=0 else '❌'} <b>{t['symbol']}</b> {sign}{pnl:.1f}%")

        msg = (
            f"تم إغلاق <b>{closed}</b> صفقة من {label}\n\n"
            + "\n".join(results) +
            f"\n\nالمتبقي — ✅ رابحة: {len(winners) if mode=='losers' else 0} | "
            f"❌ خاسرة: {len(losers) if mode=='winners' else 0}"
        )
        await query.edit_message_text(msg, reply_markup=main_keyboard(), **KW)
        return

    if data == "show_balance":
        import mexc_trade
        try:
            usdt = await asyncio.to_thread(mexc_trade.get_balance, "USDT")
            msg = f"💰 <b>الرصيد المتاح</b>\n\nUSDT: <b>{usdt:.2f}$</b>"
        except Exception as e:
            msg = f"❌ فشل جلب الرصيد: {e}"
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back")]]),
            **KW,
        )
        return

    if data == "sell_all":
        # تأكيد أولاً
        await query.edit_message_text(
            "⚠️ <b>هل أنت متأكد من بيع كل الصفقات المفتوحة؟</b>",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ نعم، بيع الكل", callback_data="sell_all_confirm"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="back"),
                ]
            ]),
            **KW,
        )
        return

    if data == "sell_all_confirm":
        import mexc_trade
        trades = trades_db.get_open_trades()
        if not trades:
            await query.edit_message_text(
                "لا توجد صفقات مفتوحة.",
                reply_markup=main_keyboard(),
                **KW,
            )
            return
        results = []
        for t in trades:
            pair = mexc_trade.resolve_symbol(t["symbol"])
            price = await asyncio.to_thread(mexc_trade.get_price, pair)
            qty = t["quantity"]
            # خصم الكميات اللي اتباعت في أهداف سابقة بشكل تقريبي
            # نبيع الكمية المتبقية المسجلة
            order = await asyncio.to_thread(mexc_trade.market_sell, pair, qty)
            if order_failed(order):
                results.append(f"⚠️ #{t['id']} {t['symbol']}: فشل البيع وبقيت مفتوحة")
                continue
            close_price = price if price > 0 else t["entry_price"]
            pnl = trades_db.close_trade(t["id"], close_price, note="SellAll")
            results.append(f"#{t['id']} {t['symbol']}: PnL ≈ {pnl:.1f}%" if pnl is not None else f"#{t['id']} {t['symbol']}: {order}")
        msg = "🛑 <b>تم إغلاق كل الصفقات</b>\n\n" + "\n".join(results)
        await query.edit_message_text(msg, reply_markup=main_keyboard(), **KW)
        return

    if data == "pnl_report":
        await query.edit_message_text(
            trades_db.report_text(40),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back")]]),
            **KW,
        )
        return

    if data == "my_trades":
        await query.edit_message_text(
            "📋 <b>الصفقات المفتوحة</b>\n\n" + trades_db.list_open_text(),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back")]]),
            **KW,
        )
        return

    # ---- تعديل النسب بالأزرار ----
    if data == "set_size_up":
        settings.set_value("trade_size_usd", float(settings.get("trade_size_usd")) + 5)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_size_down":
        settings.set_value("trade_size_usd", max(5, float(settings.get("trade_size_usd")) - 5))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_sl_up":
        settings.set_value("stop_loss_pct", min(-1, float(settings.get("stop_loss_pct")) + 1))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_sl_down":
        settings.set_value("stop_loss_pct", float(settings.get("stop_loss_pct")) - 1)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp1_up":
        settings.set_value("tp1_pct", float(settings.get("tp1_pct")) + 1)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp1_down":
        settings.set_value("tp1_pct", max(1, float(settings.get("tp1_pct")) - 1))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp2_up":
        settings.set_value("tp2_pct", float(settings.get("tp2_pct")) + 1)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp2_down":
        settings.set_value("tp2_pct", max(2, float(settings.get("tp2_pct")) - 1))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp3_up":
        settings.set_value("tp3_pct", float(settings.get("tp3_pct")) + 1)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_tp3_down":
        settings.set_value("tp3_pct", max(3, float(settings.get("tp3_pct")) - 1))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_max_up":
        settings.set_value("max_open_trades", int(settings.get("max_open_trades")) + 1)
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return
    if data == "set_max_down":
        settings.set_value("max_open_trades", max(1, int(settings.get("max_open_trades")) - 1))
        await query.edit_message_text(settings.text(), reply_markup=_settings_keyboard(), **KW)
        return

    if data == "run_best":
        prefix, minutes = "best", 0
    elif data.startswith("report_"):
        parts = data.split("_")
        if len(parts) != 3:
            return
        prefix, minutes = parts[1], int(parts[2])
    elif data == "refresh":
        prefix = context.user_data.get("prefix", "out")
        minutes = context.user_data.get("minutes", 60)
    else:
        return

    context.user_data.update(prefix=prefix, minutes=minutes)
    await query.edit_message_text("⏳ جارٍ التحليل…", **KW)
    text, markup = await fetch(prefix, minutes)
    await query.edit_message_text(text, reply_markup=markup, **KW)


async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    text = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    # --- Whale token search ---
    if awaiting == "whale_token":
        context.user_data.pop("awaiting", None)
        m = ADDR_RE.search(text)
        if not m:
            await update.message.reply_text("❌ لم أجد عنوان توكن صحيح (0x...).", reply_markup=main_keyboard(), **KW)
            return
        token = m.group(0)
        await update.message.reply_text("⏳ جارٍ البحث عن حيتان التوكن…", **KW)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, find_whales_for_token, token, 60)
        result = format_whales(data)
        await update.message.reply_text(result, reply_markup=main_keyboard(), **KW)
        return

    # --- Add wallet (single, pure address, or bulk) ---
    addrs_found = ADDR_RE.findall(text)
    is_add_cmd = text.startswith("إضافة") or text.startswith("اضافه") or awaiting == "add_wallet"
    clean = text.strip().replace(" ", "").replace("\n", "")
    is_pure_address = len(addrs_found) == 1 and clean.lower().startswith("0x") and 40 <= len(clean) <= 42

    if is_add_cmd or is_pure_address:
        context.user_data.pop("awaiting", None)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        addrs = ADDR_RE.findall(text)

        # Bulk
        if len(lines) > 1 or len(addrs) > 1:
            success, errors = store.add_bulk(lines)
            msg = f"✅ تم إضافة <b>{success}</b> محفظة"
            if errors:
                msg += "\n\n⚠️ أخطاء:\n" + "\n".join(errors[:8])
            msg += "\n\n" + store.list_text()
            await update.message.reply_text(msg, reply_markup=main_keyboard(), **KW)
            return

        # Single (with name or pure address)
        m = ADDR_RE.search(text)
        if not m:
            await update.message.reply_text(
                "❌ ابعت العنوان كده:\n<code>0x...</code>\n"
                "أو:\n<code>إضافة اسم 0x...</code>",
                **KW,
            )
            return
        address = m.group(0)
        before = text[: m.start()].strip()
        parts = before.replace("إضافة", "").replace("اضافه", "").replace("حوت", "").strip().split()
        if parts:
            label = " ".join(parts)
        else:
            # اسم تلقائي من آخر 4 حروف
            label = "محفظة_" + address[-4:].upper()
        if "حوت" in text and not label.startswith("🐋"):
            label = "🐋 " + label
        err = store.add(label, address)
        if err:
            await update.message.reply_text("❌ " + err, **KW)
        else:
            await update.message.reply_text(
                "✅ تمت إضافة <b>%s</b>\n<code>%s</code>\n\n%s"
                % (label, address, store.list_text()),
                reply_markup=main_keyboard(),
                **KW,
            )
        return

    # --- Delete by name ---
    if text.startswith("حذف ") or awaiting == "del_wallet":
        context.user_data.pop("awaiting", None)
        label = text.replace("حذف", "").strip()
        if not label:
            await update.message.reply_text("اكتب: حذف اسم_المحفظة", **KW)
            return
        ok = store.remove(label)
        msg = "✅ تم حذف: " + label if ok else "❌ لم يتم العثور على المحفظة أو لا يمكن حذف الأخيرة"
        await update.message.reply_text(msg + "\n\n" + store.list_text(), reply_markup=main_keyboard(), **KW)
        return

    # --- إعدادات نصية ---
    low = text.strip().lower()
    if low.startswith("حجم "):
        try:
            v = float(text.split()[1])
            settings.set_value("trade_size_usd", v)
            await update.message.reply_text(f"✅ حجم الصفقة = {v}$\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: حجم 20")
        return
    if low.startswith("وقف "):
        try:
            v = float(text.split()[1])
            if v > 0:
                v = -v
            settings.set_value("stop_loss_pct", v)
            await update.message.reply_text(f"✅ وقف الخسارة = {v}%\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: وقف -8")
        return
    if low.startswith("هدف1 "):
        try:
            v = float(text.split()[1])
            settings.set_value("tp1_pct", abs(v))
            await update.message.reply_text(f"✅ الهدف 1 = +{abs(v)}%\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: هدف1 5")
        return
    if low.startswith("هدف2 "):
        try:
            v = float(text.split()[1])
            settings.set_value("tp2_pct", abs(v))
            await update.message.reply_text(f"✅ الهدف 2 = +{abs(v)}%\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: هدف2 10")
        return
    if low.startswith("هدف3 "):
        try:
            v = float(text.split()[1])
            settings.set_value("tp3_pct", abs(v))
            await update.message.reply_text(f"✅ الهدف 3 = +{abs(v)}%\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: هدف3 15")
        return
    if low.startswith("حد "):
        try:
            v = int(text.split()[1])
            settings.set_value("max_open_trades", max(1, v))
            await update.message.reply_text(f"✅ أقصى صفقات = {v}\n\n" + settings.text(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("مثال: حد 3")
        return

    # Default help
    if text in ("/help", "مساعدة", "?"):
        await update.message.reply_text(
            "<b>الأوامر السريعة:</b>\n"
            "• <code>إضافة اسم 0x...</code> — إضافة محفظة\n"
            "• <code>إضافة حوت اسم 0x...</code> — إضافة حوت\n"
            "• إضافة جماعية: ابعت أكتر من سطر\n"
            "• <code>حذف الاسم</code> — حذف محفظة\n"
            "• زر 🐋 حيتان توكن — بحث عن حيتان توكن معين\n\n"
            "🔔 المراقبة المستمرة شغالة — هيجيلك إشعار فوري عند سحب جماعي",
            reply_markup=main_keyboard(),
            **KW,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        log.error(
            "Telegram polling conflict: stop every other bot instance using this token, then restart this one"
        )
        return
    log.exception("Unhandled bot error", exc_info=context.error)




# ==================== Continuous Monitoring ====================
_alerted_tokens = {}  # key -> last_alert_time

async def continuous_monitor(app):
    """مراقبة مستمرة كل MONITOR_INTERVAL_SEC وإرسال إشعار فوري عند فرصة حقيقية"""
    from config import MONITOR_INTERVAL_SEC, ALERT_COOLDOWN_SEC, TELEGRAM_CHAT_ID
    from tracker import get_strong_outflow_alerts
    import time

    log.info("Continuous monitor started FREE mode (interval=%ss, BSC+Base only)", MONITOR_INTERVAL_SEC)
    await asyncio.sleep(20)  # انتظار قصير بعد التشغيل

    while True:
        try:
            wallets = store.get_all()
            if len(wallets) < 2:
                await asyncio.sleep(MONITOR_INTERVAL_SEC)
                continue

            loop = asyncio.get_running_loop()
            alerts = await loop.run_in_executor(None, get_strong_outflow_alerts, 30, wallets)

            now = time.time()
            for item in alerts:
                ckey = "%s:%s" % (item.get("chain", ""), item.get("contract", ""))
                last = _alerted_tokens.get(ckey, 0)
                if now - last < ALERT_COOLDOWN_SEC:
                    continue

                # بناء رسالة الإشعار الفوري
                symbol = item.get("symbol", "???")
                chain = item.get("chain", "")
                reason = item.get("reason", "")
                n_wallets = item.get("wallet_count", 0)
                score = item.get("score", 0)
                contract = item.get("contract", "")

                from formatter import dex_url, esc
                url = dex_url(item)

                msg = (
                    "🚨 <b>فرصة سحب جماعي</b>\n\n"
                    f"🥇 <b>{esc(symbol)}</b>  ·  {esc(chain.upper())}\n"
                    f"└ {esc(reason)}\n\n"
                    f"📊 المحافظ المشاركة: <b>{n_wallets}</b>\n"
                    f"⭐ السكور: <b>{score:,.0f}</b>\n"
                    f"<code>{esc(contract)}</code>\n\n"
                    f"<a href='{url}'>افتح الشارت</a>"
                )

                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=msg,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
                    _alerted_tokens[ckey] = now
                    log.info("ALERT sent for %s (%s)", symbol, ckey)

                    # شراء تلقائي عند الفرصة
                    ok, buy_msg = await asyncio.to_thread(
                        try_auto_buy,
                        symbol=symbol,
                        contract=contract,
                        chain=chain,
                        note="سحب جماعي",
                    )
                    if ok or ("معطّل" not in buy_msg and "متوقف" not in buy_msg):
                        await app.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=buy_msg,
                            parse_mode="HTML",
                        )
                except Exception as e:
                    log.warning("Failed to send alert: %s", e)

        except Exception as e:
            log.exception("Monitor loop error: %s", e)

        await asyncio.sleep(MONITOR_INTERVAL_SEC)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(False).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages))

    async def post_init(application: Application):
        import trades_db
        trades_db.init()  # تحميل/إنشاء جداول الصفقات والإعدادات
        try:
            from migrate_old import import_old_trades
            n = import_old_trades()
            if n:
                log.info("Imported %s old trades", n)
        except Exception as e:
            log.warning("migrate_old skip: %s", e)
        trades_db.refresh_use_pg()
        log.info("DB ready | %s | open=%s", trades_db.storage_status_text().replace("\n"," | "), trades_db.count_open())
        from position_manager import position_loop
        application.create_task(continuous_monitor(application))
        application.create_task(position_loop(application))
        log.info("Continuous monitor + position manager started")

    app.post_init = post_init
    log.info("Wallet tracker FREE mode (BSC+Base only)")
    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
