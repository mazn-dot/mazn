# MEXC SPOT Auto Trader v2

## المتغيرات في Railway (4 فقط)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_ID=...
MEXC_API_KEY=...
MEXC_SECRET_KEY=...
```

لو أضفت **PostgreSQL** من Railway → هيتضاف تلقائي `DATABASE_URL` والكود هيستخدمه مباشرة.

لو مفيش `DATABASE_URL` → هيستخدم SQLite (ملف محلي).

**كل الإعدادات التانية من البوت فقط:**
- مبلغ الصفقة
- Paper Mode
- تشغيل/إيقاف التداول
- حد عدد الصفقات
- نسب الـ 3 أهداف
- إدارة القنوات

## الميزات
- أزرار كاملة
- 3 أهداف + رفع الوقف عند TP1
- الوقف من التوصية
- قراءة القنوات بدون API ID/Hash
- SPOT فقط
