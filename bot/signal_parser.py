"""
استخراج توصيات التداول من نصوص بشرية في قنوات تليجرام.

شكل التوصية المتوقع (مرن، بيقبل أكتر من عملة في نفس الرسالة):

    Pyth 🔥
    Long 15x
    Enter : 0.041..
    Target : 0.05050
    Stop : 0.0398

بيدعم كمان أهداف متعددة بأي صيغة من الصيغ دي:
    Target 1 : ...
    Target1 : ...
    TP1 : ...

ملاحظات مهمة:
- البوت ده لتداول SPOT فقط، فأي توصية "Short" بيتم تجاهلها تلقائياً
  (مينفعش نبيع على المكشوف في سبوت عادي).
- رقم الرافعة (15x) بيتقرأ بس مش بيتطبق - التنفيذ دايماً بدون رافعة.
- الأرقام زي "0.041.." (بنقطتين زيادة في الآخر) بيتم التعامل معاها صح -
  بناخد الرقم العشري الصحيح وبس ونتجاهل أي نقط زيادة بعده.
"""
import re
from dataclasses import dataclass, field


@dataclass
class ParsedSignal:
    symbol_raw: str          # الاسم زي ما جه في الرسالة، مثلاً "Pyth"
    side: str                # "long" أو "short"
    leverage: int | None
    entry: float
    stop: float | None
    targets: list = field(default_factory=list)  # قائمة أهداف الربح (float)
    skip_reason: str | None = None  # لو فيه سبب يمنع التنفيذ (مثلاً short) بيتحط هنا


# رمز العملة: حرف واحد على الأقل (W, X, BTC...) + إيموجي اختياري
_HEADER_RE = re.compile(r'^\s*#?([A-Za-z][A-Za-z0-9/]{0,16})\s*(?:🔥|🚀|🟢|🔴)?\s*$')
# سطور الاتجاه أو كلمات عامة - مش رؤوس توصيات (تُعامل كجزء من التوصية الحالية)
_SIDE_LINE_RE = re.compile(
    r'^\s*(Buy|Sell|Long|Short|Buy/Long|Sell/Short|Long/Short|L/S|L|S|Cake|Binance Signals|🔥|Signals)\s*(\d{1,3})?\s*[xX]?\s*$',
    re.IGNORECASE)
# رؤوس غير صالحة: كلمات عامة مش أسماء عملات
_INVALID_HEADER = {
    "BUY", "SELL", "LONG", "SHORT", "BUY/LONG", "SELL/SHORT", "LONG/SHORT",
    "L/S", "L", "S", "SPOT", "SWING", "USDT", "USD", "CAKE", "SIGNALS",
}
_SIDE_RE = re.compile(r'\b(Long|Short)\b\s*(\d{1,3})\s*[xX]?', re.IGNORECASE)
# يدعم Long/Short بأي صيغة مكتوبة في أي سطر: "Long 15x" أو "Long/15x" أو "SHORT" أو "L/S 15x"
_SIDE_FLEX_RE = re.compile(r'\b(LONG|SHORT|Long|Short|L/S|L|S)\b', re.IGNORECASE)
# صيغ عربية (قنوات توصيات سبوت بتكتب بالعربي):
# "الدخول من السعر الحالي : 0.0045" أو "الدخول: 0.0045" أو "ادخل: ..."
_ENTRY_RE = re.compile(
    r'\b(?:Enter|Entry|DCA|Buy Zone)\s*:?\s*(\d+\.?\d*)|(?:دخول|ادخل)\s*[^\d]*?(\d+\.?\d*)',
    re.IGNORECASE)
_STOP_RE = re.compile(
    r'\bStop\s*(?:Loss)?\s*:?\s*(\d+\.?\d*)|(?:وقف|ستوب)\s*[^\d]*?(\d+\.?\d*)', re.IGNORECASE)
_TARGET_RE = re.compile(r'(?:Targets?|TP)\s*\d*\s*:?\s*([\d\.\s]+)', re.IGNORECASE)
# هدف بالعربي: "الهدف : 0.01450" أو "هدف : ..." — أول رقم بعد كلمة هدف
_TARGET_AR_RE = re.compile(r'(?:هدف)\s*[^\d]*?(\d+\.?\d*)', re.IGNORECASE)


def _split_blocks(text: str):
    """يقسم الرسالة لأجزاء - كل جزء بيبدأ بسطر فيه اسم عملة + إيموجي، لحد أول سطر تاني
    من نفس النوع أو نهاية الرسالة."""
    lines = text.splitlines()
    blocks = []
    current_symbol = None
    current_lines = []

    for line in lines:
        m = _HEADER_RE.match(line)
        if m and not _SIDE_LINE_RE.match(line):
            sym = m.group(1)
            if sym.upper() in _INVALID_HEADER:
                if current_symbol is not None:
                    current_lines.append(line)
                continue
            if current_symbol is not None:
                blocks.append((current_symbol, "\n".join(current_lines)))
            current_symbol = sym
            current_lines = []
        else:
            if current_symbol is not None:
                current_lines.append(line)

    if current_symbol is not None:
        blocks.append((current_symbol, "\n".join(current_lines)))

    return blocks


def parse_signals(text: str) -> list[ParsedSignal]:
    """بيرجع قائمة بكل التوصيات اللي لقاها في الرسالة (ممكن تكون أكتر من عملة في نفس الرسالة).
    لو مفيش أي توصية متعرفة، بترجع قائمة فاضية.

    بيدعم كمان صيغة السبوت العربية:
        سبوت " spot " 🟢
        W
        الدخول من السعر الحالي  :  0.010
        الهدف : 0.0177 ✅
        الوقف : 0.00925 🛑
    """
    if not text:
        return []

    # اتجاه عام من الرسالة كلها (🟢 / 🔴 / سبوت)
    text_low = text.lower()
    global_side = None
    if "🟢" in text or re.search(r'\b(شراء|buy)\b', text_low):
        global_side = "long"
    elif "🔴" in text or re.search(r'\b(بيع|sell)\b', text_low):
        global_side = "short"
    elif re.search(r'سبوت|spot', text_low):
        global_side = "long"  # إشارات السبوت بدون اتجاه = شراء

    results = []
    for symbol_raw, block in _split_blocks(text):
        # ابحث في البلوك + الرسالة كلها للدخول/الوقف (صيغة السبوت بتكتبهم بعد اسم العملة)
        search_in = block if block.strip() else text
        side_m = _SIDE_RE.search(block) or _SIDE_RE.search(text)
        entry_m = _ENTRY_RE.search(search_in) or _ENTRY_RE.search(text)
        stop_m = _STOP_RE.search(search_in) or _STOP_RE.search(text)
        target_matches = _TARGET_RE.findall(search_in)
        # كل مطابقة قد تحتوي أكتر من رقم في نفس السطر (Targets: 0.054 0.058 0.062)
        flat_targets = []
        for tm in target_matches:
            flat_targets.extend(re.findall(r'\d+\.?\d*', tm))
        # دعم سطر 'Target : 1.8' بدون كلمة Target متبوعة برقم
        if not flat_targets:
            flat_targets = re.findall(r'\bTarget\s*:?\s*(\d+\.?\d*)', search_in, re.IGNORECASE)
        # دعم الهدف بالعربي "الهدف : 0.01450"
        flat_targets.extend(_TARGET_AR_RE.findall(search_in))
        if not flat_targets:
            flat_targets.extend(_TARGET_AR_RE.findall(text))
        target_matches = flat_targets

        if not entry_m:
            continue  # مش توصية فعلية

        if side_m:
            side = side_m.group(1).lower()
            if side in ("l",):
                side = "long"
            elif side in ("s",):
                side = "short"
        else:
            flex_m = _SIDE_FLEX_RE.search(block) or _SIDE_FLEX_RE.search(text)
            if flex_m:
                tok = flex_m.group(1).upper()
                side = "long" if tok in ("LONG", "L") else "short"
            elif global_side:
                side = global_side
            else:
                side = "long"
        leverage = int(side_m.group(2)) if side_m and side_m.lastindex and side_m.lastindex >= 2 and side_m.group(2) else None
        entry = float(entry_m.group(1) or entry_m.group(2))
        stop = float(stop_m.group(1) or stop_m.group(2)) if stop_m else None
        targets = [float(t) for t in target_matches]

        # لو مفيش أهداف من الرسالة → نولّد ثلاث أهداف +5% من الدخول
        if not targets and entry > 0:
            targets = [entry * 1.05, entry * 1.10, entry * 1.15]

        skip_reason = None
        if side == "short":
            skip_reason = "توصية Short (بيع على المكشوف) - البوت ده سبوت فقط، تم التجاهل."
        elif stop is None:
            skip_reason = "مفيش ستوب لوس مذكور في التوصية - تم التجاهل لتفادي مخاطرة غير محسوبة."
        elif not targets:
            skip_reason = "مفيش أي هدف ربح (Target) مذكور في التوصية - تم التجاهل."

        results.append(ParsedSignal(
            symbol_raw=symbol_raw,
            side=side,
            leverage=leverage,
            entry=entry,
            stop=stop,
            targets=targets,
            skip_reason=skip_reason,
        ))

    return results


def to_spot_symbol(symbol_raw: str, aliases: dict | None = None) -> str:
    """يحول اسم العملة زي ما جه في التوصية (مثلاً 'Pyth') لصيغة زوج سبوت جاهزة
    للتحقق منها على المنصة (مثلاً 'PYTH/USDT'). لو فيه alias مسجل بيتاخد بالأولوية."""
    aliases = aliases or {}
    key = symbol_raw.upper()
    if key in aliases:
        return aliases[key]
    return f"{key}/USDT"


def normalize_to_three_targets(entry: float, targets: list) -> list:
    """
    ثلاث أهداف ثابتة كل هدف +5% من سعر الدخول:
      TP1 = entry × 1.05
      TP2 = entry × 1.10
      TP3 = entry × 1.15

    لو مفيش entry صالح، بيرجع الأهداف الأصلية (مرتبة ومقصوصة لـ 3).
    """
    if entry and entry > 0:
        return [
            round(entry * 1.05, 8),  # هدف 1: +5%
            round(entry * 1.10, 8),  # هدف 2: +10%
            round(entry * 1.15, 8),  # هدف 3: +15%
        ]

    if not targets:
        return []
    sorted_targets = sorted(targets)
    return sorted_targets[:3]
