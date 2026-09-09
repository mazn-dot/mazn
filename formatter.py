import html
from config import CHAINS, MIN_TRANSFERS, TIME_PERIODS, TOP_N
from tracker import get_usd_prices

SEP = "━━━━━━━━━━━━━━━━━━"
WIN = {
    5: "5د", 15: "15د", 30: "30د", 60: "1س", 120: "2س",
    240: "4س", 360: "6س", 480: "8س", 720: "12س", 1440: "24س",
}


def esc(x):
    return html.escape(str(x), quote=True)


def safe(a):
    s = str(a).lower()
    s = s[2:] if s.startswith("0x") else s
    return "0x" + "".join(c for c in s if c in "0123456789abcdef")[:40]


def chain_label(cid):
    return CHAINS.get(cid, {}).get("name", cid or "?")


def dex_url(item):
    cid = item.get("chain", "bsc")
    slug = CHAINS.get(cid, {}).get("dex", "bsc")
    return "https://dexscreener.com/%s/%s" % (slug, safe(item.get("contract", "")))


def explorer_token(item):
    cid = item.get("chain", "bsc")
    base = CHAINS.get(cid, {}).get("explorer", "https://bscscan.com")
    return "%s/token/%s" % (base, safe(item.get("contract", "")))


def explorer_addr(address, chain="bsc"):
    base = CHAINS.get(chain, {}).get("explorer", "https://bscscan.com")
    return "%s/address/%s" % (base, safe(address))


def period(m):
    return next((x for x, n in TIME_PERIODS if n == m), str(m) + "د")


def money(v):
    v = float(v or 0)
    if v <= 0:
        return ""
    if v >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if v >= 1e6:
        return "$%.2fM" % (v / 1e6)
    if v >= 1e3:
        return "$%.1fK" % (v / 1e3)
    return "$%.2f" % v


def qty(v):
    v = float(v or 0)
    if v >= 1e9:
        return "%.2fB" % (v / 1e9)
    if v >= 1e6:
        return "%.2fM" % (v / 1e6)
    if v >= 1e3:
        return "%.2fK" % (v / 1e3)
    return "%.2f" % v if v >= 1 else "%.4f" % v


def clean_line(i, item, prices=None):
    """Minimal: token + reason only (no noise)."""
    symbol = item.get("symbol") or "???"
    reason = item.get("reason") or "نشاط ملحوظ"
    chain = chain_label(item.get("chain"))
    contract = item.get("contract", "")
    medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "%d." % i

    # optional small USD hint if available
    usd = ""
    if prices and contract:
        val = prices.get(contract.lower(), 0) * item.get("amount", 0)
        if val > 0:
            usd = "  ·  " + money(val)

    link = ""
    if contract:
        link = '  <a href="%s">📊</a>' % dex_url(item)

    return "%s  <code>%s</code>  <i>%s</i>%s%s\n   └ %s" % (
        medal, esc(symbol), esc(chain), usd, link, esc(reason)
    )


def format_report(data, direction, minutes):
    title = "📥 إيداعات" if direction == "in" else "📤 سحوبات"
    chains = data.get("_meta", {}).get("chains", [])
    chain_txt = " · ".join(CHAINS.get(c, {}).get("name", c) for c in chains) if chains else "متعدد"
    lines = [SEP, "%s  |  %s" % (title, period(minutes)), "الشبكات: %s" % chain_txt, SEP]

    tokens = [t for t in (data.get("_combined") or []) if t.get("count", 0) >= MIN_TRANSFERS]
    if not tokens:
        # fallback merge from wallets
        combined = {}
        for label, wallet in data.items():
            if label.startswith("_"):
                continue
            for t in wallet.get("tokens", []):
                ckey = "%s:%s" % (t.get("chain"), t.get("contract"))
                if ckey not in combined:
                    combined[ckey] = dict(t)
                else:
                    combined[ckey]["amount"] += t.get("amount", 0)
                    combined[ckey]["count"] += t.get("count", 0)
                    combined[ckey]["score"] = combined[ckey].get("score", 0) + t.get("score", 0)
        tokens = sorted(combined.values(), key=lambda x: x.get("score", x.get("amount", 0)), reverse=True)[:TOP_N]
        for t in tokens:
            if "reason" not in t:
                from tracker import build_reason
                t["reason"] = build_reason(t)

    if not tokens:
        return "\n".join(lines + ["لا توجد توكنات.", SEP])

    prices = get_usd_prices(tokens)
    for i, t in enumerate(tokens[:TOP_N], 1):
        lines.append(clean_line(i, t, prices))
        lines.append("")
    return "\n".join(lines + [SEP])


def format_opportunity(data, minutes):
    tokens = data.get("ranked") or []
    chains = data.get("_meta", {}).get("chains", [])
    chain_txt = " · ".join(CHAINS.get(c, {}).get("name", c) for c in chains) if chains else ""
    lines = [SEP, "🎯 فرصة  |  %s" % period(minutes), "الشبكات: %s" % chain_txt, SEP]
    if not tokens:
        return "\n".join(lines + ["لا توجد فرص.", SEP]), ""
    prices = get_usd_prices(tokens)
    for i, t in enumerate(tokens[:TOP_N], 1):
        lines.append(clean_line(i, t, prices))
        lines.append("")
    url = dex_url(tokens[0]) if tokens else ""
    return "\n".join(lines + [SEP]), url


def format_clean_opportunity(data, minutes):
    clean = data.get("clean") or []
    tainted = data.get("tainted") or []
    lines = [SEP, "🚫 فرصة نقية  |  %s" % period(minutes), SEP]
    url = ""
    if clean:
        url = dex_url(clean[0])
        prices = get_usd_prices(clean)
        lines.append("✅ سحب صافٍ")
        for i, t in enumerate(clean[:TOP_N], 1):
            lines.append(clean_line(i, t, prices))
            lines.append("")
    else:
        lines.append("لا توجد سحوبات نقية.")
    if tainted:
        lines.append("⚠️ ضغط بيع محتمل")
        prices = get_usd_prices(tainted)
        for i, t in enumerate(tainted[:5], 1):
            lines.append(clean_line(i, t, prices))
            lines.append("")
    return "\n".join(lines + [SEP]), url


def format_best_opportunities(data):
    tokens = data.get("ranked") or []
    completed = data.get("_meta", {}).get("completed_windows", [])
    lines = [
        SEP,
        "🏆 أفضل الفرص  |  " + " ".join(WIN.get(x, str(x)) for x in completed),
        SEP,
    ]
    if not tokens:
        return "\n".join(lines + ["لا توجد توكنات متكررة.", SEP]), ""
    prices = get_usd_prices(tokens)
    for i, t in enumerate(tokens[:TOP_N], 1):
        lines.append(clean_line(i, t, prices))
        lines.append("")
    url = dex_url(tokens[0]) if tokens else ""
    return "\n".join(lines + [SEP]), url


def format_discovery(data, minutes):
    lines = [SEP, "🔭 اكتشاف  |  %s" % period(minutes), SEP]

    def p(x):
        a = x["addr"]
        return "<code>%s…%s</code>  %s تحويل  <a href=\"%s\">🔗</a>" % (
            esc(a[:6]), esc(a[-4:]), x.get("cnt_total", x.get("cnt_out", x.get("cnt_in", 0))),
            explorer_addr(a, data.get("_meta", {}).get("chain", "bsc")),
        )

    if data.get("top_withdrawers"):
        lines.append("📤 أكبر المستلمين")
        for x in data["top_withdrawers"][:6]:
            lines.append(p(x))
    if data.get("top_depositors"):
        lines.append("")
        lines.append("📥 أكبر المودعين")
        for x in data["top_depositors"][:6]:
            lines.append(p(x))
    if data.get("top_bidirectional"):
        lines.append("")
        lines.append("🔄 تفاعل ثنائي")
        for x in data["top_bidirectional"][:5]:
            lines.append(p(x))
    if len(lines) <= 3:
        lines.append("لا توجد محافظ.")
    return "\n".join(lines + [SEP])


def format_whales(data):
    if data.get("error"):
        return data["error"]
    symbol = data.get("symbol", "???")
    chain = chain_label(data.get("chain", "bsc"))
    minutes = data.get("minutes", 60)
    whales = data.get("whales", [])
    lines = [
        SEP,
        "🐋 حيتان <code>%s</code>  ·  %s  |  %s" % (esc(symbol), chain, period(minutes)),
        SEP,
    ]
    if not whales:
        return "\n".join(lines + ["لا يوجد نشاط كافٍ.", SEP])
    for i, w in enumerate(whales, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "%d." % i
        a = w["addr"]
        reason = w.get("reason") or ("%d تحويل" % w.get("count", 0))
        lines.append(
            "%s  <code>%s…%s</code>\n   └ %s  ·  ↑%s ↓%s"
            % (medal, esc(a[:6]), esc(a[-4:]), esc(reason), qty(w.get("in_amount", 0)), qty(w.get("out_amount", 0)))
        )
        lines.append("")
    lines.append("💡 أرسل: <code>إضافة حوت اسم 0x...</code>")
    return "\n".join(lines + [SEP])
