import logging, threading, time
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests
    from config import MORALIS_API_KEY, TOP_N
    BASE = "https://deep-index.moralis.io/api/v2.2"
    CHAIN = "bsc"
    log = logging.getLogger(__name__)
    _price_cache, _price_lock = {}, threading.Lock()

    def api(path, params):
      if not MORALIS_API_KEY: return None
      try:
          r = requests.get(BASE + path, headers={"X-API-Key": MORALIS_API_KEY, "accept": "application/json"}, params=params, timeout=30)
          return r.json() if r.status_code == 200 else None
      except Exception as exc:
          log.warning("Moralis request failed: %s", exc); return None

    def tx_amount(tx):
      try: return float(tx.get("value_decimal") or 0)
      except (TypeError, ValueError):
          try: return int(tx.get("value") or 0) / (10 ** int(tx.get("token_decimals") or 18))
          except Exception: return 0.0

    def transfers(address, direction, minutes, cap=2000):
      date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60)); wallet = address.lower(); out = defaultdict(lambda: {"amount": 0.0, "count": 0, "symbol": "???", "name": ""}); cursor = None
      for _ in range(20):
          params = {"chain": CHAIN, "limit": 100, "order": "DESC", "from_date": date}
          if cursor: params["cursor"] = cursor
          data = api("/" + address + "/erc20/transfers", params)
          if not data or not data.get("result"): break
          for tx in data["result"]:
              sender, receiver = (tx.get("from_address") or "").lower(), (tx.get("to_address") or "").lower()
              if direction == "in" and receiver != wallet: continue
              if direction == "out" and sender != wallet: continue
              contract = (tx.get("address") or "").lower()
              if not contract: continue
              x = out[contract]; x["amount"] += tx_amount(tx); x["count"] += 1; x["symbol"] = tx.get("token_symbol") or x["symbol"]; x["name"] = tx.get("token_name") or x["symbol"]
          cursor = data.get("cursor")
          if not cursor or sum(x["count"] for x in out.values()) >= cap: break
      return dict(out)

    def top(raw, limit=TOP_N):
      result = {}
      for contract, info in sorted(raw.items(), key=lambda x: x[1]["amount"], reverse=True)[:limit]:
          symbol = info.get("symbol") or "???"; key = symbol if symbol not in result else symbol + "(" + contract[:6] + ")"; result[key] = {**info, "contract": contract, "symbol": symbol}
      return result

    def get_report(minutes, direction, wallets=None):
      if wallets is None:
          import wallets as wm; wallets = wm.get_all()
      out = {"_meta": {"minutes": minutes, "moralis_key": bool(MORALIS_API_KEY)}}
      for label, address in wallets.items():
          raw = transfers(address, direction, minutes); out[label] = {"tokens": top(raw), "source": "moralis" if raw else "none"}
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
      return {"_meta": {"minutes": minutes, "moralis_key": bool(MORALIS_API_KEY)}, "ranked": ranked[:TOP_N], "per_wallet_raw": raws}

    def get_clean_opportunity(minutes, wallets=None):
      if wallets is None:
          import wallets as wm; wallets = wm.get_all()
      outs = {label: transfers(address, "out", minutes) for label, address in wallets.items()}; ins = {label: transfers(address, "in", minutes) for label, address in wallets.items()}; incoming = merge(ins); clean, tainted = [], []
      for x in merge(outs).values():
          x["deposit_amount"] = incoming.get(x["contract"], {}).get("amount", 0); x["taint_ratio"] = x["deposit_amount"] / x["amount"] if x["amount"] else 0
          (tainted if x["taint_ratio"] > .25 else clean).append(x)
      clean.sort(key=lambda x: x["amount"], reverse=True); tainted.sort(key=lambda x: x["amount"], reverse=True)
      return {"_meta": {"minutes": minutes, "moralis_key": bool(MORALIS_API_KEY)}, "clean": clean[:TOP_N], "tainted": tainted[:5]}

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
      return {"_meta": {"windows": BEST_WINDOWS, "completed_windows": sorted(results), "moralis_key": bool(MORALIS_API_KEY)}, "ranked": ranked[:TOP_N]}

    def get_top_counterparties(minutes, wallets=None):
      if wallets is None:
          import wallets as wm; wallets = wm.get_all()
      own = {x.lower() for x in wallets.values()}; peers = defaultdict(lambda: {"vol_in": 0, "cnt_in": 0, "vol_out": 0, "cnt_out": 0, "tokens_in": set(), "tokens_out": set()})
      for address in wallets.values():
          date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60)); cursor = None
          for _ in range(10):
              p = {"chain": CHAIN, "limit": 100, "order": "DESC", "from_date": date}
              if cursor: p["cursor"] = cursor
              data = api("/" + address + "/erc20/transfers", p)
              if not data or not data.get("result"): break
              for tx in data["result"]:
                  sender, receiver = (tx.get("from_address") or "").lower(), (tx.get("to_address") or "").lower(); symbol = tx.get("token_symbol") or "???"
                  if receiver == address.lower() and sender not in own: peers[sender]["vol_in"] += tx_amount(tx); peers[sender]["cnt_in"] += 1; peers[sender]["tokens_in"].add(symbol)
                  elif sender == address.lower() and receiver not in own: peers[receiver]["vol_out"] += tx_amount(tx); peers[receiver]["cnt_out"] += 1; peers[receiver]["tokens_out"].add(symbol)
              cursor = data.get("cursor")
              if not cursor: break
      def entry(addr, x): return {"addr": addr, **x, "vol_total": x["vol_in"] + x["vol_out"], "cnt_total": x["cnt_in"] + x["cnt_out"]}
      all_peers = [entry(a, x) for a, x in peers.items()]
      return {"_meta": {"minutes": minutes, "moralis_key": bool(MORALIS_API_KEY)}, "top_depositors": sorted(all_peers, key=lambda x: x["vol_in"], reverse=True)[:8], "top_withdrawers": sorted(all_peers, key=lambda x: x["vol_out"], reverse=True)[:8], "top_bidirectional": sorted([x for x in all_peers if x["cnt_in"] and x["cnt_out"]], key=lambda x: x["vol_total"], reverse=True)[:8]}

    def get_usd_prices(contracts):
      now, answer, missing = time.time(), {}, []
      with _price_lock:
          for c in contracts:
              k = c.lower()
              if k in _price_cache and now - _price_cache[k]["time"] < 90: answer[k] = _price_cache[k]["price"]
              else: missing.append(k)
      def fetch(c):
          try: return c, float((api("/erc20/" + c + "/price", {"chain": CHAIN}) or {}).get("usdPrice") or 0)
          except Exception: return c, 0
      with ThreadPoolExecutor(max_workers=max(1, min(10, len(missing)))) as pool:
          for c, price in pool.map(fetch, missing): answer[c] = price; _price_cache[c] = {"price": price, "time": time.time()}
      return answer
    