"""
إعدادات البوت المشتركة - بتتغير لايف من تليجرام (أوامر /set...) بدون أي حاجة
في Railway أو إعادة تشغيل السيرفر.

ملاحظة مهمة: البوت ده بيتداول SPOT فقط، وبيعتمد بالكامل على توصيات قنوات
تليجرام المراقَبة (مفيش استراتيجية داخلية زي EMA/RSI). مفيش رافعة مالية
ومفيش بيع على المكشوف (short) - أي مركز بيتفتح هو شراء (buy) فعلي للعملة،
وبيتقفل ببيعها تاني عند الستوب لوس أو هدف الربح المذكورين في التوصية.
"""
import threading
from datetime import datetime, timezone

from .config import Config


def _parse_bool(value) -> bool:
    """تحويل ذكي لنص جاي من تليجرام (زي '/set signal_trading_enabled false') لقيمة bool صحيحة -
    bool() العادي بيرجع True لأي نص غير فاضي حتى لو كان 'false'، فمحتاجين تحويل يدوي."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "تشغيل", "فعل", "شغال")


# ---- القيم الافتراضية ----
DEFAULT_SETTINGS = {
    "dry_run": Config.dry_run_default(),   # القيمة الابتدائية بتيجي من متغير Railway: DRY_RUN (افتراضياً true = تجربة آمنة)
    "paused": False,                  # true = إيقاف فتح صفقات جديدة مؤقتاً (المراكز المفتوحة لسه بتتراقب)
    "market_type": "spot",            # ثابت على spot - البوت ده لتداول السبوت فقط
    "signal_channels": Config.signal_channels_list(),  # قابلة للتعديل لايف من تليجرام (بالإضافة لمتغير SIGNAL_CHANNELS الابتدائي)
    "max_daily_loss_pct": 3.0,
    "max_open_positions": 10,
    "poll_interval_seconds": 15,
    # ---- إعدادات مراقبة قنوات التوصيات (Telethon) - المصدر الوحيد لفتح صفقات ----
    "signal_trading_enabled": True,     # تشغيل/إيقاف تنفيذ التوصيات من القنوات
    "signal_trade_amount_usdt": 10.0,   # مبلغ ثابت (USDT) لكل توصية بيتم تنفيذها
    "signal_price_tolerance_pct": 3.0,  # لو السعر الحالي بعيد عن سعر الدخول المذكور بأكتر من كده، بتتجاهل التوصية (سعر قديم/متغير)
}

# نوع كل إعداد - مستخدم في التحقق وتحويل القيمة جاية من تليجرام كنص
SETTING_TYPES = {
    "max_daily_loss_pct": float,
    "max_open_positions": int,
    "poll_interval_seconds": int,
    "signal_trading_enabled": _parse_bool,
    "signal_trade_amount_usdt": float,
    "signal_price_tolerance_pct": float,
}


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._settings = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
        self.start_time = datetime.now(timezone.utc)

    # ---- عام ----
    def get(self, key):
        with self._lock:
            return self._settings.get(key)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def set(self, key: str, value):
        with self._lock:
            self._settings[key] = value

    # ---- اختصارات شائعة ----
    def is_dry_run(self) -> bool:
        return bool(self.get("dry_run"))

    def set_dry_run(self, val: bool):
        self.set("dry_run", val)

    def is_paused(self) -> bool:
        return bool(self.get("paused"))

    def set_paused(self, val: bool):
        self.set("paused", val)

    # ---- قنوات التوصيات (قابلة للتعديل لايف بالأزرار/الأوامر) ----
    def get_signal_channels(self) -> list[str]:
        with self._lock:
            return list(self._settings["signal_channels"])

    def set_signal_channels(self, channels: list[str]):
        """يستبدل قائمة القنوات بالكامل — يُستخدم بعد دمج القنوات المحفوظة دائمًا من DB."""
        with self._lock:
            self._settings["signal_channels"] = list(channels)

    def add_signal_channel(self, channel: str) -> bool:
        with self._lock:
            if channel in self._settings["signal_channels"]:
                return False
            self._settings["signal_channels"].append(channel)
            return True

    def remove_signal_channel(self, channel: str) -> bool:
        with self._lock:
            if channel not in self._settings["signal_channels"]:
                return False
            self._settings["signal_channels"].remove(channel)
            return True


shared_state = SharedState()
