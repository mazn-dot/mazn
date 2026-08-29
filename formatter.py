import html
from config import TIME_PERIODS, TOP_N
from tracker import get_usd_prices
STABLE = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP", "FRAX", "LUSD", "MIM", "HAY"}
LARGE = {"BNB", "WBNB", "ETH", "WETH", "BTC", "BTCB", "XRP", "SOL", "ADA", "DOT", "MATIC", "LINK", "UNI", "CAKE", "AAVE", "AVAX", "ATOM"}
MIN_TRANSFERS = 1
SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━"
SEP2 = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
WIN = {5: "5د", 15: "15د", 30: "30د", 60: "1س", 120: "2س", 240: "4س", 360: "6س", 480: "8س", 720: "12س", 1440: "24س"}

def esc(x): return html.escape(str(x), quote=True)
def safe(a):
  s = str(a).lower(); s = s[2:] if s.startswith("0x") else s
  return "0x" + "".join(c for c in s if c in "0123456789abcdef")[:40]
def dex(c): return '<a href="https://dexscreener.com/bsc/' + safe(c) + '">📊</a>'
def scan(c): return '<a href="https://bscscan.com/token/' + safe(c) + '">🔍</a>'
def addr(c): return '<a href="https://bscscan.com/address/' + safe(c) + '">🔗</a>'
def skip(s): return str(s).upper().strip() in STABLE | LARGE
def qty(v):
  v = float(v or 0)
  if v >= 1e9: return "%.2fB" % (v / 1e9)
  if v >= 1e6: return "%.2fM" % (v / 1e6)
  if v >= 1e3: return "%.2fK" % (v / 1e3)
  return "%.2f" % v if v >= 1 else "%.6f" % v
def money(v):
  v = float(v or 0)
  if v <= 0: return ""
  if v >= 1e9: return "$%.2fB" % (v / 1e9)
  if v >= 1e6: return "$%.2fM" % (v / 1e6)
  if v >= 1e3: return "$%.1fK" % (v / 1e3)
  return "$%.2f" % v
def period(m): return next((x for x, n in TIME_PERIODS if n == m), str(m) + "د")
def line(i, x, amount, count, prices):
  symbol, name, contract = x.get("symbol", "???"), x.get("name", ""), x.get("contract", "")
  title = '<code>' + esc(symbol) + '</code>' + (("  <b>" + esc(name) + "</b>") if name and name.upper() != symbol.upper() else "")
  val = money(prices.get(contract.lower(), 0) * amount); val = "  <b>" + val + "</b>" if val else ""
  medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i) + "."
  return "%s  %s\n     %s%s  •  %s تحويل  •  %s %s" % (medal, title, qty(amount), val, count, dex(contract), scan(contract))
def filtered(items): return [x for x in items if x.get("count", x.get("total_count", 0)) >= MIN_TRANSFERS and not skip(x.get("symbol", ""))]
def format_report(data, direction, minutes):
  title = "📥 الإيداعات" if direction == "in" else "📤 السحوبات"; lines = [SEP, title + "  |  " + period(minutes), SEP]; combined = {}
  for label, wallet in data.items():
      if label == "_meta": continue
      for key, x in wallet.get("tokens", {}).items():
          c = x.get("contract", "").lower()
          if c: combined.setdefault(c, {"contract": c, "symbol": x.get("symbol", key), "name": x.get("name", ""), "amount": 0, "count": 0}); combined[c]["amount"] += x.get("amount", 0); combined[c]["count"] += x.get("count", 0)
  tokens = [x for x in combined.values() if x["count"] >= MIN_TRANSFERS and not skip(x["symbol"])]; tokens.sort(key=lambda x: x["amount"], reverse=True); tokens = tokens[:TOP_N]
  if not tokens: return "\n".join(lines + ["لا توجد عملات تطابق الفلتر.", SEP])
  prices = get_usd_prices([x["contract"] for x in tokens])
  for i, x in enumerate(tokens, 1): lines += [line(i, x, x["amount"], x["count"], prices), ""]
  return "\n".join(lines + [SEP])
def format_opportunity(data, minutes):
  tokens = filtered(data.get("ranked", [])); tokens.sort(key=lambda x: x.get("amount", x.get("total_amount", 0)), reverse=True); tokens = tokens[:TOP_N]; lines = [SEP, "🎯 الفرصة  |  " + period(minutes), SEP]
  if not tokens: return "\n".join(lines + ["لا توجد سحوبات تطابق الفلتر.", SEP]), ""
  clean = [{"contract": x["contract"], "symbol": x.get("symbol", "???"), "name": x.get("name", ""), "amount": x.get("amount", x.get("total_amount", 0)), "count": x.get("count", x.get("total_count", 0))} for x in tokens]; prices = get_usd_prices([x["contract"] for x in clean])
  for i, x in enumerate(clean, 1): lines += [line(i, x, x["amount"], x["count"], prices), ""]
  return "\n".join(lines + [SEP]), "https://dexscreener.com/bsc/" + safe(clean[0]["contract"])
def format_clean_opportunity(data, minutes):
  clean, tainted = filtered(data.get("clean", [])), filtered(data.get("tainted", [])); lines = [SEP, "🚫 الفرصة النقية  |  " + period(minutes), SEP]; url = ""
  if clean:
      url = "https://dexscreener.com/bsc/" + safe(clean[0]["contract"]); prices = get_usd_prices([x["contract"] for x in clean]); lines.append("✅ <b>سحب صافٍ</b>")
      for i, x in enumerate(clean, 1): lines += [line(i, x, x["amount"], x["count"], prices), ""]
  else: lines.append("✨ لا توجد توكنات نقية تطابق الفلتر.")
  if tainted:
      lines += [SEP2, "⚠️ <b>إيداعات مقابلة — ضغط بيع محتمل</b>"]; prices = get_usd_prices([x["contract"] for x in tainted])
      for i, x in enumerate(tainted, 1): lines.append("%s. <code>%s</code>  %s  ↑%s ↓%s (%d%%)  %s %s" % (i, esc(x["symbol"]), money(prices.get(x["contract"].lower(), 0) * x["amount"]), qty(x["amount"]), qty(x.get("deposit_amount", 0)), int(x.get("taint_ratio", 0) * 100), dex(x["contract"]), scan(x["contract"])))
  return "\n".join(lines + [SEP]), url
def format_best_opportunities(data):
  tokens = [x for x in data.get("ranked", []) if not skip(x.get("symbol", ""))]; completed = data.get("_meta", {}).get("completed_windows", []); lines = [SEP, "🏆 أفضل الفرص  |  " + " ".join(WIN.get(x, str(x)) for x in completed), SEP]
  if not tokens: return "\n".join(lines + ["لا توجد توكنات متكررة.", SEP]), ""
  prices = get_usd_prices([x["contract"] for x in tokens])
  for i, x in enumerate(tokens[:TOP_N], 1): lines.append("%s  %s  <code>%s</code>  <b>%s</b>\n     %s  %s  •  %s (%d/%d)  %s %s" % ("🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i) + ".", "🔥🔥🔥" if len(x["windows"]) == len(completed) else "🔥🔥", esc(x["symbol"]), esc(x.get("name", "")), qty(x["max_amount"]), money(prices.get(x["contract"].lower(), 0) * x["max_amount"]), " ".join(WIN.get(w, str(w)) for w in x["windows"]), len(x["windows"]), len(completed), dex(x["contract"]), scan(x["contract"])))
  return "\n".join(lines + [SEP]), "https://dexscreener.com/bsc/" + safe(tokens[0]["contract"])
def format_discovery(data, minutes):
  lines = [SEP, "🔭 اكتشاف المحافظ  |  " + period(minutes), SEP]
  def p(x, direction):
      value, count = (x["vol_in"], x["cnt_in"]) if direction == "in" else (x["vol_out"], x["cnt_out"]) if direction == "out" else (x["vol_total"], x["cnt_total"]); a = x["addr"]; return "<code>%s…%s</code>  %s  •  %s تحويل  %s" % (esc(a[:6]), esc(a[-4:]), qty(value), count, addr(a))
  if data.get("top_withdrawers"): lines += ["📤 <b>أكبر المستلمين</b>"] + [p(x, "out") for x in data["top_withdrawers"][:6]]
  if data.get("top_depositors"): lines += [SEP2, "📥 <b>أكبر المودعين</b>"] + [p(x, "in") for x in data["top_depositors"][:6]]
  if data.get("top_bidirectional"): lines += [SEP2, "🔄 <b>تفاعل من الجانبين</b>"] + [p(x, "both") for x in data["top_bidirectional"][:5]]
  return "\n".join(lines + (["لا توجد محافظ متفاعلة."] if len(lines) == 3 else []) + [SEP])
