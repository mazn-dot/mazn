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


def main_keyboard():
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
    message = (
        "👋 <b>بوت مراقبة المحافظ — 5 شبكات</b>\n\n"
        "BSC · ETH · Base · Arbitrum · Polygon\n\n"
        "• عرض نظيف: التوكن + سبب جلبه فقط\n"
        "• Score = كمية + عدد التحويلات\n"
        "• حيتان + إضافة محافظ\n\n"
        "اختر التقرير:"
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
        application.create_task(continuous_monitor(application))
        log.info("Continuous monitor task started")

    app.post_init = post_init
    log.info("Wallet tracker FREE mode (BSC+Base only)")
    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
