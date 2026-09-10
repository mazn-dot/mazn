import json
import os

FILE = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULTS = {
    "trade_size_usd": 20.0,       # حجم الصفقة بالدولار
    "stop_loss_pct": -8.0,        # وقف الخسارة %
    "tp1_pct": 5.0,               # الهدف الأول %
    "tp2_pct": 10.0,              # الهدف الثاني %
    "tp3_pct": 15.0,              # الهدف الثالث %
    "max_open_trades": 3,         # أقصى صفقات مفتوحة
    "monitoring_enabled": False,  # الرصد شغال ولا لأ
    "auto_buy_enabled": False,    # الشراء التلقائي شغال ولا لأ
}


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        out = dict(DEFAULTS)
        out.update(data)
        return out
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(data):
    current = load()
    current.update(data)
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return current


def get(key, default=None):
    return load().get(key, default if default is not None else DEFAULTS.get(key))


def set_value(key, value):
    return save({key: value})


def text():
    s = load()
    status = "🟢 شغال" if s["monitoring_enabled"] else "🔴 متوقف"
    auto = "🟢 مفعّل" if s["auto_buy_enabled"] else "🔴 معطّل"
    return (
        f"⚙️ <b>إعدادات التداول</b>\n\n"
        f"📡 الرصد: {status}\n"
        f"🤖 الشراء التلقائي: {auto}\n\n"
        f"💵 حجم الصفقة: <b>{s['trade_size_usd']}$</b>\n"
        f"🛑 وقف الخسارة: <b>{s['stop_loss_pct']}%</b>\n"
        f"🎯 الهدف 1: <b>+{s['tp1_pct']}%</b>\n"
        f"🎯 الهدف 2: <b>+{s['tp2_pct']}%</b>\n"
        f"🎯 الهدف 3: <b>+{s['tp3_pct']}%</b>\n"
        f"📊 أقصى صفقات مفتوحة: <b>{s['max_open_trades']}</b>"
    )
