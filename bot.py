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

    # --- Add wallet (also works without awaiting) ---
    if text.startswith("إضافة") or awaiting == "add_wallet":
        context.user_data.pop("awaiting", None)
        # Formats: إضافة اسم 0x...  |  إضافة حوت اسم 0x...
        m = ADDR_RE.search(text)
        if not m:
            await update.message.reply_text(
                "❌ الصيغة:\n<code>إضافة اسم_المحفظة 0x...</code>\nأو\n<code>إضافة حوت اسم 0x...</code>",
                **KW,
            )
            return
        address = m.group(0)
        before = text[: m.start()].strip()
        # Remove the word إضافة / حوت
        parts = before.replace("إضافة", "").replace("حوت", "").strip().split()
        label = " ".join(parts) if parts else "حوت"
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
            "• <code>حذف الاسم</code> — حذف محفظة\n"
            "• زر 🐋 حيتان توكن — بحث عن حيتان توكن معين\n",
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


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(False).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages))
    log.info("BNB wallet tracker started (v2 — whales + score + fixed symbols)")
    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
