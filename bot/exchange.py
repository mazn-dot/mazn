"""
طبقة التواصل مع Bitget عبر مكتبة ccxt - تداول SPOT فقط.
مفيش رافعة مالية ومفيش أوامر reduceOnly/positions (دي مفاهيم فيوتشرز).
كل "مركز" هنا هو رصيد فعلي من العملة الأساسية تم شراؤه، وبيتقفل ببيعه.
"""
import logging
import ccxt

from .config import Config
from .state import shared_state

logger = logging.getLogger("exchange")


class BitgetExchange:
    def __init__(self):
        params = {
            "apiKey": Config.BITGET_API_KEY,
            "secret": Config.BITGET_API_SECRET,
            "password": Config.BITGET_API_PASSPHRASE,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                # يسمح بأوامر شراء ماركت بالكمية (base amount) من غير ما نحتاج نحسب التكلفة يدوي
                "createMarketBuyOrderRequiresPrice": False,
            },
        }
        self.client = ccxt.bitget(params)

    @property
    def dry_run(self) -> bool:
        """يقرأ من shared_state عشان يتغير لايف عبر أوامر تليجرام (/dryrun_on, /dryrun_off)."""
        return shared_state.is_dry_run()

    def load_markets(self):
        return self.client.load_markets()

    def is_valid_spot_symbol(self, symbol: str) -> tuple[bool, str]:
        """
        يتأكد إن الرمز موجود فعلاً كسوق سبوت على Bitget قبل ما نضيفه للتداول.
        بيرجع (True, "") لو تمام، أو (False, "سبب الرفض") لو مش صالح.
        """
        try:
            if not self.client.markets:
                self.client.load_markets()
        except Exception as e:
            return False, f"تعذر تحميل قائمة الأسواق من المنصة: {e}"

        market = self.client.markets.get(symbol)
        if market is None:
            # هات اقتراحات قريبة من نفس العملة الأساسية عشان نساعد المستخدم
            base = symbol.split("/")[0].upper()
            suggestions = sorted({
                m for m in self.client.markets
                if self.client.markets[m].get("spot") and m.upper().startswith(base + "/")
            })
            hint = f" أقرب رموز موجودة لنفس العملة: {', '.join(suggestions[:5])}" if suggestions else ""
            return False, f"الرمز {symbol} مش موجود على Bitget أصلاً.{hint}"

        if not market.get("spot", False):
            return False, f"الرمز {symbol} موجود بس كفيوتشرز/عقود مش كسبوت. البوت ده لتداول السبوت فقط."

        if not market.get("active", True):
            return False, f"الرمز {symbol} موجود بس غير نشط (متوقف) حالياً على المنصة."

        return True, ""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200):
        return self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_last_price(self, symbol: str) -> float:
        ticker = self.client.fetch_ticker(symbol)
        return float(ticker["last"])

    def fetch_balance_usdt(self) -> float:
        """الرصيد المتاح (الكاش) بعملة USDT - مش شامل قيمة العملات المشتراة حالياً."""
        try:
            bal = self.client.fetch_balance()
            return float(bal.get("USDT", {}).get("free", 0) or 0)
        except Exception as e:
            logger.error(f"فشل جلب الرصيد: {e}")
            return 0.0

    def fetch_base_balance(self, symbol: str) -> float:
        """رصيد العملة الأساسية المتاح فعلياً (مثلاً BTC في BTC/USDT) - يُستخدم قبل البيع
        للتأكد إن الكمية المسجلة في قاعدة البيانات فعلاً موجودة في المحفظة."""
        try:
            base = symbol.split("/")[0]
            bal = self.client.fetch_balance()
            return float(bal.get(base, {}).get("free", 0) or 0)
        except Exception as e:
            logger.error(f"فشل جلب رصيد {symbol}: {e}")
            return 0.0

    def create_market_buy(self, symbol: str, amount: float, max_retries: int = 2):
        """شراء سبوت بسعر السوق - amount بالعملة الأساسية (base currency).
        لو ccxt اشتكى من احتياجه لسعر السوق لحساب التكلفة (InvalidOrder مع رسالة
        createMarketBuyOrderRequiresPrice)، بنجيب السعر الحالي من المنصة ونمرره
        صراحة، ولو فشلنا كمان بنعيد المحاولة مرة واحدة قبل ما نرفع الخطأ."""
        if self.dry_run:
            logger.info(f"[DRY_RUN] MARKET BUY {amount} {symbol}")
            return {"id": "dry-run", "symbol": symbol, "side": "buy", "amount": amount, "status": "dry_run"}

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # محاولة أولى عادية - الخيارات options مع createMarketBuyOrderRequiresPrice=False
                # المفروض تغطي الحالة، لكن بعض إعدادات المنصة/نسخ ccxt ممكن ترفضها
                return self.client.create_order(symbol, type="market", side="buy", amount=amount)
            except ccxt.InvalidOrder as e:
                last_error = e
                if "createMarketBuyOrderRequiresPrice" not in str(e):
                    raise
                if attempt >= max_retries:
                    break
                logger.warning(
                    f"ccxt طلب سعر السوق لحساب تكلفة شراء {symbol} - "
                    "بنجيب السعر الحالي وبنمرره صراحة في أمر الشراء (محاولة {attempt}/{max_retries})"
                )
                try:
                    price = self.fetch_last_price(symbol)
                except Exception as fetch_err:
                    raise ccxt.InvalidOrder(
                        f"{e} (كمان تعذر جلب السعر الحالي للمحاولة الاحتياطية: {fetch_err})"
                    )
                return self.client.create_order(
                    symbol, type="market", side="buy", amount=amount, price=price
                )
            except Exception:
                raise
        raise last_error

    def round_amount_for_market(self, symbol: str, amount: float) -> float:
        """يقرب الكمية لأدق دقة مقبولة من المنصة عشان ما ترفضها (مثلاً Bitget
        يرفض كميات أدق من الحد الأدنى للدقة - مثل MASK 0.0001 / SKY 0.01).
        بيرجع الكمية مربعة لأسفل (مش بتزيد الكمية عن المتاحة أبدًا)."""
        try:
            market = self.client.markets.get(symbol)
            precision = None
            if market:
                precision = market.get("precision", {}).get("amount")
            if precision is None:
                self.client.load_markets()
                market = self.client.markets.get(symbol) or {}
                precision = market.get("precision", {}).get("amount")
            if precision is not None:
                amount = round(int(amount * (10 ** precision)) / (10 ** precision), int(precision))
        except Exception as e:
            logger.warning(f"{symbol}: تعذر تحديد دقة الكمية، استخدمت الكمية كما هي: {e}")
        # الحد الأدنى: لو قربنا الكمية نزلت لصفر، نرجع القيمة الأصلية عشان
        # المنصة هي اللي ترفض وتوضح السبب (أفضل من تجارة صفر)
        return amount if amount > 0 else amount

    def create_market_sell(self, symbol: str, amount: float):
        """بيع سبوت بسعر السوق - amount بالعملة الأساسية (base currency).
        الكمية بتتقرب تلقائيًا لدقة المنصة قبل الإرسال."""
        if self.dry_run:
            logger.info(f"[DRY_RUN] MARKET SELL {amount} {symbol}")
            return {"id": "dry-run", "symbol": symbol, "side": "sell", "amount": amount, "status": "dry_run"}
        amount = self.round_amount_for_market(symbol, amount)
        return self.client.create_order(symbol, type="market", side="sell", amount=amount)

    def close_position_market(self, symbol: str, amount: float):
        """قفل مركز سبوت مفتوح = بيع الكمية اللي معانا بسعر السوق."""
        return self.create_market_sell(symbol, amount)

    # =========================================================================
    # أوامر TP/SL على المنصة نفسها (trigger / plan orders) - طبقة حماية خارجية
    # Bitget Spot لا يدعم stopLossPrice/takeProfitPrice (دي فيوتشرز)، لكن يدعم
    # أوامر شرطية منفصلة (plan orders) - كل هدف وكل ستوب أمر مستقل.
    # =========================================================================
    def create_trigger_sell(self, symbol: str, amount: float, trigger_price: float):
        """
        أمر بيع ماركت شرطى (trigger order) على المنصة: يُفعّل تلقائيًا لما السعر
        يوصل trigger_price. بيتستخدم للـ TP (عند الهدف) والـ SL (عند الستوب).
        في Dry Run بيسجل في اللوج بس ومش بينفذ حاجة على المنصة.
        """
        if self.dry_run:
            logger.info(f"[DRY_RUN] TRIGGER SELL {amount} {symbol} @ trigger={trigger_price}")
            return {"id": "dry-run", "symbol": symbol, "side": "sell", "amount": amount,
                    "status": "dry_run", "info": {"triggerPrice": trigger_price}}
        # كل trigger order لازم يكون مستقل (ccxt بيرفض أكتر من trigger في أمر واحد)
        return self.client.create_order(
            symbol, type="market", side="sell", amount=amount,
            params={"triggerPrice": trigger_price}
        )

    def place_tp_sl_orders(self, symbol: str, legs: list[tuple[float, float]], sl: float | None):
        """
        يضع أوامر TP لكل leg (الكمية الجزئية عند كل هدف) + أمر SL واحد بالكمية الكلية.
        بيرجع dict فيه نتائج التنفيذ لكل أمر - الأخطاء مش بتقفل الشراء (حماية احتياطية):
        - لو فشل أمر المنصة، المراقبة الذاتية في البوت بتغطي القفل.
        """
        results = {"tp": [], "sl": None, "errors": []}
        total_amount = sum(a for a, _ in legs)

        for leg_amount, target in legs:
            try:
                order = self.create_trigger_sell(symbol, leg_amount, target)
                order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
                results["tp"].append({"target": target, "amount": leg_amount, "order_id": order_id})
                logger.info(f"{symbol}: تم وضع أمر TP على المنصة - target={target} amount={leg_amount} id={order_id}")
            except Exception as e:
                results["errors"].append(f"TP@{target}: {e}")
                logger.error(f"{symbol}: فشل وضع أمر TP عند {target}: {e}")

        if sl is not None and total_amount > 0:
            try:
                order = self.create_trigger_sell(symbol, total_amount, sl)
                order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
                results["sl"] = {"stop": sl, "amount": total_amount, "order_id": order_id}
                logger.info(f"{symbol}: تم وضع أمر SL على المنصة - stop={sl} amount={total_amount} id={order_id}")
            except Exception as e:
                results["errors"].append(f"SL@{sl}: {e}")
                logger.error(f"{symbol}: فشل وضع أمر SL عند {sl}: {e}")

        return results

    def fetch_open_plan_orders(self, symbol: str):
        """جلب الأوامر الشرطية (plan orders) المفتوحة - مفيد للتشخيص والإلغاء."""
        try:
            return self.client.fetch_open_orders(symbol, params={"stop": True})
        except Exception as e:
            logger.error(f"{symbol}: فشل جلب الأوامر الشرطية: {e}")
            return []

    def cancel_plan_order(self, symbol: str, order_id: str):
        """إلغاء أمر شرطي (مثلاً SL لما اتباعت أجزاء منه بالفعل)."""
        try:
            if self.dry_run:
                logger.info(f"[DRY_RUN] CANCEL PLAN ORDER {order_id}")
                return {"id": order_id, "status": "dry_run"}
            return self.client.cancel_order(order_id, symbol)
        except Exception as e:
            logger.error(f"{symbol}: فشل إلغاء الأمر {order_id}: {e}")
            return None

    def replace_sl_order(self, symbol: str, old_order_id: str, new_stop: float, total_amount: float):
        """لما الستوب يتحرك (trailing): يلغي أمر SL القديم ويضع واحد جديد بسعر الستوب الجديد."""
        errors = []
        if old_order_id:
            try:
                self.cancel_plan_order(symbol, old_order_id)
            except Exception as e:
                errors.append(f"cancel SL: {e}")
        new_order = None
        try:
            new_order = self.create_trigger_sell(symbol, total_amount, new_stop)
        except Exception as e:
            errors.append(f"new SL: {e}")
            logger.error(f"{symbol}: فشل وضع أمر SL الجديد عند {new_stop}: {e}")
        return new_order, errors
