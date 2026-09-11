"""
الإعدادات محفوظة داخل قاعدة البيانات SQLite (trades.db)
عشان متضيعش مع إعادة التشغيل (مع Volume على Railway).
"""
import trades_db

DEFAULTS = trades_db.DEFAULT_SETTINGS


def load():
    return trades_db.load_settings()


def save(data):
    return trades_db.save_settings(data)


def get(key, default=None):
    return trades_db.get_setting(key, default)


def set_value(key, value):
    return trades_db.set_setting(key, value)


def text():
    s = load()
    status = "🟢 شغال" if s.get("monitoring_enabled") else "🔴 متوقف"
    auto = "🟢 مفعّل" if s.get("auto_buy_enabled") else "🔴 معطّل"
    return (
        f"⚙️ <b>إعدادات التداول</b>\n\n"
        f"📡 الرصد: {status}\n"
        f"🤖 الشراء التلقائي: {auto}\n\n"
        f"💵 حجم الصفقة: <b>{s.get('trade_size_usd')}$</b>\n"
        f"🛑 وقف الخسارة: <b>{s.get('stop_loss_pct')}%</b>\n"
        f"🎯 الهدف 1: <b>+{s.get('tp1_pct')}%</b>\n"
        f"🎯 الهدف 2: <b>+{s.get('tp2_pct')}%</b>\n"
        f"🎯 الهدف 3: <b>+{s.get('tp3_pct')}%</b>\n"
        f"<i>البوت يتابع صفقاته فقط (من قاعدة البيانات)</i>"
    )
