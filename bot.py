import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import Conflict

import wallets as store
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIME_PERIODS
from tracker import get_report, get_opportunity, get_clean_opportunity, get_best_opportunities, get_top_counterparties
from formatter import format_report, format_opportunity, format_clean_opportunity, format_best_opportunities, format_discovery

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
KW = {"parse_mode": "HTML", "disable_web_page_preview": True}


def authorized(update):
  """Restrict the bot to the configured owner chat when TELEGRAM_CHAT_ID is set."""
  if not TELEGRAM_CHAT_ID:
      return True
  chat = update.effective_chat
  return bool(chat and str(chat.id) == TELEGRAM_CHAT_ID)


def main_keyboard():
  return InlineKeyboardMarkup([
      [InlineKeyboardButton("📥 الدخول", callback_data="type_in"), InlineKeyboardButton("📤 الخروج", callback_data="type_out")],
      [InlineKeyboardButton("🎯 الفرصة", callback_data="type_opportunity"), InlineKeyboardButton("🚫 النقية", callback_data="type_clean")],
      [InlineKeyboardButton("🏆 الأفضل", callback_data="run_best"), InlineKeyboardButton("🔭 اكتشاف", callback_data="type_discovery")],
      [InlineKeyboardButton("⚙️ المحافظ", callback_data="wallets")],
  ])


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
  rows.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh"), InlineKeyboardButton("🔙 رجوع", callback_data="back")])
  return InlineKeyboardMarkup(rows)


def wallets_keyboard():
  rows = []
  for label, address in store.get_all().items():
      rows.append([InlineKeyboardButton("🏦 %s (%s…%s)" % (label, address[:6], address[-4:]), callback_data="noop")])
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
  message = "👋 <b>بوت مراقبة محافظ BNB</b>\n\nاختر التقرير:"
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
      await query.edit_message_text("اختر التقرير:", reply_markup=main_keyboard(), **KW)
      return
  if data in ("type_in", "type_out", "type_opportunity", "type_clean", "type_discovery"):
      await query.edit_message_text("⏱ اختر الفترة:", reply_markup=periods(data[5:]), **KW)
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
  elif data == "wallets":
      await query.edit_message_text("⚙️ <b>المحافظ الحالية</b>\n" + "\n".join("• " + label for label in store.get_all()), reply_markup=wallets_keyboard(), **KW)
      return
  else:
      return
  context.user_data.update(prefix=prefix, minutes=minutes)
  await query.edit_message_text("⏳ جارٍ التحليل…", **KW)
  text, markup = await fetch(prefix, minutes)
  await query.edit_message_text(text, reply_markup=markup, **KW)


async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not authorized(update):
      return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
  if isinstance(context.error, Conflict):
      log.error("Telegram polling conflict: stop every other bot instance using this token, then restart this one")
      return
  log.exception("Unhandled bot error", exc_info=context.error)


def main():
  if not TELEGRAM_BOT_TOKEN:
      raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
  app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(False).build()
  app.add_error_handler(on_error)
  app.add_handler(CommandHandler("start", start))
  app.add_handler(CallbackQueryHandler(buttons))
  app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages))
  log.info("BNB wallet tracker started")
  app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
  main()
