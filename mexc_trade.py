"""
MEXC Spot trading helper.
Requires env: MEXC_API_KEY, MEXC_API_SECRET
"""
import hashlib
import hmac
import os
import time
import logging
import math
from decimal import Decimal, ROUND_DOWN, getcontext
from urllib.parse import urlencode

import requests

getcontext().prec = 28

log = logging.getLogger(__name__)

BASE = "https://api.mexc.com"
API_KEY = os.getenv("MEXC_API_KEY", "")
API_SECRET = os.getenv("MEXC_API_SECRET", "")

# cache for symbol filters
_symbol_info = {}


def _sign(query: str) -> str:
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _request(method, path, params=None, signed=False):
    params = dict(params or {})
    headers = {"X-MEXC-APIKEY": API_KEY, "Content-Type": "application/json"}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        params["signature"] = _sign(query)
    url = BASE + path
    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=15)
        else:
            r = requests.post(url, params=params, headers=headers, timeout=15)
        data = r.json()
        if r.status_code != 200:
            log.warning("MEXC error %s: %s", r.status_code, data)
        return data
    except Exception as e:
        log.exception("MEXC request failed: %s", e)
        return {"error": str(e)}


def resolve_symbol(token_symbol: str) -> str:
    symbol = str(token_symbol or "").upper().replace(" ", "").replace("-", "")
    symbol = symbol.split("/", 1)[0]
    if symbol.endswith("USDT"):
        symbol = symbol[:-4]
    return symbol + "USDT"


def get_price(symbol: str) -> float:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol})
    try:
        return float(data.get("price", 0))
    except (TypeError, ValueError):
        return 0.0


def get_balance(asset: str = "USDT") -> float:
    data = _request("GET", "/api/v3/account", signed=True)
    if "balances" not in data:
        return 0.0
    for b in data["balances"]:
        if b.get("asset", "").upper() == asset.upper():
            return float(b.get("free", 0))
    return 0.0


def _load_symbol_info(symbol: str):
    """Fetch lot size / step size for the symbol."""
    symbol = symbol.upper()
    if symbol in _symbol_info:
        return _symbol_info[symbol]
    data = _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
    info = {"stepSize": 0.000001, "minQty": 0.0, "tickSize": 0.000001, "baseAsset": ""}
    try:
        symbols = data.get("symbols") or []
        if not symbols and data.get("symbol"):
            symbols = [data]
        for s in symbols:
            if s.get("symbol", "").upper() == symbol:
                info["baseAsset"] = s.get("baseAsset", "")
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        info["stepSize"] = float(f.get("stepSize", 0.000001))
                        info["minQty"] = float(f.get("minQty", 0))
                    if f.get("filterType") == "PRICE_FILTER":
                        info["tickSize"] = float(f.get("tickSize", 0.000001))
                break
    except Exception as e:
        log.warning("exchangeInfo failed for %s: %s", symbol, e)
    _symbol_info[symbol] = info
    return info


def _round_step(qty: float, step: float) -> float:
    """Round quantity down to valid step size using Decimal for accuracy."""
    if step <= 0:
        return float(Decimal(str(qty)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
    d_qty = Decimal(str(qty))
    d_step = Decimal(str(step))
    # floor to nearest step
    rounded = (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    # determine display precision from step
    step_str = f"{step:.10f}".rstrip("0")
    if "." in step_str:
        precision = len(step_str.split(".")[1])
    else:
        precision = 0
    return float(rounded.quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN))


def adjust_quantity(symbol: str, quantity: float) -> float:
    """Normalize quantity to exchange LOT_SIZE rules. Returns 0 if below minQty."""
    if quantity <= 0:
        return 0.0
    info = _load_symbol_info(symbol)
    step = info.get("stepSize") or 0.000001
    min_qty = info.get("minQty") or 0
    qty = _round_step(quantity, step)
    if qty < min_qty:
        return 0.0
    return qty


def market_buy(symbol: str, quote_usd: float):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    quote_usd = float(quote_usd)
    if quote_usd < 1:
        return {"error": "amount too small", "min": 1}

    # تحقق إن الزوج موجود وله سعر
    price = get_price(symbol)
    if price <= 0:
        return {"error": f"no price for {symbol} — may be delisted or not on MEXC spot"}

    info = _load_symbol_info(symbol)
    # بعض الأزواج بتحتاج quantity بدل quoteOrderQty
    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": round(quote_usd, 4),
    }
    result = _request("POST", "/api/v3/order", params, signed=True)

    # لو فشل بـ quoteOrderQty، جرب بالكمية
    if isinstance(result, dict) and (result.get("code") or result.get("error")):
        qty = adjust_quantity(symbol, quote_usd / price)
        if qty > 0:
            params2 = {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
            }
            result2 = _request("POST", "/api/v3/order", params2, signed=True)
            if not (isinstance(result2, dict) and (result2.get("code") or result2.get("error"))):
                return result2
        log.warning("market_buy failed for %s: %s", symbol, result)
    return result


def _fmt_qty(qty: float, max_decimals: int = 8) -> str:
    """Format quantity string without scientific notation and trailing zeros."""
    s = f"{qty:.{max_decimals}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def market_sell(symbol: str, quantity: float):
    """
    Market sell with robust quantity normalization.
    Tries exchange stepSize first, then progressively coarser precision on scale errors.
    On Oversold, re-fetches real free balance and retries once with the live amount.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    original = float(quantity)
    qty = adjust_quantity(symbol, original)
    if qty <= 0:
        return {"error": "quantity too small after rounding", "original": original}

    def _try_sell(q: float):
        q = adjust_quantity(symbol, q)
        if q <= 0:
            return {"error": "quantity too small after rounding", "original": q}
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": _fmt_qty(q),
        }
        return _request("POST", "/api/v3/order", params, signed=True)

    result = _try_sell(qty)

    # Handle quantity scale / precision errors by trying coarser decimals
    msg = str(result.get("msg", "")).lower() if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("code") == 400 and ("scale" in msg or "precision" in msg or "lot" in msg):
        for decimals in (6, 5, 4, 3, 2, 1, 0):
            factor = 10 ** decimals
            qty2 = math.floor(original * factor) / factor
            if qty2 <= 0:
                continue
            result = _try_sell(qty2)
            msg2 = str(result.get("msg", "")).lower() if isinstance(result, dict) else ""
            if not (isinstance(result, dict) and result.get("code") == 400 and ("scale" in msg2 or "precision" in msg2)):
                break

    # Handle Oversold: re-read free balance and sell whatever is actually free
    if isinstance(result, dict) and (result.get("code") == 30005 or "oversold" in str(result.get("msg", "")).lower()):
        live = get_base_balance(symbol)
        if live > 0:
            # leave a tiny dust buffer to avoid residual oversold
            safe = live * 0.999
            result = _try_sell(safe)
            if isinstance(result, dict) and (result.get("code") == 30005 or "oversold" in str(result.get("msg", "")).lower()):
                # last resort: even smaller
                result = _try_sell(live * 0.995)

    return result


def get_base_balance(symbol: str) -> float:
    """رصيد العملة الأساسية (مثلاً SOCK من SOCKUSDT)"""
    info = _load_symbol_info(symbol if symbol.endswith("USDT") else symbol + "USDT")
    base = info.get("baseAsset") or symbol.replace("USDT", "")
    return get_balance(base)
