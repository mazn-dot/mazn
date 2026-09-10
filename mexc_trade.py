"""
MEXC Spot trading helper.
Requires env: MEXC_API_KEY, MEXC_API_SECRET
"""
import hashlib
import hmac
import os
import time
import logging
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

BASE = "https://api.mexc.com"
API_KEY = os.getenv("MEXC_API_KEY", "")
API_SECRET = os.getenv("MEXC_API_SECRET", "")


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


def get_price(symbol: str) -> float:
    """symbol like BTCUSDT"""
    data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol.upper()})
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


def market_buy(symbol: str, quote_usd: float):
    """
    Buy with quote amount (USDT).
    symbol example: SOCKUSDT
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": round(quote_usd, 4),
    }
    return _request("POST", "/api/v3/order", params, signed=True)


def market_sell(symbol: str, quantity: float):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    # MEXC needs quantity precision — keep it simple
    qty = round(quantity, 6)
    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
    }
    return _request("POST", "/api/v3/order", params, signed=True)


def resolve_symbol(token_symbol: str) -> str:
    """Try TOKENUSDT pair."""
    return token_symbol.upper().replace(" ", "") + "USDT"
