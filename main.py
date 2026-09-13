#!/usr/bin/env python3
"""
MEXC SPOT Auto Trader - Single File
- Reads public Telegram channel via Web Preview only (no API ID/Hash)
- SPOT BUY only for LONG signals (ignores SHORT + leverage)
- Control via your Telegram Bot
- Paper mode default = True
- Designed for Railway 24/7
"""

import os
import sys
import time
import json
import re
import hashlib
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import ccxt
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIG ====================
CHANNEL = "abojasimp"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
STATE_FILE = Path("state.json")
LOG_FILE = Path("trader.log")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "").strip()
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "").strip()

TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "10"))
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() in ("true", "1", "yes")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() in ("true", "1", "yes")
POLL_INTERVAL = max(3, int(os.getenv("POLL_INTERVAL", "5")))
MAX_TRADE_AMOUNT = 500.0  # safety hard limit

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("MEXC-SPOT-TRADER")

# ==================== STATE ====================
class State:
    def __init__(self):
        self.last_msg_id: int = 0
        self.processed_hashes: List[str] = []
        self.positions: Dict[str, Dict] = {}  # symbol -> position data
        self.trading_enabled: bool = TRADING_ENABLED
        self.paper_mode: bool = PAPER_MODE
        self.trade_amount: float = TRADE_AMOUNT_USDT
        self.last_signal: Optional[Dict] = None
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.last_msg_id = int(data.get("last_msg_id", 0))
                self.processed_hashes = data.get("processed_hashes", [])[-200:]
                self.positions = data.get("positions", {})
                self.trading_enabled = data.get("trading_enabled", TRADING_ENABLED)
                self.paper_mode = data.get("paper_mode", PAPER_MODE)
                self.trade_amount = float(data.get("trade_amount", TRADE_AMOUNT_USDT))
                self.last_signal = data.get("last_signal")
                logger.info(f"State loaded | last_msg_id={self.last_msg_id} | positions={len(self.positions)}")
            except Exception as e:
                logger.error(f"Failed to load state: {e}")

    def save(self):
        try:
            data = {
                "last_msg_id": self.last_msg_id,
                "processed_hashes": self.processed_hashes[-200:],
                "positions": self.positions,
                "trading_enabled": self.trading_enabled,
                "paper_mode": self.paper_mode,
                "trade_amount": self.trade_amount,
                "last_signal": self.last_signal,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

state = State()

# ==================== TELEGRAM BOT HELPERS ====================
def tg_send(text: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_ADMIN_ID,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"tg_send error: {e}")
        return False

def tg_get_updates(offset: int = 0) -> List[Dict]:
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"offset": offset, "timeout": 10, "limit": 20}
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        logger.debug(f"getUpdates error: {e}")
    return []

# ==================== CHANNEL SCRAPER (Web Preview only) ====================
def scrape_channel() -> List[Dict]:
    """Fetch latest messages from public channel web preview. No login, no API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    try:
        r = requests.get(CHANNEL_URL, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        messages = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg = wrap.select_one(".tgme_widget_message")
            if not msg:
                continue
            # Message ID from data-post="channel/12345"
            data_post = msg.get("data-post", "")
            msg_id = 0
            if "/" in data_post:
                try:
                    msg_id = int(data_post.split("/")[-1])
                except:
                    pass
            if msg_id == 0:
                # fallback from link
                link = msg.select_one("a.tgme_widget_message_date")
                if link and link.get("href"):
                    m = re.search(r"/(\d+)$", link["href"])
                    if m:
                        msg_id = int(m.group(1))
            text_el = msg.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n", strip=True) if text_el else ""
            if msg_id > 0 and text:
                messages.append({"id": msg_id, "text": text})
        # newest first usually, sort ascending by id
        messages.sort(key=lambda x: x["id"])
        return messages
    except Exception as e:
        logger.error(f"Scrape error: {e}")
        return []

# ==================== SIGNAL PARSER (flexible) ====================
def normalize_symbol(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.upper().strip()
    s = re.sub(r"[#$]", "", s)
    s = s.replace("USDT", "").replace("USD", "").replace("/", "").replace("-", "")
    s = re.sub(r"[^A-Z0-9]", "", s)
    if len(s) < 2 or len(s) > 15:
        return None
    return f"{s}/USDT"

def parse_signal(text: str) -> Optional[Dict]:
    """Flexible parser. Returns None if not a clear trading signal."""
    if not text or len(text) < 10:
        return None
    original = text
    text_lower = text.lower()

    # Must look like a signal
    has_direction = bool(re.search(r"\b(long|short|buy|sell)\b", text_lower))
    has_price_like = bool(re.search(r"\d+\.?\d*", text))
    if not (has_direction and has_price_like):
        return None

    # Direction
    direction = None
    if re.search(r"\b(long|buy|شراء|لونج)\b", text_lower):
        direction = "LONG"
    elif re.search(r"\b(short|sell|بيع|شورت)\b", text_lower):
        direction = "SHORT"
    if not direction:
        return None

    # Leverage (ignored later)
    lev_match = re.search(r"(?:leverage|lev|x|×)\s*[:=]?\s*(\d+)", text_lower)
    leverage = int(lev_match.group(1)) if lev_match else None

    # Prices
    def find_price(patterns):
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except:
                    pass
        return None

    entry = find_price([
        r"(?:enter|entry|دخول|سعر الدخول|entry\s*price)\s*[:=]?\s*([\d.]+)",
        r"(?:enter|entry)\s+([\d.]+)",
    ])
    target = find_price([
        r"(?:target|tp|هدف|تارجيت|take\s*profit)\s*[:=]?\s*([\d.]+)",
        r"(?:target|tp)\s+([\d.]+)",
    ])
    stop = find_price([
        r"(?:stop|sl|وقف|ستوب|stop\s*loss)\s*[:=]?\s*([\d.]+)",
        r"(?:stop|sl)\s+([\d.]+)",
    ])

    # Symbol - smart extraction
    symbol = None
    # Try common patterns first
    patterns = [
        r"(?:#|\$)?([A-Z]{2,12})(?:USDT|/USDT|\s+USDT)?",
        r"([A-Z]{2,12})\s*/\s*USDT",
        r"pair\s*[:=]?\s*([A-Z0-9]+)",
        r"coin\s*[:=]?\s*([A-Z0-9]+)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            candidate = normalize_symbol(m.group(1))
            if candidate:
                symbol = candidate
                break

    # Fallback: look for all-caps words that look like tickers
    if not symbol:
        candidates = re.findall(r"\b([A-Z]{2,10})\b", text)
        for c in candidates:
            if c not in ("LONG", "SHORT", "USDT", "USD", "ENTER", "TARGET", "STOP", "LEVERAGE", "SIGN"):
                candidate = normalize_symbol(c)
                if candidate:
                    symbol = candidate
                    break

    if not symbol or not direction:
        return None

    # Basic validation
    if entry and entry <= 0:
        return None

    signal = {
        "symbol": symbol,
        "direction": direction,
        "leverage": leverage,
        "entry": entry,
        "target": target,
        "stop": stop,
        "raw": original[:500],
    }
    return signal

def signal_hash(sig: Dict, msg_id: int) -> str:
    s = f"{msg_id}|{sig['symbol']}|{sig['direction']}|{sig.get('entry')}|{sig.get('target')}|{sig.get('stop')}"
    return hashlib.sha256(s.encode()).hexdigest()[:16]

# ==================== MEXC SPOT ====================
exchange: Optional[ccxt.Exchange] = None

def init_exchange() -> bool:
    global exchange
    if not MEXC_API_KEY or not MEXC_SECRET_KEY:
        logger.warning("MEXC keys missing - exchange not initialized")
        return False
    try:
        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_SECRET_KEY,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},  # CRITICAL: spot only
        })
        exchange.load_markets()
        # sanity check
        bal = exchange.fetch_balance()
        logger.info("MEXC Spot connected successfully")
        return True
    except Exception as e:
        logger.error(f"MEXC init failed: {e}")
        exchange = None
        return False

def get_usdt_balance() -> float:
    if not exchange:
        return 0.0
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0) or 0)
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0

def market_exists(symbol: str) -> bool:
    if not exchange:
        return False
    return symbol in exchange.markets and exchange.markets[symbol].get("spot", False)

def calculate_amount(symbol: str, usdt_amount: float) -> Optional[float]:
    """Return base amount that respects precision, min amount, min cost."""
    if not exchange or not market_exists(symbol):
        return None
    try:
        market = exchange.markets[symbol]
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker["last"] or ticker.get("close") or 0)
        if price <= 0:
            return None
        amount = usdt_amount / price
        # precision
        amount = float(exchange.amount_to_precision(symbol, amount))
        min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0
        min_cost = market.get("limits", {}).get("cost", {}).get("min") or 0
        if amount < min_amount:
            logger.warning(f"Amount {amount} < min_amount {min_amount}")
            return None
        cost = amount * price
        if cost < min_cost:
            logger.warning(f"Cost {cost} < min_cost {min_cost}")
            return None
        return amount
    except Exception as e:
        logger.error(f"calculate_amount error: {e}")
        return None

def execute_spot_buy(symbol: str, usdt_amount: float, signal: Dict) -> Optional[Dict]:
    """Real or paper SPOT market buy."""
    if state.paper_mode:
        # simulate
        price = signal.get("entry") or 0
        if not price and exchange and market_exists(symbol):
            try:
                price = float(exchange.fetch_ticker(symbol)["last"])
            except:
                price = 1.0
        qty = usdt_amount / price if price > 0 else 0
        order = {
            "id": f"PAPER-{int(time.time())}",
            "symbol": symbol,
            "side": "buy",
            "amount": qty,
            "price": price,
            "cost": usdt_amount,
            "status": "closed",
            "paper": True,
        }
        logger.info(f"[PAPER] BUY {symbol} ~{qty:.6f} @ {price}")
        return order

    if not exchange:
        return None
    if not market_exists(symbol):
        logger.error(f"Symbol {symbol} not in MEXC Spot")
        return None
    amount = calculate_amount(symbol, usdt_amount)
    if not amount:
        return None
    free = get_usdt_balance()
    if free < usdt_amount * 1.01:  # small buffer
        logger.error(f"Insufficient USDT: {free} < {usdt_amount}")
        return None
    try:
        order = exchange.create_order(symbol, "market", "buy", amount)
        logger.info(f"SPOT BUY executed: {order.get('id')}")
        return order
    except Exception as e:
        logger.error(f"Order failed: {e}")
        tg_send(f"❌ ERROR\nOrder failed for {symbol}\n{str(e)[:300]}")
        return None

def execute_spot_sell(symbol: str, amount: float, reason: str) -> Optional[Dict]:
    if state.paper_mode:
        order = {
            "id": f"PAPER-SELL-{int(time.time())}",
            "symbol": symbol,
            "side": "sell",
            "amount": amount,
            "status": "closed",
            "paper": True,
            "reason": reason,
        }
        logger.info(f"[PAPER] SELL {symbol} {amount} reason={reason}")
        return order
    if not exchange:
        return None
    try:
        amount = float(exchange.amount_to_precision(symbol, amount))
        order = exchange.create_order(symbol, "market", "sell", amount)
        logger.info(f"SPOT SELL executed: {order.get('id')} reason={reason}")
        return order
    except Exception as e:
        logger.error(f"Sell failed: {e}")
        tg_send(f"❌ ERROR\nSell failed {symbol}\n{str(e)[:300]}")
        return None

# ==================== POSITION MANAGEMENT ====================
def open_position(signal: Dict, order: Dict):
    symbol = signal["symbol"]
    qty = float(order.get("amount") or order.get("filled") or 0)
    price = float(order.get("price") or order.get("average") or signal.get("entry") or 0)
    state.positions[symbol] = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": price,
        "target": signal.get("target"),
        "stop": signal.get("stop"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "order_id": order.get("id"),
        "paper": state.paper_mode,
        "signal": signal,
    }
    state.save()

def check_positions():
    if not state.positions:
        return
    to_close = []
    for symbol, pos in list(state.positions.items()):
        try:
            if state.paper_mode or not exchange:
                # simple paper check using last known or skip real price
                current = pos.get("entry_price", 0)
            else:
                ticker = exchange.fetch_ticker(symbol)
                current = float(ticker["last"])
            target = pos.get("target")
            stop = pos.get("stop")
            reason = None
            if target and current >= target:
                reason = "TARGET"
            elif stop and current <= stop:
                reason = "STOP"
            if reason:
                order = execute_spot_sell(symbol, pos["qty"], reason)
                if order:
                    to_close.append(symbol)
                    msg = f"{'🎯 TARGET HIT' if reason == 'TARGET' else '🛑 STOP HIT'}\n"
                    msg += f"Coin: {symbol}\nPrice: {current}\nQty: {pos['qty']}\n"
                    if state.paper_mode:
                        msg += "(PAPER MODE)"
                    tg_send(msg)
        except Exception as e:
            logger.error(f"Position check {symbol}: {e}")
    for s in to_close:
        del state.positions[s]
    if to_close:
        state.save()

# ==================== PROCESS NEW SIGNAL ====================
def process_signal(msg: Dict):
    msg_id = msg["id"]
    text = msg["text"]
    if msg_id <= state.last_msg_id:
        return
    sig = parse_signal(text)
    if not sig:
        state.last_msg_id = max(state.last_msg_id, msg_id)
        state.save()
        return

    h = signal_hash(sig, msg_id)
    if h in state.processed_hashes:
        logger.info(f"Duplicate hash {h}, skip")
        state.last_msg_id = max(state.last_msg_id, msg_id)
        state.save()
        return

    # Only LONG
    if sig["direction"] != "LONG":
        logger.info(f"Ignoring SHORT signal {sig['symbol']}")
        state.processed_hashes.append(h)
        state.last_msg_id = max(state.last_msg_id, msg_id)
        state.save()
        tg_send(f"ℹ️ Signal ignored (SHORT)\n{sig['symbol']}")
        return

    logger.info(f"🔥 NEW SIGNAL DETECTED | {sig['symbol']} LONG")
    state.last_signal = sig
    state.processed_hashes.append(h)
    state.last_msg_id = max(state.last_msg_id, msg_id)
    state.save()

    # Notify
    lev_txt = f"{sig['leverage']}x (ignored)" if sig.get("leverage") else "none"
    notify = (
        f"🚨 توصية جديدة\n\n"
        f"Coin: <b>{sig['symbol']}</b>\n"
        f"Signal: LONG\n"
        f"Entry: {sig.get('entry') or 'market'}\n"
        f"Target: {sig.get('target') or '-'}\n"
        f"Stop: {sig.get('stop') or '-'}\n"
        f"Leverage in signal: {lev_txt}\n"
        f"Mode: SPOT\n"
        f"Paper: {state.paper_mode}\n"
        f"Trading: {state.trading_enabled}"
    )
    tg_send(notify)

    if not state.trading_enabled:
        logger.info("Trading disabled - signal recorded only")
        return

    # Safety checks
    amount = min(state.trade_amount, MAX_TRADE_AMOUNT)
    if amount <= 0:
        tg_send("❌ ERROR: trade amount invalid")
        return

    if not state.paper_mode:
        if not exchange:
            tg_send("❌ ERROR: MEXC not connected")
            return
        if not market_exists(sig["symbol"]):
            tg_send(f"❌ ERROR: {sig['symbol']} not available on MEXC Spot")
            return
        if get_usdt_balance() < amount * 1.02:
            tg_send(f"❌ ERROR: Insufficient USDT balance")
            return

    # Execute
    logger.info(f"Executing SPOT BUY {sig['symbol']} amount={amount} USDT")
    order = execute_spot_buy(sig["symbol"], amount, sig)
    if order:
        open_position(sig, order)
        fill_price = order.get("price") or order.get("average") or sig.get("entry")
        qty = order.get("amount") or order.get("filled")
        confirm = (
            f"✅ تم تنفيذ SPOT BUY\n\n"
            f"Coin: <b>{sig['symbol']}</b>\n"
            f"Amount: {amount} USDT\n"
            f"Quantity: {qty}\n"
            f"Price: {fill_price}\n"
            f"Order ID: {order.get('id')}\n"
            f"{'(PAPER MODE)' if state.paper_mode else ''}"
        )
        tg_send(confirm)
    else:
        tg_send(f"❌ ERROR: Failed to execute buy for {sig['symbol']}")

# ==================== BOT COMMANDS ====================
last_update_id = 0

def handle_command(text: str, chat_id: str):
    global last_update_id
    if str(chat_id) != str(TELEGRAM_ADMIN_ID):
        return
    cmd = text.strip().split()
    if not cmd:
        return
    c = cmd[0].lower()

    if c == "/start":
        tg_send(
            "🤖 MEXC SPOT Auto Trader\n\n"
            "Commands:\n"
            "/status /on /off /balance /positions\n"
            "/amount <n> /paper /settings /last /stop\n"
            "/test"
        )
    elif c == "/status":
        mexc_ok = "✅" if exchange else "❌"
        bal = get_usdt_balance() if exchange else 0
        msg = (
            f"📊 STATUS\n\n"
            f"Trading: {'ON ✅' if state.trading_enabled else 'OFF ⛔'}\n"
            f"Paper Mode: {'ON 📝' if state.paper_mode else 'OFF 🔴'}\n"
            f"MEXC: {mexc_ok}\n"
            f"Channel: @{CHANNEL}\n"
            f"Trade Amount: {state.trade_amount} USDT\n"
            f"Last Msg ID: {state.last_msg_id}\n"
            f"Open Positions: {len(state.positions)}\n"
            f"USDT Free: {bal:.2f}\n"
            f"Last Signal: {state.last_signal['symbol'] if state.last_signal else '-'}"
        )
        tg_send(msg)
    elif c == "/on":
        state.trading_enabled = True
        state.save()
        tg_send("✅ Trading ENABLED")
    elif c == "/off":
        state.trading_enabled = False
        state.save()
        tg_send("⛔ Trading DISABLED")
    elif c == "/stop":
        state.trading_enabled = False
        state.save()
        tg_send("🛑 KILL SWITCH - Trading stopped immediately")
    elif c == "/paper":
        state.paper_mode = not state.paper_mode
        state.save()
        tg_send(f"Paper Mode now: {'ON' if state.paper_mode else 'OFF'}")
    elif c == "/balance":
        if not exchange:
            tg_send("MEXC not connected")
            return
        try:
            bal = exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            msg = f"💰 Balance\nUSDT free: {usdt.get('free', 0)}\nUSDT total: {usdt.get('total', 0)}"
            # show a few non-zero
            for k, v in list(bal.items())[:15]:
                if isinstance(v, dict) and float(v.get("total") or 0) > 0 and k != "USDT":
                    msg += f"\n{k}: {v.get('total')}"
            tg_send(msg)
        except Exception as e:
            tg_send(f"Error: {e}")
    elif c == "/positions":
        if not state.positions:
            tg_send("No open positions")
            return
        msg = "📈 Open Positions\n"
        for s, p in state.positions.items():
            msg += f"\n{s}\nQty: {p['qty']}\nEntry: {p['entry_price']}\nTP: {p.get('target')}\nSL: {p.get('stop')}\n"
        tg_send(msg)
    elif c == "/amount":
        if len(cmd) < 2:
            tg_send(f"Current amount: {state.trade_amount} USDT\nUsage: /amount 15")
            return
        try:
            val = float(cmd[1])
            if val <= 0 or val > MAX_TRADE_AMOUNT:
                tg_send(f"Amount must be 0 < x <= {MAX_TRADE_AMOUNT}")
                return
            state.trade_amount = val
            state.save()
            tg_send(f"✅ Trade amount set to {val} USDT")
        except:
            tg_send("Invalid number")
    elif c == "/settings":
        tg_send(
            f"Settings\n"
            f"Amount: {state.trade_amount}\n"
            f"Paper: {state.paper_mode}\n"
            f"Trading: {state.trading_enabled}\n"
            f"Poll: {POLL_INTERVAL}s\n"
            f"Max amount hard limit: {MAX_TRADE_AMOUNT}"
        )
    elif c == "/last":
        if state.last_signal:
            s = state.last_signal
            tg_send(f"Last signal:\n{s['symbol']} {s['direction']}\nEntry: {s.get('entry')}\nTarget: {s.get('target')}\nStop: {s.get('stop')}")
        else:
            tg_send("No last signal")
    elif c == "/test":
        tg_send("Bot is alive ✅\nScraping channel...")
        msgs = scrape_channel()
        tg_send(f"Scraped {len(msgs)} recent messages\nLatest ID: {msgs[-1]['id'] if msgs else 0}")
    else:
        tg_send("Unknown command. /start for help")

def poll_bot():
    global last_update_id
    updates = tg_get_updates(last_update_id + 1)
    for u in updates:
        last_update_id = max(last_update_id, u["update_id"])
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "")
        if text.startswith("/"):
            handle_command(text, chat_id)

# ==================== MAIN LOOP ====================
def print_banner():
    print("=" * 50)
    print("MEXC SPOT AUTO TRADER")
    print()
    print(f"Telegram Channel: @{CHANNEL}")
    print("Telegram API ID: NOT USED")
    print("Telegram API HASH: NOT USED")
    print()
    print("Trading Mode: SPOT ONLY")
    print("Futures: DISABLED")
    print("Leverage: DISABLED")
    print("Short: DISABLED")
    print()
    print(f"Paper Mode: {str(state.paper_mode).upper()}")
    print(f"Trading: {'ON' if state.trading_enabled else 'OFF'}")
    print()
    print("Bot Control: ACTIVE" if TELEGRAM_BOT_TOKEN else "Bot Control: MISSING TOKEN")
    print()
    print("Monitoring channel...")
    print("=" * 50)

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID are required")
        sys.exit(1)

    print_banner()
    init_exchange()
    tg_send(
        f"🚀 Bot started\n"
        f"Paper: {state.paper_mode}\n"
        f"Trading: {state.trading_enabled}\n"
        f"Amount: {state.trade_amount} USDT\n"
        f"Last Msg ID: {state.last_msg_id}"
    )

    # On first run (last_msg_id==0) set to current max so we skip old messages
    if state.last_msg_id == 0:
        msgs = scrape_channel()
        if msgs:
            state.last_msg_id = max(m["id"] for m in msgs)
            state.save()
            logger.info(f"First run: set last_msg_id to {state.last_msg_id} (skipping history)")

    consecutive_errors = 0
    while True:
        try:
            # 1. Poll bot commands
            poll_bot()

            # 2. Scrape new messages
            messages = scrape_channel()
            for msg in messages:
                if msg["id"] > state.last_msg_id:
                    process_signal(msg)

            # 3. Manage open positions (TP/SL)
            check_positions()

            consecutive_errors = 0
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            state.save()
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Main loop error: {e}\n{traceback.format_exc()}")
            if consecutive_errors >= 10:
                tg_send(f"❌ Too many errors, sleeping 60s\n{str(e)[:200]}")
                time.sleep(60)
                consecutive_errors = 0
            else:
                time.sleep(min(30, consecutive_errors * 3))

if __name__ == "__main__":
    main()
