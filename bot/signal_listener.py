"""
مراقبة قنوات تليجرام (كعضو عادي - Telethon User API) واستخراج توصيات التداول
منها تلقائياً، وتنفيذها Spot على MEXC.

القنوات المراقَبة قابلة للتعديل لايف من تليجرام (أزرار إدارة القنوات) - من غير
داعي لإعادة تشغيل السيرفر. عشان كده الهاندلر بيستقبل كل الرسايل الجاية للحساب
وبيفلتر بنفسه مين منها من قناة مراقَبة فعلياً حالياً (بدل تسجيل فلتر ثابت
وقت الإقلاع بس).

الأمان والتكرار:
- كل توصية (رسالة + رمز عملة) بيتم "حجزها" في قاعدة البيانات قبل التنفيذ
  (channel_signals.claim) - فلو البوت اتعاد تشغيله أو الرسالة اتعدلت، مش هينفذها تاني.
- التوصيات بتتفلتر: Short (مش مدعوم في سبوت)، مفيش ستوب، مفيش هدف، الرمز مش
  موجود كسبوت على MEXC، أو السعر الحالي بعيد جداً عن سعر الدخول المذكور
  (يعني التوصية قديمة والسعر اتحرك) - كل دي بيتم تجاهلها وتسجيل السبب.

تقسيم الأهداف (دايماً 3 أهداف بالظبط بغض النظر عن عدد الأهداف في التوصية):
- الكمية بتتقسم 3 أجزاء متساوية، كل جزء مربوط بهدف مختلف من الثلاثة
  (شوف signal_parser.normalize_to_three_targets للتفاصيل).
- بعد ما أول هدف يتحقق: الستوب لوس للجزئين الباقيين بيترفع لسعر الدخول (تعادل).
- بعد ما ثاني هدف يتحقق: الستوب لوس للجزء الأخير بيترفع لسعر أول هدف (تأمين ربح).
- الترقية دي بتتم في main.py (_check_open_trade_exit) لأنها مرتبطة بمراقبة الأسعار.
"""
import asyncio
import logging
import threading
import time
import uuid

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from .config import Config
from .state import shared_state
from .signal_parser import parse_signals, to_spot_symbol, normalize_to_three_targets

logger = logging.getLogger("signal_listener")

# لو رمز عملة في القناة مختلف عن رمزه الحقيقي على MEXC، ضيفه هنا يدوياً.
# مثال: لو القناة بتكتب "1000PEPE" بس MEXC عنده الرمز "PEPE" أو العكس.
SYMBOL_ALIASES: dict[str, str] = {
    # "1000PEPE": "PEPE/USDT",
}


def _channel_identity_matches(event, configured: list[str], saved_chat_ids: set[str] | None = None) -> bool:
    """يتأكد هل الرسالة جاية من قناة موجودة في قائمة القنوات المراقَبة الحالية،
    بمقارنة اليوزرنيم (من غير @) والآيدي الرقمي - القائمة بتتغير لايف من تليجرام.
    saved_chat_ids: الآيديات الرقمية الحقيقية المحفوظة في قاعدة البيانات (للقنوات الخاصة
    اللي مفيهاش يوزرنيم - بتتسجل أول ما يضيفها المستخدم)."""
    if not configured:
        return False
    normalized = {c.strip().lstrip("@").lower() for c in configured if c.strip()}
    saved_chat_ids = saved_chat_ids or set()

    chat_id_str = str(event.chat_id)
    if chat_id_str in normalized or chat_id_str.lstrip("-") in normalized:
        return True

    # القنوات الخاصة (مفيهاش يوزرنيم) بتوصل هنا بالآيدي الرقمي فقط - نتطابق مع المحفوظة
    if chat_id_str in saved_chat_ids:
        return True

    username = getattr(event.chat, "username", None) if event.chat else None
    if username and username.lower() in normalized:
        return True

    return False


class SignalListener:
    def __init__(self, exchange, db, risk, telegram):
        self.exchange = exchange
        self.db = db
        self.risk = risk
        self.telegram = telegram  # عشان نستخدم .notify() لإشعارات تليجرام
        self.client: TelegramClient | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # =========================================================================
    def _fixed_amount_usdt(self) -> float:
        return float(shared_state.get("signal_trade_amount_usdt"))

    def _price_tolerance_pct(self) -> float:
        return float(shared_state.get("signal_price_tolerance_pct"))

    async def _handle_signal(self, channel_id: str, message_id: int, parsed):
        symbol_raw = parsed.symbol_raw
        spot_symbol = to_spot_symbol(symbol_raw, SYMBOL_ALIASES)

        # ---- حجز التوصية عشان محدش ينفذها مرتين ----
        claimed = self.db.claim_channel_signal(channel_id, message_id, symbol_raw)
        if not claimed:
            logger.info(f"توصية {symbol_raw} (رسالة {message_id}) اتعالجت قبل كده - تجاهل.")
            return

        def _reject(reason: str):
            self.db.update_channel_signal(
                channel_id, message_id, symbol_raw,
                status="skipped", detail=reason, symbol=spot_symbol, side=parsed.side,
                entry_price=parsed.entry, stop_loss=parsed.stop, targets=parsed.targets,
            )
            logger.info(f"{symbol_raw}: تم تجاهل التوصية - {reason}")
            self.telegram.notify(f"⏭ تم تجاهل توصية {symbol_raw} ({spot_symbol})\nالسبب: {reason}")

        # ---- فلاتر أساسية ----
        if not shared_state.get("signal_trading_enabled"):
            _reject("تنفيذ توصيات القنوات متوقف حالياً")
            return

        if parsed.skip_reason:
            _reject(parsed.skip_reason)
            return

        if shared_state.is_paused():
            _reject("البوت في وضع الإيقاف المؤقت (paused)")
            return

        valid, reason = self.exchange.is_valid_spot_symbol(spot_symbol)
        if not valid:
            _reject(f"رمز غير صالح: {reason}")
            return

        balance = self.exchange.fetch_balance_usdt()
        if not self.risk.trading_allowed(balance):
            _reject("تم تجاوز حد الخسارة اليومي المسموح - إيقاف تلقائي لأي صفقة جديدة")
            return

        try:
            current_price = self.exchange.fetch_last_price(spot_symbol)
        except Exception as e:
            _reject(f"تعذر جلب السعر الحالي: {e}")
            return

        # ---- تأكد إن سعر الدخول لسه قريب من السعر الحالي (مش توصية قديمة) ----
        drift_pct = abs(current_price - parsed.entry) / parsed.entry * 100 if parsed.entry else 999
        tolerance = self._price_tolerance_pct()
        if drift_pct > tolerance:
            _reject(
                f"السعر الحالي ({current_price:.6g}) بعيد عن سعر الدخول المذكور "
                f"({parsed.entry:.6g}) بنسبة {drift_pct:.1f}% (الحد المسموح {tolerance}%)"
            )
            return

        # ---- تنفيذ الشراء ----
        fixed_usdt = self._fixed_amount_usdt()
        if fixed_usdt > balance:
            _reject(f"الرصيد المتاح ({balance:.2f} USDT) أقل من المبلغ الثابت المحدد ({fixed_usdt} USDT)")
            return

        # ---- تنفيذ الشراء بمبلغ ثابت بالـ USDT (الطريقة الأضمن على MEXC) ----
        try:
            order = self.exchange.create_market_buy(spot_symbol, cost=fixed_usdt)
            logger.info(f"{spot_symbol}: أمر الشراء اتنفذ | order={order.get('id') if isinstance(order, dict) else order}")
        except Exception as e:
            _reject(f"فشل تنفيذ أمر الشراء: {e}")
            return

        import time
        expected_amount = round(fixed_usdt / current_price, 6)
        if shared_state.is_dry_run():
            # في وضع التجربة: نحسب الكمية المتوقعة من السعر (مش نجيب رصيد حقيقي = dust)
            total_amount = expected_amount
            logger.info(f"{spot_symbol}: [DRY_RUN] كمية متوقعة = {total_amount} (من {fixed_usdt} USDT @ {current_price})")
        else:
            # تداول حقيقي: جرب نطلع الكمية من رد الأمر، وإلا من رصيد المحفظة
            filled = None
            if isinstance(order, dict):
                filled = order.get("filled") or order.get("amount")
                if filled is not None:
                    try:
                        filled = float(filled)
                    except (TypeError, ValueError):
                        filled = None

            time.sleep(1.5)
            actual_amount = self.exchange.fetch_base_balance(spot_symbol)
            if actual_amount is None or actual_amount <= 0:
                time.sleep(1.5)
                actual_amount = self.exchange.fetch_base_balance(spot_symbol)

            # نختار أفضل تقدير: filled من الأمر > رصيد المحفظة > الكمية المتوقعة
            candidates = []
            if filled and filled > 0:
                candidates.append(("order_filled", filled))
            if actual_amount and actual_amount > 0:
                candidates.append(("wallet", float(actual_amount)))
            candidates.append(("expected", expected_amount))

            # لو الرصيد أكبر بكتير من المتوقع (كان فيه رصيد قديم)، استخدم المتوقع أو filled
            chosen_name, total_amount = candidates[0]
            if chosen_name == "wallet" and expected_amount > 0:
                # لو الرصيد أكبر من المتوقع بـ 30%+ يبقى فيه dust قديم → استخدم المتوقع أو filled
                if total_amount > expected_amount * 1.3 and filled and filled > 0:
                    chosen_name, total_amount = "order_filled", filled
                elif total_amount > expected_amount * 1.3:
                    chosen_name, total_amount = "expected", expected_amount
                # لو الرصيد أصغر بكتير من المتوقع (شراء جزئي) استخدم الرصيد الفعلي
                elif total_amount < expected_amount * 0.5:
                    logger.warning(
                        f"{spot_symbol}: الرصيد الفعلي ({total_amount}) أقل من المتوقع ({expected_amount}) — شراء جزئي؟"
                    )

            total_amount = round(float(total_amount), 6)
            if total_amount <= 0:
                _reject(f"كمية غير صالحة بعد الشراء: {total_amount}")
                return
            logger.info(
                f"{spot_symbol}: كمية مستخدمة = {total_amount} "
                f"(مصدر={chosen_name}, متوقع={expected_amount}, محفظة={actual_amount}, filled={filled})"
            )

        # ---- تقسيم الكمية على 3 أهداف بالظبط (بغض النظر عن عدد أهداف التوصية) ----
        three_targets = normalize_to_three_targets(current_price, parsed.targets)
        legs = len(three_targets)
        leg_amount = round(total_amount / legs, 6)
        group_id = uuid.uuid4().hex
        trade_ids = []
        for i, tp in enumerate(three_targets, start=1):
            trade_id = self.db.open_trade(
                spot_symbol, "buy", leg_amount, current_price,
                parsed.stop, tp,
                f"توصية قناة: {symbol_raw} (رسالة {message_id}) - هدف {i}/{legs}",
                shared_state.is_dry_run(),
                group_id=group_id, leg_index=i, total_legs=legs,
            )
            trade_ids.append(trade_id)

        self.db.update_channel_signal(
            channel_id, message_id, symbol_raw,
            status="executed", detail=f"تم تنفيذ {legs} أجزاء (مجموعة {group_id[:8]})", symbol=spot_symbol,
            side=parsed.side, entry_price=current_price, stop_loss=parsed.stop,
            targets=three_targets, trade_ids=trade_ids,
        )

        # ---- طبقة حماية خارجية: أوامر TP/SL على MEXC نفسها (trigger orders) ----
        try:
            legs_list = [(leg_amount, tp) for tp in three_targets]
            tp_sl_results = self.exchange.place_tp_sl_orders(spot_symbol, legs_list, parsed.stop)
            # حفظ أرقام أوامر المنصة مع كل leg في قاعدة البيانات (عشان نقدر نلغيها لاحقًا)
            tp_orders = tp_sl_results.get("tp", [])
            sl_info = tp_sl_results.get("sl")
            for i, trade_id in enumerate(trade_ids):
                plan_ids = [tp_orders[i]["order_id"]] if i < len(tp_orders) and tp_orders[i]["order_id"] else []
                sl_id = sl_info["order_id"] if sl_info and sl_info.get("order_id") else None
                try:
                    self.db.save_plan_order_ids(trade_id, plan_ids, sl_id)
                except Exception as save_err:
                    logger.warning(f"{spot_symbol}: فشل حفظ أرقام أوامر المنصة للصفقة #{trade_id}: {save_err}")
            if tp_sl_results["errors"]:
                self.telegram.notify(
                    f"⚠ {spot_symbol}: بعض أوامر المنصة فشلت:\n"
                    + "\n".join("- " + err for err in tp_sl_results["errors"])
                    + "\nالبوت لسه بيراقب ويحمي الصفقة داخليًا."
                )
        except Exception as tp_sl_err:
            logger.error(f"{spot_symbol}: فشل عام في وضع أوامر المنصة: {tp_sl_err}")

        targets_txt = ", ".join(f"{t:.6g}" for t in three_targets)
        logger.info(f"{spot_symbol}: تم تنفيذ توصية من القناة | كمية={total_amount} | أهداف={legs}")
        self.telegram.notify(
            f"📡 تم تنفيذ توصية من القناة\n"
            f"{spot_symbol} (من: {symbol_raw})\n"
            f"سعر الدخول الفعلي: {current_price:.6g} (المذكور: {parsed.entry:.6g})\n"
            f"الستوب: {parsed.stop:.6g}\n"
            f"الأهداف الثلاثة: {targets_txt}\n"
            f"الكمية الكلية: {total_amount} (مقسّمة {legs} أجزاء متساوية)\n"
            f"بعد كل هدف يتحقق هيترفع الستوب تلقائياً للأجزاء الباقية.\n"
            f"المبلغ: {fixed_usdt} USDT | صفقات #{', #'.join(str(t) for t in trade_ids)}"
        )

    async def _on_new_message(self, event):
        try:
            configured = shared_state.get_signal_channels()
            saved_chat_ids = set()
            try:
                for ch in self.db.get_persisted_channels_with_chat_ids():
                    name, chat_id = ch
                    if chat_id:
                        saved_chat_ids.add(str(chat_id))
            except Exception:
                pass
            if not _channel_identity_matches(event, configured, saved_chat_ids):
                # تسجيل تشخيصي: لو وصلت رسالة من قناة مش في قائمة المراقبة
                # بنتبّه في اللوج عشان نعرف إيه القنوات اللي بتوصل منها رسائل
                try:
                    title = getattr(event.chat, "title", None)
                    uname = getattr(event.chat, "username", None)
                    logger.info(
                        f"📩 رسالة وصلت من قناة غير مراقبة: '{title}' "
                        f"(يوزرنيم: {uname or '-'}) | chat_id={event.chat_id}"
                    )
                except Exception:
                    pass
                return
            text = event.raw_text or ""
            channel_id = str(event.chat_id)
            message_id = event.id
            signals = parse_signals(text)
            if not signals:
                return
            for parsed in signals:
                await self._handle_signal(channel_id, message_id, parsed)
        except Exception as e:
            logger.exception(f"خطأ أثناء معالجة رسالة من القناة: {e}")

    async def _run_async(self):
        missing = Config.validate_signal_listener()
        if missing:
            logger.warning(
                f"ميزة مراقبة قنوات التوصيات متوقفة - متغيرات ناقصة: {', '.join(missing)}. "
                f"راجع README لطريقة إعدادها."
            )
            return

        # إعادة محاولة الاتصال لانهائيًا كل 60 ثانية - الجلسة ممكن تفسد
        # أو تنقطع، والبوت لازم يفضل يحاول يوصّلها بدل ما يتوقف بصمت
        retry_delay = 60
        while True:
            self.client = TelegramClient(
                StringSession(Config.TELEGRAM_SESSION_STRING),
                int(Config.TELEGRAM_API_ID),
                Config.TELEGRAM_API_HASH,
            )
            try:
                await self.client.start()
                break  # نجح الاتصال - نكمل التجهيز
            except Exception as e:
                err_str = str(e)
                logger.error(f"قارئ القنوات فشل في الاتصال: {e}")
                # لو الجلسه منتهية/مرفوضة، البلاغ أوضح عشان المستخدم يولّد جلسة جديدة
                if any(k in err_str for k in ("AuthKey", "SessionRevoked", "AuthKeyDuplicated",
                                              "invalid", "API_ID_INVALID", "PHONE")):
                    self.telegram.notify(
                        "⚠ قارئ قنوات التوصيات فشل في الاتصال بحساب تليجرام.\n"
                        "السبب الأرجح: الجلسة (TELEGRAM_SESSION_STRING) منتهية أو غير صالحة.\n"
                        "الحل: شغّل python generate_session.py والصق الناتج في Railway."
                    )
                await asyncio.sleep(retry_delay)
                continue

        # مفيش chats= filter هنا عمداً - القنوات بتتغير لايف من تليجرام (شوف
        # _channel_identity_matches)، فبنسمع كل الرسايل الجاية للحساب ونفلتر بنفسنا.
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        try:
            me = await self.client.get_me()
            channels = self._merged_channels()
            shared_state.set_signal_channels(channels)
            logger.info(f"✅ مراقبة قنوات التوصيات شغالة (حساب: {me.first_name}) | القنوات: {channels}")
            self.telegram.notify(
                "📡 مراقبة قنوات التوصيات اشتغلت.\n"
                f"القنوات الحالية: {', '.join(channels) if channels else '(لسه مفيش قنوات - ضيف من /menu)'}"
            )
            # فحص تشخيصي عند بدء القارئ: نتأكد إن كل قناة مضافة نستطيع نقرأ منها فعلًا
            # (لو الحساب طُرد من قناة أو القناة مش موجودة بنتبّه المستخدم فورًا بدل
            # ما يضيع الوقت على إن القارئ مش شغال)
            await self._verify_accessible_channels(channels, me)
        except Exception as e:
            logger.error(f"فشل جلب معلومات الحساب من تليجرام: {e}")
            self.telegram.notify(f"⚠ قارئ القنوات اتصل لكن فشل في قراءة البيانات: {e}")
        await self.client.run_until_disconnected()

    async def _verify_accessible_channels(self, channels: list[str], me):
        """يحاول يجيب آخر رسالة من كل قناة مراقبة — لو فشل، القناة مش متاحة
        للحساب ده (طُرد منها / اسم غلط / القناة خاصة والحساب مش عضو).
        كمان يسجل الآيدي الرقمي لكل قناة متاحة عشان تطابق القنوات الخاصة
        (المفيهاش يوزرنيم) يشتغل صح."""
        for ch in channels:
            try:
                entity = await self.client.get_entity(ch)
                chat_id = entity.id
                try:
                    await self.db.update_channel_chat_id(ch, chat_id)
                except Exception:
                    pass
                if not getattr(entity, "broadcast", None):
                    logger.warning(
                        f"القناة {ch} (chat_id={chat_id}) مش من نوع قناة - "
                        f"بتبقى '{getattr(entity, 'title', ch)}'. القارئ بيسمعها بس."
                    )
                try:
                    last_msg = await self.client.get_messages(entity, limit=1)
                    if not last_msg:
                        logger.warning(f"قناة {ch} (chat_id={chat_id}): مفيهاش رسائل بعد.")
                    else:
                        logger.info(
                            f"قناة {ch} (chat_id={chat_id}): متاحة، آخر رسالة منها "
                            f"بتاريخ {last_msg[0].date}"
                        )
                except Exception as read_err:
                    logger.error(
                        f"قناة {ch} (chat_id={chat_id}): مش قادر نقرأ منها - {read_err}. "
                        f"ممكن تكون طردت الحساب أو القناة خاصة ومش مشترك فيها."
                    )
            except ValueError as e:
                logger.error(f"قناة {ch}: مش موجودة أو الاسم غلط - {e}")
            except Exception as e:
                logger.error(f"قناة {ch}: فشل التحقق - {e}")

    def _merged_channels(self) -> list[str]:
        """يدمج القنوات المحفوظة دائمًا في قاعدة البيانات مع قائمة env — عشان القنوات
        المضافة من أزرار التليجرام تفضل موجودة حتى بعد إعادة تشغيل البوت، والقنوات
        المحذوفة تُحذف نهائيًا من القائمة."""
        try:
            env_channels = Config.signal_channels_list() or []
            db_channels = self.db.get_persisted_channels()
            # union بترتيب: المحفوظة دائمًأ ثم الجاية من env (من غير تكرار)
            seen = set()
            merged = []
            for ch in list(db_channels) + list(env_channels):
                key = str(ch).strip().lstrip("@").lower()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(ch)
            return merged
        except Exception as e:
            logger.error(f"فشل دمج قائمة القنوات المحفوظة: {e}")
            return Config.signal_channels_list() or []

    def _run_with_reconnect(self):
        """يشغّل حلقة async ويعيد تشغيلها من تاني لو قارئ القنوات انفصل -
        الانفصال ممكن يحصل لأسباب شبكية، والبوت لازم يرجع تلقائيًا."""
        while True:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                loop.run_until_complete(self._run_async())
            except Exception as e:
                logger.exception(f"توقف مراقب قنوات التوصيات بخطأ: {e}")
                self.telegram.notify(f"⚠ قارئ قنوات التوصيات توقف: {e}\nبيحاول يرجع تلقائيًا كل دقيقة.")
            logger.info("قارئ القنوات بيحاول يرجع... (خلال 60 ثانية)")
            time.sleep(60)

    def start_in_background(self):
        self._thread = threading.Thread(target=self._run_with_reconnect, daemon=True,
                                        name="signal-listener")
        self._thread.start()
