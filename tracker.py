import logging, threading, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from config import TOP_N
RPC = "https://bsc-dataseed.binance.org"
DEX = "https://api.dexscreener.com/latest/dex/tokens/"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55aebf5f7f"
CHAIN = "bsc"
log = logging.getLogger(__name__)
_price_cache, _price_lock = {}, threading.Lock()
_meta_cache, _rpc_lock, _rpc_times = {}, threading.Lock(), []


def rpc(method, params):
  now = time.time()
  with _rpc_lock:
      while _rpc_times and _rpc_times[0] < now - 60: _rpc_times.pop(0)
      if len(_rpc_times) >= 45: return None
      _rpc_times.append(now)
  try:
      r = requests.post(RPC, json={"jsonrpc":"2.0", "id":1, "method":method, "params":params}, timeout=20)
      return r.json().get("result") if r.status_code == 200 else None
  except (requests.RequestException, ValueError): return None


def topic_address(address): return "0x" + address.lower().replace("0x", "").rjust(64, "0")
def uint(value):
  try: return int(value, 16)
  except (TypeError, ValueError): return 0

def text(value):
  try:
      raw = bytes.fromhex((value or "0x")[2:])
      if len(raw) >= 96:
          offset = int.from_bytes(raw[:32], "big"); length = int.from_bytes(raw[offset:offset+32], "big"); raw = raw[offset+32:offset+32+length]
      return raw.rstrip(b"\\x00").decode("utf-8", errors="ignore")[:32]
  except (ValueError, UnicodeError, IndexError): return ""

def token_meta(contract):
  key = contract.lower()
  if key in _meta_cache: return _meta_cache[key]
  decimals = min(uint(rpc("eth_call", [{"to":key,"data":"0x313ce567"},"latest"]) or "0x12"), 36)
  symbol = text(rpc("eth_call", [{"to":key,"data":"0x95d89b41"},"latest"]) or "") or "???"
  name = text(rpc("eth_call", [{"to":key,"data":"0x06fdde03"},"latest"]) or "") or symbol
  _meta_cache[key] = {"decimals":decimals,"symbol":symbol,"name":name}; return _meta_cache[key]

def transfers(address, direction, minutes, cap=2000):
  latest_hex = rpc("eth_blockNumber", [])
  if not latest_hex: return {}
  latest = uint(latest_hex); start = max(0, latest - min(max(1, int(minutes * 20)), 40000)); wallet = address.lower()
  out = defaultdict(lambda: {"amount":0.0,"count":0,"symbol":"???","name":""}); processed = 0
  for end in range(latest, start - 1, -2000):
      begin = max(start, end - 1999); topic = topic_address(address)
      topics = [TRANSFER_TOPIC, topic if direction == "out" else None, topic if direction == "in" else None]
      logs = rpc("eth_getLogs", [{"fromBlock":hex(begin),"toBlock":hex(end),"topics":topics}]) or []
      for event in logs:
          contract = (event.get("address") or "").lower()
          if not contract: continue
          meta = token_meta(contract); decimals = meta["decimals"]
          item = out[contract]; item["amount"] += uint(event.get("data","0x")) / (10 ** decimals if decimals else 1); item["count"] += 1; item["symbol"], item["name"] = meta["symbol"], meta["name"]; processed += 1
          if processed >= cap: return dict(out)
      if begin == start: break
  return dict(out)

def top(raw, limit=TOP_N):
  result = {}
  for contract, info in sorted(raw.items(), key=lambda x: x[1]["amount"], reverse=True)[:limit]:
      symbol = info.get("symbol") or "???"; key = symbol if symbol not in result else symbol + "(" + contract[:6] + ")"; result[key] = {**info, "contract": contract, "symbol": symbol}
  return result

def get_report(minutes, direction, wallets=None):
  if wallets is None:
      import wallets as wm; wallets = wm.get_all()
  out = {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}}
  for label, address in wallets.items():
      raw = transfers(address, direction, minutes); out[label] = {"tokens": top(raw), "source": "public_bsc_rpc" if raw else "none"}
  return out

def merge(raws):
  out = {}
  for raw in raws.values():
      for contract, info in raw.items():
          x = out.setdefault(contract, {"contract": contract, "symbol": info.get("symbol", "???"), "name": info.get("name", ""), "amount": 0.0, "count": 0}); x["amount"] += info["amount"]; x["count"] += info["count"]
  return out

def get_opportunity(minutes, wallets=None):
  if wallets is None:
      import wallets as wm; wallets = wm.get_all()
  raws = {label: transfers(address, "out", minutes) for label, address in wallets.items()}; ranked = sorted(merge(raws).values(), key=lambda x: x["amount"], reverse=True)
  return {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}, "ranked": ranked[:TOP_N], "per_wallet_raw": raws}

def get_clean_opportunity(minutes, wallets=None):
  if wallets is None:
      import wallets as wm; wallets = wm.get_all()
  outs = {label: transfers(address, "out", minutes) for label, address in wallets.items()}; ins = {label: transfers(address, "in", minutes) for label, address in wallets.items()}; incoming = merge(ins); clean, tainted = [], []
  for x in merge(outs).values():
      x["deposit_amount"] = incoming.get(x["contract"], {}).get("amount", 0); x["taint_ratio"] = x["deposit_amount"] / x["amount"] if x["amount"] else 0
      (tainted if x["taint_ratio"] > .25 else clean).append(x)
  clean.sort(key=lambda x: x["amount"], reverse=True); tainted.sort(key=lambda x: x["amount"], reverse=True)
  return {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}, "clean": clean[:TOP_N], "tainted": tainted[:5]}

BEST_WINDOWS = [5, 15, 30, 60]
def get_best_opportunities(wallets=None):
  with ThreadPoolExecutor(max_workers=4) as pool:
      jobs = {pool.submit(get_opportunity, m, wallets): m for m in BEST_WINDOWS}; results = {jobs[j]: j.result() for j in as_completed(jobs)}
  agg = {}
  for minutes, result in results.items():
      for item in result["ranked"]:
          x = agg.setdefault(item["contract"], {"contract": item["contract"], "symbol": item["symbol"], "name": item.get("name", ""), "windows": [], "rank_sum": 0, "max_amount": 0})
          x["windows"].append(minutes); x["rank_sum"] += result["ranked"].index(item) + 1; x["max_amount"] = max(x["max_amount"], item["amount"])
  ranked = sorted(agg.values(), key=lambda x: (len(x["windows"]), -x["rank_sum"] / len(x["windows"]), x["max_amount"]), reverse=True)
  return {"_meta": {"windows": BEST_WINDOWS, "completed_windows": sorted(results), "source": "public_bsc_rpc"}, "ranked": ranked[:TOP_N]}

def get_top_counterparties(minutes, wallets=None):
  if wallets is None:
      import wallets as wm; wallets = wm.get_all()
  own = {x.lower() for x in wallets.values()}; peers = defaultdict(lambda: {"vol_in": 0, "cnt_in": 0, "vol_out": 0, "cnt_out": 0, "tokens_in": set(), "tokens_out": set()})
  for address in wallets.values():
      latest_hex = rpc("eth_blockNumber", [])
      if not latest_hex: continue
      latest = uint(latest_hex); start = max(0, latest - min(max(1, int(minutes * 20)), 40000))
      for end in range(latest, start - 1, -2000):
          begin = max(start, end - 1999); topic = topic_address(address)
          for direction in ("in", "out"):
              topics = [TRANSFER_TOPIC, topic if direction == "out" else None, topic if direction == "in" else None]
              for event in rpc("eth_getLogs", [{"fromBlock":hex(begin),"toBlock":hex(end),"topics":topics}]) or []:
                  raw_topics = event.get("topics") or []
                  if len(raw_topics) < 3: continue
                  sender, receiver = "0x" + raw_topics[1][-40:], "0x" + raw_topics[2][-40:]
                  peer = sender if direction == "in" else receiver
                  if peer.lower() in own: continue
                  contract = (event.get("address") or "").lower(); meta = token_meta(contract); amount = uint(event.get("data", "0x")) / (10 ** meta["decimals"] if meta["decimals"] else 1)
                  key = "vol_in" if direction == "in" else "vol_out"; count = "cnt_in" if direction == "in" else "cnt_out"; tokens = "tokens_in" if direction == "in" else "tokens_out"
                  peers[peer][key] += amount; peers[peer][count] += 1; peers[peer][tokens].add(meta["symbol"])
          if begin == start: break
  def entry(addr, x): return {"addr": addr, **x, "vol_total": x["vol_in"] + x["vol_out"], "cnt_total": x["cnt_in"] + x["cnt_out"]}
  all_peers = [entry(a, x) for a, x in peers.items()]
  return {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}, "top_depositors": sorted(all_peers, key=lambda x: x["vol_in"], reverse=True)[:8], "top_withdrawers": sorted(all_peers, key=lambda x: x["vol_out"], reverse=True)[:8], "top_bidirectional": sorted([x for x in all_peers if x["cnt_in"] and x["cnt_out"]], key=lambda x: x["vol_total"], reverse=True)[:8]}

def get_usd_prices(contracts):
  answer = {}
  for contract in contracts:
      key = contract.lower()
      with _price_lock:
          cached = _price_cache.get(key)
      if cached and time.time() - cached["time"] < 90:
          answer[key] = cached["price"]; continue
      try:
          response = requests.get(DEX + key, timeout=20)
          pairs = (response.json().get("pairs") or []) if response.status_code == 200 else []
          price = float(pairs[0].get("priceUsd") or 0) if pairs else 0
      except (requests.RequestException, ValueError, TypeError): price = 0
      answer[key] = price
      with _price_lock: _price_cache[key] = {"price": price, "time": time.time()}
  return answer
