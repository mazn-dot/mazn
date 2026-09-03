"""
طبقة التواصل مع MEXC عبر مكتبة ccxt - تداول SPOT فقط.
مفيش رافعة مالية ومفيش أوامر reduceOnly/positions (دي مفاهيم فيوتشرز).
كل "مركز" هنا هو رصيد فعلي من العملة الأساسية تم شراؤه، وبيتقفل ببيعه.
"""
import logging
import ccxt

from .config import Config
from .state import shared_state

logger = logging.getLogger("exchange")


class MexcExchange:
    def __init__(self):
        params = {
            "apiKey": Config.MEXC_API_KEY,
            "secret": Config.MEXC_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                # نفضل استخدام quoteOrderQty للشراء بالـ USDT (أضمن على MEXC)
                "createMarketBuyOrderRequiresPrice": False,
            },
        }
        self.client = ccxt.mexc(params)

    @property
    def dry_run(self) -> bool:
        """يقرأ من shared_state عشان يتغير لايف عبر أوامر تليجرام (/dryrun_on, /dryrun_off)."""
        return shared_state.is_dry_run()

    def load_markets(self):
        return self.client.load_markets()

    def is_valid_spot_symbol(self, symbol: str) -> tuple[bool, str]:
        """
        يتأكد إن الرمز موجود فعلاً كسوق سبوت على MEXC قبل ما نضيفه للتداول.
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
            return False, f"الرمز {symbol} مش موجود على MEXC أصلاً.{hint}"

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

    def create_market_buy(self, symbol: str, amount: float = None, cost: float = None, max_retries: int = 2):
        """شراء سبوت بسعر السوق.
        يُفضل تمرير cost (مبلغ بالـ USDT) عبر quoteOrderQty — الطريقة الأضمن على MEXC.
        لو اتمرر amount فقط (كمية بالعملة الأساسية) بيتم استخدامه كـ fallback.
        """
        if self.dry_run:
            logger.info(f"[DRY_RUN] MARKET BUY amount={amount} cost={cost} {symbol}")
            return {
                "id": "dry-run",
                "symbol": symbol,
                "side": "buy",
                "amount": amount,
                "cost": cost,
                "status": "dry_run",
            }

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                if cost is not None and cost > 0:
                    # الطريقة المضمونة: شراء بمبلغ ثابت بالـ USDT
                    order = self.client.create_order(
                        symbol,
                        type="market",
                        side="buy",
                        amount=None,
                        params={"quoteOrderQty": float(cost)},
                    )
                    logger.info(f"{symbol}: MARKET BUY بـ {cost} USDT | order_id={order.get('id')}")
                    return order
                else:
                    # fallback: كمية بالعملة الأساسية
                    order = self.client.create_order(
                        symbol, type="market", side="buy", amount=amount
                    )
                    logger.info(f"{symbol}: MARKET BUY كمية={amount} | order_id={order.get('id')}")
                    return order
            except ccxt.InvalidOrder as e:
                last_error = e
                msg = str(e)
                if "createMarketBuyOrderRequiresPrice" not in msg and "quoteOrderQty" not in msg.lower():
                    raise
                if attempt >= max_retries:
                    break
                logger.warning(
                    f"ccxt طلب تعديل طريقة الشراء لـ {symbol} - "
                    f"محاولة احتياطية ({attempt}/{max_retries}): {e}"
                )
                try:
                    price = self.fetch_last_price(symbol)
                    if cost is not None and cost > 0:
                        fallback_amount = float(cost) / price
                        return self.client.create_order(
                            symbol, type="market", side="buy", amount=fallback_amount, price=price
                        )
                    return self.client.create_order(
                        symbol, type="market", side="buy", amount=amount, price=price
                    )
                except Exception as fetch_err:
                    raise ccxt.InvalidOrder(
                        f"{e} (كمان تعذر جلب السعر الحالي للمحاولة الاحتياطية: {fetch_err})"
                    )
            except Exception:
                raise
        raise last_error

    def round_amount_for_market(self, symbol: str, amount: float) -> float:
        """يقرب الكمية لأدق دقة مقبولة من المنصة عشان ما ترفضها (مثلاً MEXC
        يرفض كميات أدق من الحد الأدنى للدقة).
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
    # أوامر TP على المنصة = Limit Sell عادي (اللي MEXC Spot بيدعمه كويس)
    # الستوب لوس يفضل بالمراقبة الداخلية في البوت فقط (لأن Limit TP بيحجز الرصيد).
    # =========================================================================
    def round_price(self, symbol: str, price: float) -> float:
        """تقريب السعر لدقة المنصة."""
        try:
            market = self.client.markets.get(symbol)
            if not market:
                self.client.load_markets()
                market = self.client.markets.get(symbol) or {}
            precision = market.get("precision", {}).get("price")
            if precision is not None:
                return round(float(price), int(precision) if isinstance(precision, (int, float)) and precision < 20 else 8)
        except Exception:
            pass
        return float(price)

    def create_limit_sell(self, symbol: str, amount: float, price: float):
        """
        أمر بيع Limit عادي عند سعر الهدف (تيك بروفيت).
        ده الطريقة المضمونة على MEXC Spot — مش Trigger.
        """
        if self.dry_run:
            logger.info(f"[DRY_RUN] LIMIT SELL {amount} {symbol} @ {price}")
            return {
                "id": "dry-run",
                "symbol": symbol,
                "side": "sell",
                "type": "limit",
                "amount": amount,
                "price": price,
                "status": "dry_run",
            }
        amount = self.round_amount_for_market(symbol, amount)
        price = self.round_price(symbol, price)
        if amount <= 0:
            raise ValueError(f"كمية غير صالحة بعد التقريب: {amount}")
        if price <= 0:
            raise ValueError(f"سعر غير صالح: {price}")
        order = self.client.create_order(
            symbol, type="limit", side="sell", amount=amount, price=price
        )
        logger.info(f"{symbol}: LIMIT SELL (TP) amount={amount} @ {price} | id={order.get('id')}")
        return order

    def place_tp_sl_orders(self, symbol: str, legs: list[tuple[float, float]], sl: float | None):
        """
        يضع أوامر Limit Sell (تيك بروفيت) لكل جزء عند سعر الهدف.
        الستوب لوس **مش** بيتحط على المنصة (Limit TP بيحجز الرصيد كله)،
        الحماية الداخلية في البوت هي اللي بتبيع الباقي لما الستوب يضرب.
        """
        results = {"tp": [], "sl": None, "errors": []}
        total_amount = sum(a for a, _ in legs)

        for leg_amount, target in legs:
            try:
                order = self.create_limit_sell(symbol, leg_amount, target)
                order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
                results["tp"].append({"target": target, "amount": leg_amount, "order_id": order_id})
                logger.info(f"{symbol}: تم وضع أمر TP (Limit) على المنصة - target={target} amount={leg_amount} id={order_id}")
            except Exception as e:
                results["errors"].append(f"TP@{target}: {e}")
                logger.error(f"{symbol}: فشل وضع أمر TP عند {target}: {e}")

        # الستوب لوس داخلي فقط — Limit TP بيحجز الكمية فلازم ما نحطش SL على المنصة
        if sl is not None and total_amount > 0:
            logger.info(
                f"{symbol}: تم تخطي أمر SL على المنصة (stop={sl}) "
                "— أوامر Limit TP حجزت الرصيد. الحماية الداخلية شغالة."
            )
            results["sl"] = {"stop": sl, "amount": total_amount, "order_id": None, "skipped": True}

        return results

    def fetch_open_orders(self, symbol: str = None):
        """جلب الأوامر المفتوحة (Limit) على الرمز."""
        try:
            return self.client.fetch_open_orders(symbol)
        except Exception as e:
            logger.error(f"{symbol}: فشل جلب الأوامر المفتوحة: {e}")
            return []

    def cancel_order(self, symbol: str, order_id: str):
        """إلغاء أمر Limit (لما الستوب يضرب أو نحتاج نلغي الباقي)."""
        try:
            if self.dry_run:
                logger.info(f"[DRY_RUN] CANCEL ORDER {order_id}")
                return {"id": order_id, "status": "dry_run"}
            return self.client.cancel_order(order_id, symbol)
        except Exception as e:
            logger.error(f"{symbol}: فشل إلغاء الأمر {order_id}: {e}")
            return None

    def cancel_all_open_orders(self, symbol: str):
        """يلغي كل الأوامر المفتوحة على الرمز (مفيد لما الستوب يضرب)."""
        cancelled = []
        try:
            opens = self.fetch_open_orders(symbol)
            for o in opens:
                oid = o.get("id")
                if oid:
                    self.cancel_order(symbol, oid)
                    cancelled.append(oid)
        except Exception as e:
            logger.error(f"{symbol}: فشل إلغاء الأوامر المفتوحة: {e}")
        return cancelled
