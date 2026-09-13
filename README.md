# MEXC SPOT Auto Trader v2

## Environment Variables (Railway) — فقط 4 متغيرات

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_ID=...
MEXC_API_KEY=...
MEXC_SECRET_KEY=...
```

**كل شيء آخر يُدار من بوت تيليجرام بالأزرار:**
- مبلغ الصفقة
- Paper Mode
- تشغيل/إيقاف التداول
- حد عدد الصفقات المفتوحة
- نسب الـ 3 أهداف (TP1 / TP2 / TP3)
- إضافة/حذف قنوات الإشارات

## الميزات
- تحكم كامل بأزرار Inline
- 3 أهداف + رفع الوقف تلقائي عند TP1
- الوقف يُؤخذ من التوصية نفسها
- قراءة القنوات العامة بدون API ID/Hash
- SPOT فقط (لا فيوتشر ولا رافعة ولا شورت)
- قاعدة بيانات SQLite

## Railway
أضف Volume وحدد `DATA_DIR=/data` حتى لا تُحذف قاعدة البيانات عند إعادة النشر.
