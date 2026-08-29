import asyncio, logging
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
    import wallets as store
    from config import TELEGRAM_BOT_TOKEN, TIME_PERIODS
    from tracker import get_report, get_opportunity, get_clean_opportunity, get_best_opportunities, get_top_counterparties
    from formatter import format_report, format_opportunity, format_clean_opportunity, format_best_opportunities, format_discovery
    logging.basicConfig(level=logging.INFO); KW = {"parse_mode": "HTML", "disable_web_page_preview": True}; WAIT = "waiting"
    def main_keyboard(): return InlineKeyboardMarkup([[InlineKeyboardButton("📥 الدخول", callback_data="type_in"), InlineKeyboardButton("📤 الخروج", callback_data="type_out")], [InlineKeyboardButton("🎯 الفرصة", callback_data="type_opportunity"), InlineKeyboardButton("🚫 النقية", callback_data="type_clean")], [InlineKeyboardButton("🏆 الأفضل", callback_data="run_best"), InlineKeyboardButton("🔭 اكتشاف", callback_data="type_discovery")], [InlineKeyboardButton("⚙️ المحافظ", callback_data="wallets")]])
    def periods(prefix):
      rows, row = [], []
      for label, minutes in TIME_PERIODS:
          row.append(InlineKeyboardButton(label, callback_data="report_%s_%s" % (prefix, minutes)))
          if len(row) == 2: rows.append(row); row = []
      if row: rows.append(row)
      rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")]); return InlineKeyboardMarkup(rows)
    def result_keyboard(url=""): return InlineKeyboardMarkup(([ [InlineKeyboardButton("📊 افتح الشارت", url=url)] ] if url else []) + [[InlineKeyboardButton("🔄 تحديث", callback_data="refresh"), InlineKeyboardButton("🔙 رجوع", callback_data="back")]])
    async def fetch(prefix, minutes):
      w = store.get_all(); loop = asyncio.get_running_loop()
      if prefix in ("in", "out"): data = await loop.run_in_executor(None, get_report, minutes, prefix, w); return format_report(data, prefix, minutes), result_keyboard()
      if prefix == "opportunity": data = await loop.run_in_executor(None, get_opportunity, minutes, w); text, url = format_opportunity(data, minutes); return text, result_keyboard(url)
      if prefix == "clean": data = await loop.run_in_executor(None, get_clean_opportunity, minutes, w); text, url = format_clean_opportunity(data, minutes); return text, result_keyboard(url)
      if prefix == "best": data = await loop.run_in_executor(None, get_best_opportunities, w); text, url = format_best_opportunities(data); return text, result_keyboard(url)
      data = await loop.run_in_executor(None, get_top_counterparties, minutes, w); return format_discovery(data, minutes), result_keyboard()
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 <b>بوت مراقبة محافظ BNB</b>\n\nاختر التقرير:", reply_markup=main_keyboard(), **KW)
    async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
      q = update.callback_query; await q.answer(); d = q.data or ""
      if d == "back": await q.edit_message_text("اختر التقرير:", reply_markup=main_keyboard(), **KW); return
      if d in ("type_in", "type_out", "type_opportunity", "type_clean", "type_discovery"): await q.edit_message_text("⏱ اختر الفترة:", reply_markup=periods(d[5:]), **KW); return
      if d == "run_best": prefix, minutes = "best", 0
      elif d.startswith("report_"): _, prefix, raw = d.split("_"); minutes = int(raw)
      elif d == "refresh": prefix, minutes = context.user_data.get("prefix", "out"), context.user_data.get("minutes", 60)
      elif d == "wallets": await q.edit_message_text("⚙️ المحافظ الحالية:\n" + "\n".join("• " + x for x in store.get_all()), reply_markup=main_keyboard(), **KW); return
      else: return
      context.user_data.update(prefix=prefix, minutes=minutes); await q.edit_message_text("⏳ جارٍ التحليل…", **KW); text, markup = await fetch(prefix, minutes); await q.edit_message_text(text, reply_markup=markup, **KW)
    async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE): return

    def main():
      if not TELEGRAM_BOT_TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
      app = Application.builder().token(TELEGRAM_BOT_TOKEN).build(); app.add_handler(CommandHandler("start", start)); app.add_handler(CallbackQueryHandler(buttons)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages)); app.run_polling()
    if __name__ == "__main__": main()
    