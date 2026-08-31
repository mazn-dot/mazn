"""
Railway Variables محدودة بس لأسرار الاتصال (API + بوت + قاعدة بيانات).
كل إعدادات الاستراتيجية والمخاطرة اتنقلت لـ state.py وبتتحكم فيها لايف من تليجرام.
"""
import os


class Config:
    # ---- MEXC API credentials ----
    MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
    MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")

    # ---- بوت تليجرام (التحكم الكامل) ----
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # آيديات المستخدمين المصرح لهم - مفصولة بفاصلة لو أكتر من شخص
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    # اختياري: آيديات إضافية مصرح لها بالتحكم (بيتضاف لقائمة TELEGRAM_CHAT_ID مش بيلغيها)
    TELEGRAM_ADMIN_IDS = os.getenv("TELEGRAM_ADMIN_IDS", "")

    # ---- وضع التشغيل الافتراضي وقت إقلاع البوت لأول مرة ----
    # DRY_RUN=true يخلي البوت يبدأ في وضع تجربة (آمن) - القيمة الافتراضية لو المتغير مش موجود
    # أصلاً هي "true" (تجربة) عشان محدش يشغّل تداول حقيقي بالغلط من غير قصد.
    # تقدر بعد كده تغيّرها لايف من تليجرام بـ /dryrun_on أو /dryrun_off في أي وقت.
    DRY_RUN = os.getenv("DRY_RUN", "true")

    # ---- قاعدة البيانات (Railway Postgres) ----
    DATABASE_URL = os.getenv("DATABASE_URL", "")  # Railway بيحطها تلقائي عند إضافة Postgres plugin

    # ---- Telethon (حساب تليجرام شخصي لقراءة قنوات التوصيات كعضو عادي) ----
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
    # الجلسة (Session String) بتتولد مرة واحدة محلياً بسكريبت generate_session.py
    # وبتتحط هنا كمتغير بيئة - مينفعش تسجيل دخول تفاعلي على Railway نفسه.
    TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")
    # أسماء أو آيديات القنوات المراد مراقبتها - مفصولة بفاصلة
    # مثال: SIGNAL_CHANNELS=channel_username_1,channel_username_2 أو -1001234567890
    SIGNAL_CHANNELS = os.getenv("SIGNAL_CHANNELS", "")

    @classmethod
    def signal_channels_list(cls):
        return [c.strip() for c in cls.SIGNAL_CHANNELS.split(",") if c.strip()]

    @classmethod
    def allowed_chat_ids_list(cls):
        """يجمع TELEGRAM_CHAT_ID و TELEGRAM_ADMIN_IDS في قائمة واحدة (بدون تكرار)."""
        ids = [c.strip() for c in cls.TELEGRAM_CHAT_ID.split(",") if c.strip()]
        ids += [c.strip() for c in cls.TELEGRAM_ADMIN_IDS.split(",") if c.strip()]
        return sorted(set(ids))

    @classmethod
    def dry_run_default(cls) -> bool:
        return str(cls.DRY_RUN).strip().lower() in ("1", "true", "yes", "on")

    @classmethod
    def validate(cls):
        missing = []
        if not cls.MEXC_API_KEY:
            missing.append("MEXC_API_KEY")
        if not cls.MEXC_API_SECRET:
            missing.append("MEXC_API_SECRET")
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if not cls.DATABASE_URL:
            missing.append("DATABASE_URL")
        if missing:
            raise RuntimeError(
                f"متغيرات بيئة ناقصة: {', '.join(missing)}. ضيفها في Railway -> Variables."
            )

    @classmethod
    def validate_signal_listener(cls):
        """يتحقق من متغيرات مراقبة قنوات التوصيات - منفصلة عن التحقق الأساسي
        عشان البوت الأساسي يشتغل عادي حتى لو ميزة التوصيات لسه متظبطتش.
        SIGNAL_CHANNELS مش شرط هنا لأن القنوات ممكن تتضاف لايف من تليجرام
        بعد التشغيل (أزرار إدارة القنوات) - مش لازم تكون موجودة وقت الإقلاع."""
        missing = []
        if not cls.TELEGRAM_API_ID:
            missing.append("TELEGRAM_API_ID")
        if not cls.TELEGRAM_API_HASH:
            missing.append("TELEGRAM_API_HASH")
        if not cls.TELEGRAM_SESSION_STRING:
            missing.append("TELEGRAM_SESSION_STRING")
        return missing
