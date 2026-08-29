import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import TOP_N

RPC = "https://rpc-bnb.blockmachine.io"
DEX = "https://api.dexscreener.com/latest/dex/tokens/"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHAIN = "bsc"

log = logging.getLogger(__name__)
_price_cache, _price_lock = {}, threading.Lock()
_meta_cache, _rpc_lock, _rpc_times = {}, threading.Lock(), []


def rpc(method, params):
    now = time.time()
    with _rpc_lock:
        while _rpc_times and _rpc_times[0] < now - 60:
            _rpc_times.pop(0)
        if len(_rpc_times) >= 45:
            return None
        _rpc_times.append(now)
    try:
        r = requests.post(
            RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=20,
        )
        return r.json().get("result") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def topic_address(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def uint(value):
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return 0


def decode_string(value):
    """Decode both dynamic string and bytes32 return values from eth_call."""
    if not value or value in ("0x", "0x0"):
        return ""
    try:
        raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    except ValueError:
        return ""

    # Dynamic string (offset + length + data)
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

    # bytes32 style (fixed 32 bytes, null-padded)
    try:
        text = raw[:32].rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
        if text and text.isprintable():
            return text[:32]
    except (UnicodeError, ValueError):
        pass
    return ""


def token_meta(contract):
    """Fetch symbol / name / decimals with better decoding + Dexscreener fallback."""
    key = contract.lower()
    if key in _meta_cache:
        return _meta_cache[key]

    decimals = min(uint(rpc("eth_call", [{"to": key, "data": "0x313ce567"}, "latest"]) or "0x12"), 36)
    symbol = decode_string(rpc("eth_call", [{"to": key, "data": "0x95d89b41"}, "latest"]) or "")
    name = decode_string(rpc("eth_call", [{"to": key, "data": "0x06fdde03"}, "latest"]) or "")

    # Fallback to Dexscreener when RPC returns empty / ???
    if not symbol or symbol in ("???", ""):
        try:
            resp = requests.get(DEX + key, timeout=12)
            pairs = (resp.json().get("pairs") or []) if resp.status_code == 200 else []
            if pairs:
                base = pairs[0].get("baseToken") or {}
                symbol = (base.get("symbol") or symbol or "???").strip()[:32]
                name = (base.get("name") or name or symbol).strip()[:48]
        except (requests.RequestException, ValueError, TypeError, KeyError):
            pass

    if not symbol:
        symbol = "???"
    if not name:
        name = symbol

    meta = {"decimals": decimals, "symbol": symbol, "name": name}
    _meta_cache[key] = meta
    return meta


def transfer_logs(address, direction, begin, end):
    topic = topic_address(address)
    topics = [TRANSFER_TOPIC, topic] if direction == "out" else [TRANSFER_TOPIC, [], topic]
    result = rpc("eth_getLogs", [{"fromBlock": hex(begin), "toBlock": hex(end), "topics": topics}])
    if result is not None or end - begin <= 5:
        return result or []
    midpoint = (begin + end) // 2
    return transfer_logs(address, direction, begin, midpoint) + transfer_logs(
        address, direction, midpoint + 1, end
    )


def transfers(address, direction, minutes, cap=2000):
    latest_hex = rpc("eth_blockNumber", [])
    if not latest_hex:
        return {}
    latest = uint(latest_hex)
    start = max(0, latest - min(max(1, int(minutes * 20)), 40000))
    raw = defaultdict(lambda: {"raw_amount": 0, "count": 0})
    processed = 0
    for end in range(latest, start - 1, -2000):
        begin = max(start, end - 1999)
        for event in transfer_logs(address, direction, begin, end):
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

    ranked = sorted(raw.items(), key=lambda pair: pair[1]["raw_amount"], reverse=True)[: TOP_N * 2]
    out = {}
    for contract, item in ranked:
        meta = token_meta(contract)
        decimals = meta["decimals"]
        amount = item["raw_amount"] / (10 ** decimals if decimals else 1)
        count = item["count"]
        # Score: combines size + activity intensity (helps spot coordinated dumps/pumps)
        score = amount * (1.0 + (count ** 0.6) / 8.0)
        out[contract] = {
            "amount": amount,
            "count": count,
            "score": score,
            "symbol": meta["symbol"],
            "name": meta["name"],
        }
    return out


def top(raw, limit=TOP_N):
    result = {}
    for contract, info in sorted(raw.items(), key=lambda x: x[1].get("score", x[1]["amount"]), reverse=True)[:limit]:
        symbol = info.get("symbol") or "???"
        key = symbol if symbol not in result else symbol + "(" + contract[:6] + ")"
        result[key] = {**info, "contract": contract, "symbol": symbol}
    return result


def get_report(minutes, direction, wallets=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    out = {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}}
    for label, address in wallets.items():
        raw = transfers(address, direction, minutes)
        out[label] = {"tokens": top(raw), "source": "public_bsc_rpc" if raw else "none"}
    return out


def merge(raws):
    out = {}
    for raw in raws.values():
        for contract, info in raw.items():
            x = out.setdefault(
                contract,
                {
                    "contract": contract,
                    "symbol": info.get("symbol", "???"),
                    "name": info.get("name", ""),
                    "amount": 0.0,
                    "count": 0,
                    "score": 0.0,
                },
            )
            x["amount"] += info["amount"]
            x["count"] += info["count"]
            x["score"] += info.get("score", info["amount"])
    return out


def get_opportunity(minutes, wallets=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    raws = {label: transfers(address, "out", minutes) for label, address in wallets.items()}
    ranked = sorted(merge(raws).values(), key=lambda x: x.get("score", x["amount"]), reverse=True)
    return {"_meta": {"minutes": minutes, "source": "public_bsc_rpc"}, "ranked": ranked[:TOP_N], "per_wallet_raw": raws}


def get_clean_opportunity(minutes, wallets=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    outs = {label: transfers(address, "out", minutes) for label, address in wallets.items()}
    ins = {label: transfers(address, "in", minutes) for label, address in wallets.items()}
    incoming = merge(ins)
    clean, tainted = [], []
    for x in merge(outs).values():
        x["deposit_amount"] = incoming.get(x["contract"], {}).get("amount", 0)
        x["taint_ratio"] = x["deposit_amount"] / x["amount"] if x["amount"] else 0
        (tainted if x["taint_ratio"] > 0.25 else clean).append(x)
    clean.sort(key=lambda x: x.get("score", x["amount"]), reverse=True)
    tainted.sort(key=lambda x: x.get("score", x["amount"]), reverse=True)
    return {
        "_meta": {"minutes": minutes, "source": "public_bsc_rpc"},
        "clean": clean[:TOP_N],
        "tainted": tainted[:5],
    }


BEST_WINDOWS = [5, 15, 30, 60]


def get_best_opportunities(wallets=None):
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(get_opportunity, m, wallets): m for m in BEST_WINDOWS}
        results = {jobs[j]: j.result() for j in as_completed(jobs)}
    agg = {}
    for minutes, result in results.items():
        for item in result["ranked"]:
            x = agg.setdefault(
                item["contract"],
                {
                    "contract": item["contract"],
                    "symbol": item["symbol"],
                    "name": item.get("name", ""),
                    "windows": [],
                    "rank_sum": 0,
                    "max_amount": 0,
                    "max_score": 0,
                    "max_count": 0,
                },
            )
            x["windows"].append(minutes)
            x["rank_sum"] += result["ranked"].index(item) + 1
            x["max_amount"] = max(x["max_amount"], item["amount"])
            x["max_score"] = max(x["max_score"], item.get("score", item["amount"]))
            x["max_count"] = max(x["max_count"], item.get("count", 0))
    ranked = sorted(
        agg.values(),
        key=lambda x: (len(x["windows"]), -x["rank_sum"] / len(x["windows"]), x["max_score"]),
        reverse=True,
    )
    return {
        "_meta": {
            "windows": BEST_WINDOWS,
            "completed_windows": sorted(results),
            "source": "public_bsc_rpc",
        },
        "ranked": ranked[:TOP_N],
    }


def get_top_counterparties(minutes, wallets=None):
    if wallets is None:
        import wallets as wm
        wallets = wm.get_all()
    own = {x.lower() for x in wallets.values()}
    peers = defaultdict(
        lambda: {
            "vol_in": 0,
            "cnt_in": 0,
            "vol_out": 0,
            "cnt_out": 0,
            "tokens_in": set(),
            "tokens_out": set(),
        }
    )
    for address in wallets.values():
        latest_hex = rpc("eth_blockNumber", [])
        if not latest_hex:
            continue
        latest = uint(latest_hex)
        start = max(0, latest - min(max(1, int(minutes * 20)), 40000))
        for end in range(latest, start - 1, -2000):
            begin = max(start, end - 1999)
            for direction in ("in", "out"):
                for event in transfer_logs(address, direction, begin, end):
                    raw_topics = event.get("topics") or []
                    if len(raw_topics) < 3:
                        continue
                    sender = "0x" + raw_topics[1][-40:]
                    receiver = "0x" + raw_topics[2][-40:]
                    peer = sender if direction == "in" else receiver
                    if peer.lower() in own:
                        continue
                    contract = (event.get("address") or "").lower()
                    meta = token_meta(contract)
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
            "addr": addr,
            **x,
            "vol_total": x["vol_in"] + x["vol_out"],
            "cnt_total": x["cnt_in"] + x["cnt_out"],
        }

    all_peers = [entry(a, x) for a, x in peers.items()]
    return {
        "_meta": {"minutes": minutes, "source": "public_bsc_rpc"},
        "top_depositors": sorted(all_peers, key=lambda x: x["vol_in"], reverse=True)[:8],
        "top_withdrawers": sorted(all_peers, key=lambda x: x["vol_out"], reverse=True)[:8],
        "top_bidirectional": sorted(
            [x for x in all_peers if x["cnt_in"] and x["cnt_out"]],
            key=lambda x: x["vol_total"],
            reverse=True,
        )[:8],
    }


def find_whales_for_token(token_address, minutes=60, min_count=3, limit=15):
    """
    Discover wallets that received or sent large amounts of a specific token.
    Uses transfer logs of the token contract itself.
    """
    token = token_address.lower().strip()
    if not token.startswith("0x") or len(token) != 42:
        return {"error": "عنوان توكن غير صحيح", "whales": []}

    latest_hex = rpc("eth_blockNumber", [])
    if not latest_hex:
        return {"error": "RPC غير متاح", "whales": []}

    latest = uint(latest_hex)
    start = max(0, latest - min(max(1, int(minutes * 20)), 40000))
    meta = token_meta(token)
    decimals = meta["decimals"] or 18

    wallets_data = defaultdict(
        lambda: {"in_amount": 0.0, "out_amount": 0.0, "in_count": 0, "out_count": 0}
    )

    for end in range(latest, start - 1, -2000):
        begin = max(start, end - 1999)
        result = rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(begin),
                    "toBlock": hex(end),
                    "address": token,
                    "topics": [TRANSFER_TOPIC],
                }
            ],
        )
        if result is None and end - begin > 5:
            mid = (begin + end) // 2
            part1 = rpc(
                "eth_getLogs",
                [{"fromBlock": hex(begin), "toBlock": hex(mid), "address": token, "topics": [TRANSFER_TOPIC]}],
            ) or []
            part2 = rpc(
                "eth_getLogs",
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
        candidates.append(
            {
                "addr": addr,
                "in_amount": d["in_amount"],
                "out_amount": d["out_amount"],
                "in_count": d["in_count"],
                "out_count": d["out_count"],
                "total": total,
                "count": cnt,
                "score": score,
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {
        "token": token,
        "symbol": meta["symbol"],
        "name": meta["name"],
        "minutes": minutes,
        "whales": candidates[:limit],
    }


def get_usd_prices(contracts):
    answer = {}
    for contract in contracts:
        key = contract.lower()
        with _price_lock:
            cached = _price_cache.get(key)
        if cached and time.time() - cached["time"] < 90:
            answer[key] = cached["price"]
            continue
        try:
            response = requests.get(DEX + key, timeout=20)
            pairs = (response.json().get("pairs") or []) if response.status_code == 200 else []
            price = float(pairs[0].get("priceUsd") or 0) if pairs else 0
        except (requests.RequestException, ValueError, TypeError):
            price = 0
        answer[key] = price
        with _price_lock:
            _price_cache[key] = {"price": price, "time": time.time()}
    return answer
