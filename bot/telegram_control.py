"""
تحكم كامل بالبوت عبر تليجرام: لوحة تحكم بالأزرار (Inline Keyboard) بالكامل.
البوت ده بيعتمد بالكامل على توصيات قنوات تليجرام المراقَبة (مفيش استراتيجية
داخلية) - فمفيش هنا إدارة أزواج يدوية ولا إعدادات EMA/RSI/ATR، بس إعدادات
عامة (خسارة يومية، عدد مراكز، مبلغ التوصية...) وتحكم في التشغيل/الإيقاف.
الأوامر/الأزرار متاحة فقط للـ Chat ID المحدد في TELEGRAM_CHAT_ID/TELEGRAM_ADMIN_IDS.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .config import Config
from .state import shared_state, SETTING_TYPES

logger = logging.getLogger("telegram")


def _allowed_chat_ids() -> list[str]:
    return Config.allowed_chat_ids_list()


# ---------------------------------------------------------------------------
# ميتاداتا الإعدادات القابلة للتعديل بالأزرار: لابل عربي + نوع + خطوة + حدود
# ---------------------------------------------------------------------------
SETTINGS_META = {
    "max_daily_loss_pct": {"label": "🩸 أقصى خسارة يومية %", "kind": "num", "type": float, "step": 0.5, "min": 0.5, "max": 50},
    "max_open_positions": {"label": "📚 أقصى مراكز مفتوحة", "kind": "num", "type": int, "step": 1, "min": 1, "max": 20},
    "poll_interval_seconds": {"label": "🔄 مدة الفحص (ثانية)", "kind": "num", "type": int, "step": 5, "min": 5, "max": 900},
    "signal_trade_amount_usdt": {"label": "📡 مبلغ توصيات القنوات (USDT)", "kind": "num", "type": float, "step": 5, "min": 1, "max": 10000},
    "signal_price_tolerance_pct": {"label": "📡 سماحية فرق السعر %", "kind": "num", "type": float, "step": 0.5, "min": 0.1, "max": 20},
}


def _fmt_val(v):
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class TelegramController:
    def __init__(self, exchange, db, risk):
        self.exchange = exchange
        self.db = db
        self.risk = risk
        self.app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._register_handlers()

    # =========================================================================
    # تسجيل الهاندلرز
    # =========================================================================
    def _register_handlers(self):
        cmds = {
            "start": self.cmd_menu,
            "help": self.cmd_help,
            "menu": self.cmd_menu,
            "status": self.cmd_status,
            "settings": self.cmd_settings,
            "balance": self.cmd_balance,
            "positions": self.cmd_positions,
            "trades": self.cmd_trades,
            "pnl": self.cmd_pnl,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "dryrun_on": self.cmd_dryrun_on,
            "dryrun_off": self.cmd_dryrun_off,
            "set": self.cmd_set,
            "closeall": self.cmd_closeall,
            "channelsignals": self.cmd_channel_signals,
            "addchannel": self.cmd_addchannel,
            "removechannel": self.cmd_removechannel,
        }
        for name, handler in cmds.items():
            self.app.add_handler(CommandHandler(name, handler))

        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def _guard(self, update: Update) -> bool:
        if str(update.effective_chat.id) not in _allowed_chat_ids():
            if update.message:
                await update.message.reply_text("🚫 غير مصرح لك باستخدام هذا البوت.")
            logger.warning(f"محاولة دخول غير مصرح بها من chat_id={update.effective_chat.id}")
            return False
        return True

    async def _guard_cb(self, update: Update) -> bool:
        if str(update.effective_chat.id) not in _allowed_chat_ids():
            await update.callback_query.answer("🚫 غير مصرح لك.", show_alert=True)
            logger.warning(f"محاولة دخول غير مصرح بها من chat_id={update.effective_chat.id}")
            return False
        return True

    # =========================================================================
    # بناء لوحات المفاتيح (القوائم)
    # =========================================================================
    def _kb_main(self) -> InlineKeyboardMarkup:
        s = shared_state.get_all()
        pause_btn = ("▶️ استئناف التداول", "act:resume") if s["paused"] else ("⏸ إيقاف صفقات جديدة", "act:pause")
        dry_btn = ("💰 تفعيل تداول حقيقي", "act:dryrun_off") if s["dry_run"] else ("🧪 تفعيل وضع تجربة", "act:dryrun_on")
        signal_btn = ("📡 إيقاف توصيات القنوات", "act:signals_off") if s["signal_trading_enabled"] else ("📡 تفعيل توصيات القنوات", "act:signals_on")
        rows = [
            [InlineKeyboardButton("⚙️ الحالة", callback_data="menu:status"),
             InlineKeyboardButton("🛠 الإعدادات", callback_data="menu:settings_view")],
            [InlineKeyboardButton("💰 الرصيد", callback_data="menu:balance"),
             InlineKeyboardButton("📌 المراكز المفتوحة", callback_data="menu:positions")],
            [InlineKeyboardButton("📜 آخر الصفقات", callback_data="menu:trades"),
             InlineKeyboardButton("📊 أداء اليوم", callback_data="menu:pnl")],
            [InlineKeyboardButton(pause_btn[0], callback_data=pause_btn[1])],
            [InlineKeyboardButton(dry_btn[0], callback_data=dry_btn[1])],
            [InlineKeyboardButton(signal_btn[0], callback_data=signal_btn[1])],
            [InlineKeyboardButton("📡 آخر توصيات القنوات", callback_data="menu:channel_signals")],
            [InlineKeyboardButton("📺 إدارة قنوات التوصيات", callback_data="menu:channels")],
            [InlineKeyboardButton("🎛 ضبط الإعدادات العامة", callback_data="menu:strategy")],
            [InlineKeyboardButton("🚨 إغلاق كل المراكز", callback_data="act:closeall_ask")],
        ]
        return InlineKeyboardMarkup(rows)

    def _kb_back(self, target="menu:main") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data=target)]])

    def _kb_strategy(self) -> InlineKeyboardMarkup:
        s = shared_state.get_all()
        rows = []
        row = []
        for key, meta in SETTINGS_META.items():
            row.append(InlineKeyboardButton(f"{meta['label']}: {_fmt_val(s.get(key))}", callback_data=f"setting:{key}"))
            if len(row) == 1:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _kb_channels(self) -> InlineKeyboardMarkup:
        channels = shared_state.get_signal_channels()
        rows = [[InlineKeyboardButton(f"🗑 حذف {c}", callback_data=f"chan:rm:{c}")] for c in channels]
        rows.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="chan:add")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _kb_setting_edit(self, key: str) -> InlineKeyboardMarkup:
        meta = SETTINGS_META[key]
        rows = [
            [InlineKeyboardButton(f"➖ تقليل ({meta['step']})", callback_data=f"setval:{key}:dec"),
             InlineKeyboardButton(f"➕ زيادة ({meta['step']})", callback_data=f"setval:{key}:inc")],
            [InlineKeyboardButton("✏️ إدخال قيمة يدوياً", callback_data=f"setval:{key}:custom")],
            [InlineKeyboardButton("🔙 رجوع للإعدادات", callback_data="menu:strategy")],
        ]
        return InlineKeyboardMarkup(rows)

    def _kb_closeall_confirm(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، أغلق كل المراكز", callback_data="act:closeall_yes")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="menu:main")],
        ])

    # =========================================================================
    # نصوص العرض (يُعاد استخدامها في الأزرار والأوامر النصية)
    # =========================================================================
    def _text_status(self) -> str:
        s = shared_state.get_all()
        uptime = datetime.now(timezone.utc) - shared_state.start_time
        return (
            "⚙️ *حالة البوت*\n"
            f"التشغيل: {'⏸ متوقف مؤقتاً' if s['paused'] else '▶️ يعمل بشكل طبيعي'}\n"
            f"الوضع: {'🧪 تجربة (Dry Run)' if s['dry_run'] else '💰 تداول حقيقي'}\n"
            f"توصيات القنوات: {'📡 مفعّلة' if s['signal_trading_enabled'] else '📡 متوقفة'}\n"
            f"القنوات المراقَبة: {len(s['signal_channels'])}\n"
            f"مبلغ التوصية: {s['signal_trade_amount_usdt']} USDT\n"
            f"مدة التشغيل: {str(uptime).split('.')[0]}"
        )

    def _text_settings(self) -> str:
        s = shared_state.get_all()
        lines = ["🛠 *كل الإعدادات الحالية*\n"]
        for key, value in s.items():
            if key == "signal_channels":
                value = ", ".join(value) if value else "(لا يوجد)"
            lines.append(f"`{key}` = {value}")
        return "\n".join(lines)

    def _fetch_wallet_holdings(self) -> dict[str, float]:
        """يجيب كل رصيد عملة فعلًا موجود في المحفظة دلوقتي (من MEXC مباشرة).
        بيرجع dict {base_currency: balance} لكل عملة رصيدها أكبر من صفر (ومقفلها أقل
        من الكمية المحجوزة لأوامر بيع مفتوحة حتى نعرض الكمية المتاحة فعلًا)."""
        try:
            bal = self.exchange.client.fetch_balance()
        except Exception as e:
            logger.warning(f"تعذر جلب رصيد المحفظة من MEXC: {e}")
            return {}
        holdings: dict[str, float] = {}
        open_sell_reserved = {}
        if not shared_state.is_dry_run():
            # كمية العملة المحجوزة على أوامر بيع مفتوحة (مش متاحات للتقويم)
            try:
                for order in self.exchange.client.fetch_open_orders():
                    if order.get("side") == "sell" and order.get("type") != "market":
                        base = order["symbol"].split("/")[0]
                        open_sell_reserved[base] = open_sell_reserved.get(base, 0) + float(order.get("amount") or 0)
            except Exception:
                pass
        for currency, info in bal.items():
            if not isinstance(info, dict):
                continue
            total = float(info.get("total", 0) or 0)
            if currency not in ("USDT", "info") and total > 0:
                holdings[currency] = max(total - open_sell_reserved.get(currency, 0), 0)
        return holdings

    def _match_wallet_to_recorded(self, holdings: dict[str, float], trades: list) -> list[dict]:
        """يربط الرصيد الفعلي في المحفظة بالسجلات المسجلة في قاعدة البيانات.
        لكل عملة لسه معانا فعليًا: يرجع (symbol, amount, avg_entry) من سجلات الصفقات
        المفتوحة المتطابقة، أو (none, all, avg_all) لو مفيش سجلات (اشتريت يدويًا)."""
        by_symbol: dict[str, list] = {}
        for t in trades:
            if t.get("status") != "open" or t.get("dry_run"):
                continue
            base = t["symbol"].split("/")[0]
            by_symbol.setdefault(base, []).append(t)

        matched: list[dict] = []
        for base, amount in sorted(holdings.items()):
            spot = f"{base}/USDT"
            recs = by_symbol.get(base, [])
            rec_total = sum(float(r["amount"]) for r in recs)
            if recs and rec_total > 0:
                used = min(rec_total, amount)
                avg_entry = sum(float(r["entry_price"]) * float(r["amount"]) for r in recs) / rec_total
                matched.append({"symbol": recs[0]["symbol"], "amount": used, "entry": avg_entry,
                                "from_record": True})
            elif amount > 0:
                # رصيد موجود في المحفظة بس مفيش سجل له في قاعدة البيانات (اشتريت يدويًا)
                matched.append({"symbol": spot, "amount": amount, "entry": None,
                                "from_record": False})
        return matched

    async def _text_positions(self) -> str:
        """يعرض فقط المراكز المفتوحة فعليًا في محفظة MEXC دلوقتي (مش السجل)،
        بنسبة الربح/الخسارة غير المحققة بشكل مبسّط، ومدمجًا لكل عملة في سطر واحد."""
        holdings = self._fetch_wallet_holdings()
        if not holdings:
            return "📭 محفظتك فاضية - لا توجد مراكز مفتوحة حاليًا."

        trades = self.db.open_trades()
        matched = self._match_wallet_to_recorded(holdings, trades)
        if not matched:
            return "📭 لا توجد مراكز مفتوحة حاليًا."

        lines = []
        total_upnl = 0.0
        for m in matched:
            symbol = m["symbol"]
            amount = m["amount"]
            if amount <= 0:
                continue
            try:
                current = self.exchange.fetch_last_price(symbol)
            except Exception:
                lines.append(f"📌 {symbol} | كمية={amount:.6g} (تعذر جلب السعر الحالي)")
                continue
            entry = m["entry"] if m["from_record"] else current
            upnl = (current - entry) * amount
            upnl_pct = ((current / entry) - 1) * 100 if m["from_record"] and entry else 0.0
            total_upnl += upnl
            sign = "📈" if upnl >= 0 else "📉"
            mark = "" if m["from_record"] else " *(اشتريت يدويًا)*"
            if m["from_record"]:
                lines.append(
                    f"{sign} {symbol}{mark}\n"
                    f"   دخول: {entry:.6g} | الآن: {current:.6g} | الكمية: {amount:.6g}\n"
                    f"   ⚡ غير محقق: {upnl:+.2f} USDT ({upnl_pct:+.2f}%)"
                )
            else:
                lines.append(
                    f"{sign} {symbol}{mark}\n"
                    f"   الكمية: {amount:.6g} | الآن: {current:.6g} | غير محقق: {upnl:+.2f} USDT"
                )
        header = "📊 *المراكز المفتوحة في محفظتك حاليًا*"
        if total_upnl != 0.0:
            header += f"\n💵 الإجمالي غير المحقق: {total_upnl:+.2f} USDT"
        return header + "\n\n" + "\n".join(lines)

    async def _text_trades(self) -> str:
        trades = self.db.open_trades()
        if not trades:
            return "لا توجد صفقات مفتوحة مسجلة في قاعدة البيانات."
        return "\n".join(
            f"#{t['id']} {t['symbol']} {t['side']} @ {t['entry_price']} "
            f"(SL={t['stop_loss']}, TP={t['take_profit']})"
            for t in trades
        )

    def _text_channel_signals(self) -> str:
        rows = self.db.recent_channel_signals(10)
        if not rows:
            return "لسه مفيش أي توصية اتقرأت من القنوات."
        status_emoji = {"executed": "✅", "skipped": "⏭", "pending": "⏳"}
        lines = ["📡 *آخر 10 توصيات من القنوات*\n"]
        for r in rows:
            emoji = status_emoji.get(r["status"], "•")
            symbol = r.get("symbol") or r.get("symbol_raw")
            line = f"{emoji} {symbol} | {r['status']}"
            if r.get("detail"):
                line += f"\n   ↳ {r['detail']}"
            lines.append(line)
        return "\n".join(lines)

    # =========================================================================
    # أمر القائمة الرئيسية
    # =========================================================================
    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "🤖 *لوحة تحكم بوت توصيات القنوات*\nاختر من الأزرار تحت 👇",
            parse_mode="Markdown",
            reply_markup=self._kb_main(),
        )

    # ---------- مساعدة عامة ----------
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = (
            "🤖 *أوامر التحكم في البوت*\n\n"
            "البوت ده بيتداول SPOT فقط، وبيعتمد بالكامل على توصيات قنوات تليجرام "
            "المراقَبة - مفيش استراتيجية داخلية.\n\n"
            "افتح لوحة التحكم بالأزرار في أي وقت بكتابة /menu\n\n"
            "*معلومات*\n"
            "/status - حالة البوت الحالية\n"
            "/settings - كل الإعدادات الحالية\n"
            "/balance - الرصيد المتاح (USDT)\n"
            "/positions - المراكز المفتوحة\n"
            "/trades - آخر الصفقات المسجلة\n"
            "/pnl - أداء اليوم (%)\n"
            "/channelsignals - آخر 10 توصيات اتقرأت من القنوات وحالتها\n\n"
            "*قنوات التوصيات*\n"
            "/addchannel channel_username - إضافة قناة للمراقبة\n"
            "/removechannel channel_username - حذف قناة من المراقبة\n\n"
            "*تشغيل/إيقاف*\n"
            "/pause - إيقاف فتح صفقات جديدة\n"
            "/resume - استئناف التداول\n"
            "/dryrun\\_on - وضع تجربة بدون تنفيذ حقيقي\n"
            "/dryrun\\_off - تفعيل التداول الحقيقي\n\n"
            "*الإعدادات*\n"
            "/set <اسم\\_الإعداد> <القيمة>\n"
            "الإعدادات المتاحة: max\\_daily\\_loss\\_pct, max\\_open\\_positions, "
            "poll\\_interval\\_seconds, signal\\_trading\\_enabled, "
            "signal\\_trade\\_amount\\_usdt, signal\\_price\\_tolerance\\_pct\n"
            "مثال: /set signal\\_trade\\_amount\\_usdt 25\n\n"
            "*طوارئ*\n"
            "/closeall confirm - إغلاق كل المراكز فوراً"
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=self._kb_main())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(self._text_status(), parse_mode="Markdown", reply_markup=self._kb_back())

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(self._text_settings(), parse_mode="Markdown", reply_markup=self._kb_back())

    # ---------- معلومات الحساب ----------
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        bal = self.exchange.fetch_balance_usdt()
        await update.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT", reply_markup=self._kb_back())

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(await self._text_positions(), reply_markup=self._kb_back())

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(await self._text_trades(), reply_markup=self._kb_back())

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(
            f"📊 أداء اليوم الحالي: {self.risk.daily_pnl_pct:.2f}%", reply_markup=self._kb_back()
        )

    async def cmd_channel_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(self._text_channel_signals(), parse_mode="Markdown", reply_markup=self._kb_back())

    async def cmd_addchannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("استخدم: /addchannel channel_username (من غير @) أو آيدي رقمي")
            return
        channel = context.args[0].lstrip("@")
        added = shared_state.add_signal_channel(channel)
        try:
            if added:
                self.db.persist_channel(channel)
        except Exception as e:
            logger.error(f"فشل حفظ القناة دائمًأ: {e}")
        msg = (
            f"✅ تم إضافة القناة {channel}{' وستبدأ مراقبتها فورًأ' if added else ' (مضافة بالفعل)'} — "
            "الإضافة دي محفوظة دائمًأ حتى بعد إعادة تشغيل البوت."
        )
        await update.message.reply_text(msg, reply_markup=self._kb_channels())

    async def cmd_removechannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("استخدم: /removechannel channel_username")
            return
        channel = context.args[0].lstrip("@")
        removed = shared_state.remove_signal_channel(channel)
        try:
            if removed:
                self.db.remove_persisted_channel(channel)
        except Exception as e:
            logger.error(f"فشل حذف القناة من الحفظ الدائم: {e}")
        msg = f"🗑 تم حذف القناة {channel}." if removed else f"القناة {channel} مش موجودة في القائمة."
        await update.message.reply_text(msg, reply_markup=self._kb_channels())

    # ---------- تشغيل/إيقاف ----------
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        shared_state.set_paused(True)
        await update.message.reply_text("⏸ تم إيقاف فتح صفقات جديدة. الصفقات المفتوحة هتفضل شغالة زي ما هي.", reply_markup=self._kb_main())

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        shared_state.set_paused(False)
        await update.message.reply_text("▶️ تم استئناف التداول وفتح صفقات جديدة.", reply_markup=self._kb_main())

    async def cmd_dryrun_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        shared_state.set_dry_run(True)
        await update.message.reply_text("🧪 تم تفعيل وضع التجربة. مفيش أي أوامر حقيقية هتتنفذ دلوقتي.", reply_markup=self._kb_main())

    async def cmd_dryrun_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        shared_state.set_dry_run(False)
        await update.message.reply_text("💰 تم تفعيل التداول الحقيقي. الأوامر هتتنفذ فعلياً على حسابك من اللحظة دي.", reply_markup=self._kb_main())

    # ---------- إعدادات عامة ----------
    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "استخدم: /set <اسم_الإعداد> <القيمة>\nمثال: /set signal_trade_amount_usdt 25\n"
                "اكتب /settings عشان تشوف كل الإعدادات وأسماءها."
            )
            return
        key = context.args[0].lower()
        raw_value = context.args[1]

        if key in ("dry_run", "paused"):
            await update.message.reply_text(
                "الإعداد ده ليه أمر مخصص:\n"
                "- dry_run: استخدم /dryrun_on أو /dryrun_off\n"
                "- paused: استخدم /pause أو /resume"
            )
            return

        if key not in SETTING_TYPES:
            await update.message.reply_text(f"⚠️ إعداد غير معروف: {key}\nاكتب /settings عشان تشوف الأسماء الصحيحة.")
            return

        cast = SETTING_TYPES[key]
        try:
            value = cast(raw_value)
        except ValueError:
            await update.message.reply_text(f"⚠️ القيمة '{raw_value}' مش صالحة لإعداد {key}.")
            return

        shared_state.set(key, value)
        await update.message.reply_text(f"✅ تم تحديث {key} = {value}", reply_markup=self._kb_main())

    # ---------- طوارئ ----------
    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not context.args or context.args[0].lower() != "confirm":
            await update.message.reply_text(
                "⚠️ الأمر ده هيقفل كل المراكز المفتوحة فوراً بسعر السوق.\nللتأكيد اكتب: /closeall confirm",
                reply_markup=self._kb_closeall_confirm(),
            )
            return
        await self._do_closeall(update.message)

    async def _do_close_symbol(self, symbol: str) -> str:
        """إغلاق مركز رمز واحد: إلغاء الطلبات المفتوحة + بيع Market + قفل السجل."""
        trades = [t for t in self.db.open_trades() if t["symbol"] == symbol]
        # حتى لو مفيش سجل: نبيع الرصيد الحر ونلغي الطلبات
        try:
            self.exchange.cancel_all_open_orders(symbol)
        except Exception as e:
            logger.warning(f"{symbol}: فشل إلغاء الطلبات: {e}")

        import time as _t
        _t.sleep(0.6)

        available = 0.0
        if not shared_state.is_dry_run():
            try:
                available = float(self.exchange.fetch_base_balance(symbol) or 0)
            except Exception:
                available = 0.0
        else:
            available = sum(float(t["amount"]) for t in trades)

        if available <= 0 and not trades:
            return f"📭 {symbol}: مفيش رصيد ولا سجل مفتوح."

        amount = available
        if trades and available > 0:
            recorded = sum(float(t["amount"]) for t in trades)
            # نبيع الأقل بين الرصيد الفعلي ومجموع السجل (ما نلمسش رصيد قديم زيادة)
            amount = min(available, recorded) if recorded > 0 else available

        current_price = 0.0
        try:
            current_price = self.exchange.fetch_last_price(symbol)
        except Exception:
            pass

        if amount > 0:
            try:
                self.exchange.create_market_sell(symbol, amount)
            except Exception as e:
                return f"❌ {symbol}: فشل البيع بسعر السوق: {e}"

        closed = 0
        total_pnl = 0.0
        for t in trades:
            try:
                entry = float(t["entry_price"])
                leg_amt = float(t["amount"])
                pnl = (current_price - entry) * leg_amt if current_price else 0.0
                self.db.close_trade(t["id"], current_price or entry, pnl)
                self.risk.register_trade_result(pnl, self.exchange.fetch_balance_usdt())
                total_pnl += pnl
                closed += 1
            except Exception as e:
                logger.error(f"فشل قفل سجل #{t.get('id')}: {e}")

        return (
            f"✅ {symbol}: تم إلغاء الطلبات + بيع {amount:.6g} بسعر السوق "
            f"({current_price:.6g}) | قُفل {closed} سجل | PnL={total_pnl:+.2f} USDT"
        )

    async def _do_closeall(self, message):
        """بيع كل المراكز المفتوحة (السبوت) بسعر السوق + إلغاء الطلبات وقفل السجل."""
        trades = self.db.open_trades()
        symbols = sorted({t["symbol"] for t in trades})
        if not symbols:
            # كمان نحاول نلغي أي طلبات مفتوحة عامة لو موجودة
            await message.reply_text("مفيش مراكز مفتوحة مسجلة.", reply_markup=self._kb_main())
            return
        results = []
        for symbol in symbols:
            try:
                msg = await self._do_close_symbol(symbol)
                results.append(msg)
            except Exception as e:
                results.append(f"❌ {symbol}: {e}")
                logger.error(f"فشل إغلاق {symbol}: {e}")
        await message.reply_text("\n".join(results), reply_markup=self._kb_main())

    def _kb_positions_close(self, symbols: list[str]) -> InlineKeyboardMarkup:
        """أزرار إغلاق فردي لكل رمز + رجوع."""
        rows = [
            [InlineKeyboardButton(f"🛑 إغلاق {s} (سوق + حذف طلبات)", callback_data=f"act:close_sym:{s}")]
            for s in symbols
        ]
        rows.append([InlineKeyboardButton("🚨 إغلاق الكل", callback_data="act:closeall_ask")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    # =========================================================================
    # هاندلر الأزرار (Callback Query) - المحرك الرئيسي للوحة التحكم
    # =========================================================================
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard_cb(update):
            return
        query = update.callback_query
        data = query.data or ""
        await query.answer()

        try:
            if data == "menu:main":
                context.user_data.pop("awaiting", None)
                await query.edit_message_text(
                    "🤖 *لوحة تحكم بوت توصيات القنوات*\nاختر من الأزرار تحت 👇",
                    parse_mode="Markdown", reply_markup=self._kb_main(),
                )
            elif data == "menu:status":
                await query.edit_message_text(self._text_status(), parse_mode="Markdown", reply_markup=self._kb_back())
            elif data == "menu:settings_view":
                await query.edit_message_text(self._text_settings(), parse_mode="Markdown", reply_markup=self._kb_back())
            elif data == "menu:balance":
                bal = self.exchange.fetch_balance_usdt()
                await query.edit_message_text(f"💰 الرصيد المتاح: {bal:.2f} USDT", reply_markup=self._kb_back())
            elif data == "menu:positions":
                text = await self._text_positions()
                open_syms = sorted({t["symbol"] for t in self.db.open_trades()})
                if open_syms:
                    await query.edit_message_text(
                        text + "\n\nاختر رمز للإغلاق الفردي (بيع سوق + حذف الطلبات):",
                        reply_markup=self._kb_positions_close(open_syms),
                    )
                else:
                    await query.edit_message_text(text, reply_markup=self._kb_back())
            elif data == "menu:trades":
                await query.edit_message_text(await self._text_trades(), reply_markup=self._kb_back())
            elif data == "menu:pnl":
                await query.edit_message_text(f"📊 أداء اليوم الحالي: {self.risk.daily_pnl_pct:.2f}%", reply_markup=self._kb_back())
            elif data == "act:pause":
                shared_state.set_paused(True)
                await query.edit_message_text("⏸ تم إيقاف فتح صفقات جديدة.", reply_markup=self._kb_main())
            elif data == "act:resume":
                shared_state.set_paused(False)
                await query.edit_message_text("▶️ تم استئناف التداول.", reply_markup=self._kb_main())
            elif data == "act:dryrun_on":
                shared_state.set_dry_run(True)
                await query.edit_message_text("🧪 تم تفعيل وضع التجربة.", reply_markup=self._kb_main())
            elif data == "act:dryrun_off":
                shared_state.set_dry_run(False)
                await query.edit_message_text("💰 تم تفعيل التداول الحقيقي.", reply_markup=self._kb_main())
            elif data == "act:signals_on":
                shared_state.set("signal_trading_enabled", True)
                await query.edit_message_text("📡 تم تفعيل تنفيذ توصيات القنوات تلقائياً.", reply_markup=self._kb_main())
            elif data == "act:signals_off":
                shared_state.set("signal_trading_enabled", False)
                await query.edit_message_text("📡 تم إيقاف تنفيذ توصيات القنوات (البوت هيفضل بس يراقب من غير تنفيذ).", reply_markup=self._kb_main())
            elif data == "menu:channel_signals":
                await query.edit_message_text(self._text_channel_signals(), parse_mode="Markdown", reply_markup=self._kb_back())
            elif data == "menu:channels":
                channels = shared_state.get_signal_channels()
                txt = "📺 *إدارة قنوات التوصيات*\nالقنوات الحالية:\n" + ("\n".join(f"• {c}" for c in channels) if channels else "(لا يوجد)")
                await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=self._kb_channels())
            elif data.startswith("chan:rm:"):
                channel = data.split(":", 2)[2]
                shared_state.remove_signal_channel(channel)
                try:
                    self.db.remove_persisted_channel(channel)
                except Exception as e:
                    logger.error(f"فشل حذف القناة من الحفظ الدائم: {e}")
                channels = shared_state.get_signal_channels()
                txt = "🗑 تم الحذف.\n\n📺 *القنوات الحالية*:\n" + ("\n".join(f"• {c}" for c in channels) if channels else "(لا يوجد)")
                await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=self._kb_channels())
            elif data == "chan:add":
                context.user_data["awaiting"] = {"kind": "add_channel"}
                await query.edit_message_text(
                    "✏️ اكتب يوزرنيم القناة (من غير @) أو آيديها الرقمي، مثال:\n`some_signals_channel`\n"
                    "لازم تكون مشترك في القناة دي بحسابك الشخصي (نفس رقم تليفون TELEGRAM_SESSION_STRING) عشان البوت يقدر يقراها.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu:channels")]]),
                )
            elif data == "menu:strategy":
                await query.edit_message_text("🎛 *ضبط الإعدادات العامة*\nاختر الإعداد اللي عايز تعدله:", parse_mode="Markdown", reply_markup=self._kb_strategy())
            elif data.startswith("setting:"):
                key = data.split(":", 1)[1]
                meta = SETTINGS_META.get(key)
                if not meta:
                    await query.answer("إعداد غير معروف", show_alert=True)
                    return
                current = shared_state.get(key)
                await query.edit_message_text(
                    f"{meta['label']}\nالقيمة الحالية: *{_fmt_val(current)}*",
                    parse_mode="Markdown", reply_markup=self._kb_setting_edit(key),
                )
            elif data.startswith("setval:"):
                _, key, action = data.split(":", 2)
                meta = SETTINGS_META.get(key)
                if not meta:
                    await query.answer("إعداد غير معروف", show_alert=True)
                    return
                if action == "custom":
                    context.user_data["awaiting"] = {"kind": "set_value", "key": key}
                    await query.edit_message_text(
                        f"✏️ اكتب القيمة الجديدة لـ {meta['label']}\n(حد أدنى {meta['min']} - حد أقصى {meta['max']})",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data=f"setting:{key}")]]),
                    )
                elif action in ("inc", "dec"):
                    current = meta["type"](shared_state.get(key))
                    step = meta["step"]
                    new_val = current + step if action == "inc" else current - step
                    new_val = max(meta["min"], min(meta["max"], new_val))
                    new_val = meta["type"](round(new_val, 4))
                    shared_state.set(key, new_val)
                    await query.edit_message_text(
                        f"{meta['label']}\nالقيمة الحالية: *{_fmt_val(new_val)}*",
                        parse_mode="Markdown", reply_markup=self._kb_setting_edit(key),
                    )
            elif data == "act:closeall_ask":
                await query.edit_message_text(
                    "⚠️ الأمر ده هيقفل كل المراكز المفتوحة فوراً بسعر السوق + يحذف الطلبات.\nمتأكد؟",
                    reply_markup=self._kb_closeall_confirm(),
                )
            elif data == "act:closeall_yes":
                await query.edit_message_text("⏳ جاري إغلاق كل المراكز وحذف الطلبات...")
                await self._do_closeall(query.message)
            elif data.startswith("act:close_sym:"):
                symbol = data.split("act:close_sym:", 1)[1].strip()
                await query.edit_message_text(f"⏳ جاري إغلاق {symbol} وحذف الطلبات...")
                result = await self._do_close_symbol(symbol)
                await query.edit_message_text(result, reply_markup=self._kb_main())
            else:
                await query.answer()
        except Exception as e:
            logger.exception(f"خطأ في معالجة زر: {data} - {e}")
            try:
                await query.edit_message_text(f"⚠️ حصل خطأ: {e}", reply_markup=self._kb_main())
            except Exception:
                pass

    # =========================================================================
    # استقبال إدخال نصي حر (لما يكون في زر طالب قيمة يدوية)
    # =========================================================================
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        awaiting = context.user_data.get("awaiting")
        if not awaiting:
            return  # مفيش عملية معلقة - تجاهل الرسالة

        text = (update.message.text or "").strip()

        if awaiting["kind"] == "add_channel":
            channel = text.lstrip("@")
            added = shared_state.add_signal_channel(channel)
            context.user_data.pop("awaiting", None)
            try:
                if added:
                    self.db.persist_channel(channel)
            except Exception as e:
                logger.error(f"فشل حفظ القناة دائمًأ: {e}")
            msg = (
                f"✅ تم إضافة القناة {channel}{' وستبدأ مراقبتها فورًأ' if added else ' (مضافة بالفعل)'} — "
                "الإضافة دي محفوظة دائمًأ حتى بعد إعادة تشغيل البوت."
            )
            await update.message.reply_text(msg, reply_markup=self._kb_channels())

        elif awaiting["kind"] == "set_value":
            key = awaiting["key"]
            meta = SETTINGS_META[key]
            try:
                value = meta["type"](text)
                if not (meta["min"] <= value <= meta["max"]):
                    await update.message.reply_text(
                        f"⚠️ القيمة لازم تكون بين {meta['min']} و {meta['max']}. حاول تاني:"
                    )
                    return
                shared_state.set(key, value)
                context.user_data.pop("awaiting", None)
                await update.message.reply_text(
                    f"✅ تم تحديث {meta['label']} = {_fmt_val(value)}",
                    reply_markup=self._kb_setting_edit(key),
                )
            except ValueError:
                await update.message.reply_text(f"⚠️ قيمة غير صحيحة، اكتب رقم صحيح لـ {meta['label']}:")

    # ---------- إشعارات تلقائية ----------
    def notify(self, text: str):
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_to_all(text), self._loop)
        except Exception as e:
            logger.error(f"فشل جدولة إشعار تليجرام: {e}")

    async def _send_to_all(self, text: str):
        for chat_id in _allowed_chat_ids():
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.error(f"فشل إرسال رسالة لـ {chat_id}: {e}")

    # ---------- تشغيل في الخلفية ----------
    async def _run_async(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("✅ بوت تليجرام شغال ويستقبل الأوامر.")
        stop_event = asyncio.Event()
        await stop_event.wait()

    def start_in_background(self):
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._run_async())
            except Exception as e:
                logger.exception(f"توقف بوت تليجرام بخطأ: {e}")

        self._thread = threading.Thread(target=_run, daemon=True, name="telegram-controller")
        self._thread.start()
