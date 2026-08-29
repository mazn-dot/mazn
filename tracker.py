import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import ACTIVE_CHAINS, CHAINS, MIN_TRANSFERS, TOP_N

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEX = "https://api.dexscreener.com/latest/dex/tokens/"

log = logging.getLogger(__name__)
_price_cache, _price_lock = {}, threading.Lock()
_meta_cache = {}
_rpc_locks = {c: threading.Lock() for c in CHAINS}
_rpc_times = {c: [] for c in CHAINS}

STABLE_LARGE = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP", "FRAX", "LUSD", "MIM", "HAY",
    "BNB", "WBNB", "ETH", "WETH", "BTC", "BTCB", "WBTC", "XRP", "SOL", "ADA", "DOT",
    "MATIC", "WMATIC", "LINK", "UNI", "CAKE", "AAVE", "AVAX", "ATOM", "ARB", "OP",
}


def rpc(chain_id, method, params):
    cfg = CHAINS.get(chain_id)
    if not cfg:
        return None
    now = time.time()
    lock = _rpc_locks[chain_id]
    with lock:
        times = _rpc_times[chain_id]
        while times and times[0] < now - 60:
            times.pop(0)
        if len(times) >= 40:
            return None
        times.append(now)
    try:
        r = requests.post(
            cfg["rpc"],
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=18,
        )
        return r.json().get("result") if r.status_code == 200 else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def topic_address(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def uint(value):
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return 0


def decode_string(value):
    if not value or value in ("0x", "0x0"):
        return ""
    try:
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    except ValueError:
        return ""
    if len(raw) >= 96:
        try:
            offset = int.from_bytes(raw[:32], "big")
            if offset < len(raw):
                length = int.from_bytes(raw[offset : offset + 32], "big")
                if 0 < length <= 64:
                    data = raw[offset + 32 : offset + 32 + length]
                    text = data.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
                    if text:
                        return text[:32]
        except (IndexError, ValueError):
            pass
    try:
        text = raw[:32].rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
        if text and text.isprintable():
            return text[:32]
    except (UnicodeError, ValueError):
        pass
    return ""


def token_meta(chain_id, contract):
    key = "%s:%s" % (chain_id, contract.lower())
    if key in _meta_cache:
        return _meta_cache[key]

    decimals = min(
        uint(rpc(chain_id, "eth_call", [{"to": contract.lower(), "data": "0x313ce567"}, "latest"]) or "0x12"),
        36,
    )
    symbol = decode_string(
        rpc(chain_id, "eth_call", [{"to": contract.lower(), "data": "0x95d89b41"}, "latest"]) or ""
    )
    name = decode_string(
        rpc(chain_id, "eth_call", [{"to": contract.lower(), "data": "0x06fdde03"}, "latest"]) or ""
    )

    if not symbol or symbol in ("???", ""):
        try:
            resp = requests.get(DEX + contract.lower(), timeout=12)
            pairs = (resp.json().get("pairs") or []) if resp.status_code == 200 else []
            dex_slug = CHAINS[chain_id]["dex"]
            preferred = [p for p in pairs if (p.get("chainId") or "").lower() == dex_slug]
            pick = preferred[0] if preferred else (pairs[0] if pairs else None)
            if pick:
                base = pick.get("baseToken") or {}
                symbol = (base.get("symbol") or symbol or "???").strip()[:32]
                name = (base.get("name") or name or symbol).strip()[:48]
        except (requests.RequestException, ValueError, TypeError, KeyError):
            pass

    if not symbol:
        symbol = "???"
    if not name:
        name = symbol

    meta = {"decimals": decimals, "symbol": symbol, "name": name, "chain": chain_id}
    _meta_cache[key] = meta
    return meta


def transfer_logs(chain_id, address, direction, begin, end):
    topic = topic_address(address)
    topics = [TRANSFER_TOPIC, topic] if direction == "out" else [TRANSFER_TOPIC, None, topic]
    result = rpc(
        chain_id,
        "eth_getLogs",
        [{"fromBlock": hex(begin), "toBlock": hex(end), "topics": topics}],
    )
    if result is not None or end - begin <= 5:
        return result or []
    midpoint = (begin + end) // 2
    return transfer_logs(chain_id, address, direction, begin, midpoint) + transfer_logs(
        chain_id, address, direction, midpoint + 1, end
    )


def transfers_on_chain(chain_id, address, direction, minutes, cap=1200):
    cfg = CHAINS[chain_id]
    latest_hex = rpc(chain_id, "eth_blockNumber", [])
    if not latest_hex:
        return {}
    latest = uint(latest_hex)
    bpm = cfg.get("blocks_per_min", 15)
    start = max(0, latest - min(max(1, int(minutes * bpm)), 30000))
    raw = defaultdict(lambda: {"raw_amount": 0, "count": 0})
    processed = 0
    for end in range(latest, start - 1, -1500):
        begin = max(start, end - 1499)
        for event in transfer_logs(chain_id, address, direction, begin, end):
            contract = (event.get("address") or "").lower()
            if not contract:
                continue
            raw[contract]["raw_amount"] += uint(event.get("data", "0x"))
            raw[contract]["count"] += 1
            processed += 1
            if processed >= cap:
                break
        if begin == start or processed >= cap:
            break

    out = {}
    ranked = sorted(raw.items(), key=lambda p: p[1]["raw_amount"], reverse=True)[: TOP_N * 2]
    for contract, item in ranked:
        meta = token_meta(chain_id, contract)
        decimals = meta["decimals"]
        amount = item["raw_amount"] / (10 ** decimals if decimals else 1)
        count = item["count"]
        score = amount * (1.0 + (count ** 0.6) / 8.0)
        out[contract] = {
            "amount": amount,
            "count": count,
            "score": score,
            "symbol": meta["symbol"],
            "name": meta["name"],
            "chain": chain_id,
            "contract": contract,
        }
    return out


def transfers_multi(address, direction, minutes, chains=None):
    chains = chains or ACTIVE_CHAINS
    merged = {}
    with ThreadPoolExecutor(max_workers=min(5, len(chains))) as pool:
        futures = {
            pool.submit(transfers_on_chain, cid, address, direction, minutes): cid for cid in chains
        }
        for fut in as_completed(futures):
            cid = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                log.warning("chain %s failed: %s", cid, e)
                continue
            for contract, info in data.items():
                key = "%s:%s" % (cid, contract)
                merged[key] = info
    return merged


def build_reason(item):
    amount = item.get("amount", 0)
    count = item.get("count", 0)
    taint = item.get("taint_ratio")
    windows = item.get("windows")
    parts = []

    if amount >= 1e6:
        parts.append("كمية ضخمة")
    elif amount >= 1e5:
        parts.append("كمية كبيرة")
    elif amount >= 1e4:
        parts.append("كمية ملحوظة")
    else:
        parts.append("نشاط")

    if count >= 40:
        parts.append("%d تحويل منسّق" % count)
    elif count >= 20:
        parts.append("%d تحويل نشط" % count)
    elif count >= MIN_TRANSFERS:
        parts.append("%d تحويل" % count)

    if taint is not None:
        if taint <= 0.1:
            parts.append("سحب صافٍ")
        elif taint > 0.25:
            parts.append("إيداع مقابل %d%%" % int(taint * 100))

    if windows:
        parts.append("ظهر في %d نوافذ" % len(windows))

    chain = item.get("chain")
    if chain and chain in CHAINS:
        parts.append(CHAINS[chain]["name"])

    return " · ".join(parts) if parts else "نشاط ملحوظ"


def top_from_raw(raw, limit=TOP_N):
    items = sorted(raw.values(), key=lambda x: x.get("score", x.get("amount", 0)), reverse=True)
    result = []
    seen_sym = set()
    for info in items:
        if info.get("count", 0) < MIN_TRANSFERS:
            continue
        sym = (info.get("symbol") or "???").upper()
        if sym in STABLE_LARGE:
            continue
        key = "%s:%s" % (info.get("chain", ""), sym)
        if key in seen_sym:
            continue
        seen_sym.add(key)
        info = dict(info)
        info["reason"] = build_reason(info)
        result.append(info)
        if len(result) >= limit:
            break
    return result


def get_report(minutes, direction, wallets=None, chains=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    chains = chains or ACTIVE_CHAINS
    out = {"_meta": {"minutes": minutes, "source": "multi_rpc", "chains": chains}}
    combined = {}
    for label, address in wallets.items():
        raw = transfers_multi(address, direction, minutes, chains)
        for key, info in raw.items():
            ckey = "%s:%s" % (info["chain"], info["contract"])
            if ckey not in combined:
                combined[ckey] = dict(info)
            else:
                combined[ckey]["amount"] += info["amount"]
                combined[ckey]["count"] += info["count"]
                combined[ckey]["score"] += info.get("score", info["amount"])
        out[label] = {"tokens": top_from_raw(raw), "source": "multi_rpc"}
    out["_combined"] = top_from_raw(combined)
    return out


def merge_multi(raws):
    out = {}
    for raw in raws.values():
        for key, info in raw.items():
            ckey = "%s:%s" % (info.get("chain", ""), info.get("contract", key))
            x = out.setdefault(
                ckey,
                {
                    "contract": info.get("contract", ""),
                    "symbol": info.get("symbol", "???"),
                    "name": info.get("name", ""),
                    "chain": info.get("chain", ""),
                    "amount": 0.0,
                    "count": 0,
                    "score": 0.0,
                },
            )
            x["amount"] += info["amount"]
            x["count"] += info["count"]
            x["score"] += info.get("score", info["amount"])
    return out


def get_opportunity(minutes, wallets=None, chains=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    chains = chains or ACTIVE_CHAINS
    raws = {
        label: transfers_multi(address, "out", minutes, chains) for label, address in wallets.items()
    }
    merged = merge_multi(raws)
    ranked = top_from_raw(merged)
    return {
        "_meta": {"minutes": minutes, "source": "multi_rpc", "chains": chains},
        "ranked": ranked,
        "per_wallet_raw": raws,
    }


def get_clean_opportunity(minutes, wallets=None, chains=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    chains = chains or ACTIVE_CHAINS
    outs = {
        label: transfers_multi(address, "out", minutes, chains) for label, address in wallets.items()
    }
    ins = {
        label: transfers_multi(address, "in", minutes, chains) for label, address in wallets.items()
    }
    incoming = merge_multi(ins)
    clean, tainted = [], []
    for x in merge_multi(outs).values():
        ckey = "%s:%s" % (x.get("chain", ""), x.get("contract", ""))
        dep = incoming.get(ckey, {}).get("amount", 0)
        x["deposit_amount"] = dep
        x["taint_ratio"] = dep / x["amount"] if x["amount"] else 0
        x["reason"] = build_reason(x)
        (tainted if x["taint_ratio"] > 0.25 else clean).append(x)
    clean = [t for t in clean if t.get("count", 0) >= MIN_TRANSFERS and (t.get("symbol") or "").upper() not in STABLE_LARGE]
    tainted = [t for t in tainted if t.get("count", 0) >= MIN_TRANSFERS and (t.get("symbol") or "").upper() not in STABLE_LARGE]
    clean.sort(key=lambda x: x.get("score", x["amount"]), reverse=True)
    tainted.sort(key=lambda x: x.get("score", x["amount"]), reverse=True)
    for t in clean[:TOP_N]:
        t["reason"] = build_reason(t)
    return {
        "_meta": {"minutes": minutes, "source": "multi_rpc", "chains": chains},
        "clean": clean[:TOP_N],
        "tainted": tainted[:5],
    }


BEST_WINDOWS = [5, 15, 30, 60]


def get_best_opportunities(wallets=None, chains=None):
    chains = chains or ACTIVE_CHAINS
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(get_opportunity, m, wallets, chains): m for m in BEST_WINDOWS}
        results = {jobs[j]: j.result() for j in as_completed(jobs)}
    agg = {}
    for minutes, result in results.items():
        for item in result.get("ranked", []):
            ckey = "%s:%s" % (item.get("chain", ""), item.get("contract", ""))
            x = agg.setdefault(
                ckey,
                {
                    "contract": item.get("contract", ""),
                    "symbol": item.get("symbol", "???"),
                    "name": item.get("name", ""),
                    "chain": item.get("chain", ""),
                    "windows": [],
                    "rank_sum": 0,
                    "max_amount": 0,
                    "max_score": 0,
                    "max_count": 0,
                },
            )
            x["windows"].append(minutes)
            ranked_list = result["ranked"]
            try:
                x["rank_sum"] += ranked_list.index(item) + 1
            except ValueError:
                x["rank_sum"] += 10
            x["max_amount"] = max(x["max_amount"], item.get("amount", 0))
            x["max_score"] = max(x["max_score"], item.get("score", item.get("amount", 0)))
            x["max_count"] = max(x["max_count"], item.get("count", 0))
    for x in agg.values():
        x["amount"] = x["max_amount"]
        x["count"] = x["max_count"]
        x["score"] = x["max_score"]
        x["reason"] = build_reason(x)
    ranked = sorted(
        [x for x in agg.values() if (x.get("symbol") or "").upper() not in STABLE_LARGE],
        key=lambda x: (len(x["windows"]), -x["rank_sum"] / max(len(x["windows"]), 1), x["max_score"]),
        reverse=True,
    )
    return {
        "_meta": {
            "windows": BEST_WINDOWS,
            "completed_windows": sorted(results),
            "source": "multi_rpc",
            "chains": chains,
        },
        "ranked": ranked[:TOP_N],
    }


def get_top_counterparties(minutes, wallets=None, chains=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    chain_id = "bsc"
    if chains and "bsc" not in chains:
        chain_id = chains[0]
    own = {x.lower() for x in wallets.values()}
    peers = defaultdict(
        lambda: {
            "vol_in": 0, "cnt_in": 0, "vol_out": 0, "cnt_out": 0,
            "tokens_in": set(), "tokens_out": set(),
        }
    )
    cfg = CHAINS[chain_id]
    for address in wallets.values():
        latest_hex = rpc(chain_id, "eth_blockNumber", [])
        if not latest_hex:
            continue
        latest = uint(latest_hex)
        start = max(0, latest - min(max(1, int(minutes * cfg["blocks_per_min"])), 30000))
        for end in range(latest, start - 1, -1500):
            begin = max(start, end - 1499)
            for direction in ("in", "out"):
                for event in transfer_logs(chain_id, address, direction, begin, end):
                    raw_topics = event.get("topics") or []
                    if len(raw_topics) < 3:
                        continue
                    sender = "0x" + raw_topics[1][-40:]
                    receiver = "0x" + raw_topics[2][-40:]
                    peer = sender if direction == "in" else receiver
                    if peer.lower() in own:
                        continue
                    contract = (event.get("address") or "").lower()
                    meta = token_meta(chain_id, contract)
                    amount = uint(event.get("data", "0x")) / (
                        10 ** meta["decimals"] if meta["decimals"] else 1
                    )
                    key = "vol_in" if direction == "in" else "vol_out"
                    count = "cnt_in" if direction == "in" else "cnt_out"
                    tokens = "tokens_in" if direction == "in" else "tokens_out"
                    peers[peer][key] += amount
                    peers[peer][count] += 1
                    peers[peer][tokens].add(meta["symbol"])
            if begin == start:
                break

    def entry(addr, x):
        return {
            "addr": addr, **x,
            "vol_total": x["vol_in"] + x["vol_out"],
            "cnt_total": x["cnt_in"] + x["cnt_out"],
        }

    all_peers = [entry(a, x) for a, x in peers.items()]
    return {
        "_meta": {"minutes": minutes, "source": "multi_rpc", "chain": chain_id},
        "top_depositors": sorted(all_peers, key=lambda x: x["vol_in"], reverse=True)[:8],
        "top_withdrawers": sorted(all_peers, key=lambda x: x["vol_out"], reverse=True)[:8],
        "top_bidirectional": sorted(
            [x for x in all_peers if x["cnt_in"] and x["cnt_out"]],
            key=lambda x: x["vol_total"],
            reverse=True,
        )[:8],
    }


def find_whales_for_token(token_address, chain_id="bsc", minutes=60, min_count=3, limit=15):
    token = token_address.lower().strip()
    if not token.startswith("0x") or len(token) != 42:
        return {"error": "عنوان توكن غير صحيح", "whales": []}
    if chain_id not in CHAINS:
        chain_id = "bsc"

    latest_hex = rpc(chain_id, "eth_blockNumber", [])
    if not latest_hex:
        return {"error": "RPC غير متاح على " + CHAINS[chain_id]["name"], "whales": []}

    cfg = CHAINS[chain_id]
    latest = uint(latest_hex)
    start = max(0, latest - min(max(1, int(minutes * cfg["blocks_per_min"])), 30000))
    meta = token_meta(chain_id, token)
    decimals = meta["decimals"] or 18
    wallets_data = defaultdict(
        lambda: {"in_amount": 0.0, "out_amount": 0.0, "in_count": 0, "out_count": 0}
    )

    for end in range(latest, start - 1, -1500):
        begin = max(start, end - 1499)
        result = rpc(
            chain_id,
            "eth_getLogs",
            [{"fromBlock": hex(begin), "toBlock": hex(end), "address": token, "topics": [TRANSFER_TOPIC]}],
        )
        if result is None and end - begin > 5:
            mid = (begin + end) // 2
            part1 = rpc(
                chain_id, "eth_getLogs",
                [{"fromBlock": hex(begin), "toBlock": hex(mid), "address": token, "topics": [TRANSFER_TOPIC]}],
            ) or []
            part2 = rpc(
                chain_id, "eth_getLogs",
                [{"fromBlock": hex(mid + 1), "toBlock": hex(end), "address": token, "topics": [TRANSFER_TOPIC]}],
            ) or []
            result = part1 + part2
        for event in result or []:
            topics = event.get("topics") or []
            if len(topics) < 3:
                continue
            sender = "0x" + topics[1][-40:]
            receiver = "0x" + topics[2][-40:]
            amount = uint(event.get("data", "0x")) / (10 ** decimals)
            if amount <= 0:
                continue
            wallets_data[sender]["out_amount"] += amount
            wallets_data[sender]["out_count"] += 1
            wallets_data[receiver]["in_amount"] += amount
            wallets_data[receiver]["in_count"] += 1
        if begin == start:
            break

    candidates = []
    for addr, d in wallets_data.items():
        total = d["in_amount"] + d["out_amount"]
        cnt = d["in_count"] + d["out_count"]
        if cnt < min_count:
            continue
        score = total * (1.0 + (cnt ** 0.5) / 5.0)
        candidates.append({
            "addr": addr,
            "in_amount": d["in_amount"],
            "out_amount": d["out_amount"],
            "in_count": d["in_count"],
            "out_count": d["out_count"],
            "total": total,
            "count": cnt,
            "score": score,
            "reason": build_reason({"amount": total, "count": cnt, "chain": chain_id}),
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {
        "token": token,
        "symbol": meta["symbol"],
        "name": meta["name"],
        "chain": chain_id,
        "minutes": minutes,
        "whales": candidates[:limit],
    }


def get_usd_prices(items):
    answer = {}
    for item in items:
        if isinstance(item, dict):
            contract = item.get("contract", "")
            chain = item.get("chain", "bsc")
        else:
            contract, chain = str(item), "bsc"
        key = contract.lower()
        cache_key = "%s:%s" % (chain, key)
        with _price_lock:
            cached = _price_cache.get(cache_key)
        if cached and time.time() - cached["time"] < 90:
            answer[key] = cached["price"]
            continue
        try:
            response = requests.get(DEX + key, timeout=15)
            pairs = (response.json().get("pairs") or []) if response.status_code == 200 else []
            dex_slug = CHAINS.get(chain, {}).get("dex", "bsc")
            preferred = [p for p in pairs if (p.get("chainId") or "").lower() == dex_slug]
            pick = preferred[0] if preferred else (pairs[0] if pairs else None)
            price = float(pick.get("priceUsd") or 0) if pick else 0
        except (requests.RequestException, ValueError, TypeError):
            price = 0
        answer[key] = price
        with _price_lock:
            _price_cache[cache_key] = {"price": price, "time": time.time()}
    return answer
