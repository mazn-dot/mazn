"""
سكريبت مرة واحدة بس - شغّله على جهازك الشخصي (مش على Railway).

بيسجل دخول بحسابك الشخصي على تليجرام (نفس الحساب المشترك في قنوات التوصيات)
وبيطلعلك "Session String" - نص طويل تحطه في متغير TELEGRAM_SESSION_STRING على Railway
عشان البوت يقدر يقرأ رسايل القنوات نيابة عنك من غير ما يحتاج يسجل دخول تاني.

طريقة الاستخدام:
1. روح https://my.telegram.org وسجل دخول برقمك، وخد API_ID و API_HASH من
   "API development tools".
2. شغل الأمر ده في نفس مجلد المشروع:
       pip install telethon
       python generate_session.py
3. هيطلبلك API_ID و API_HASH ورقم تليفونك وكود التفعيل اللي هيجيلك على تليجرام
   (وباسورد التحقق بخطوتين لو مفعّله).
4. في الآخر هيطبعلك الـ Session String - انسخه كامل وحطه في Railway Variables
   باسم TELEGRAM_SESSION_STRING.

⚠️ الـ Session String ده زي كلمة سر حسابك بالكامل - محدش غيرك يشوفه، ومتحطوش
في أي مكان عام (GitHub، محادثة، إلخ).
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("=== توليد Session String لحسابك على تليجرام ===\n")
api_id = input("API_ID (من my.telegram.org): ").strip()
api_hash = input("API_HASH (من my.telegram.org): ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    session_string = client.session.save()
    print("\n✅ تم تسجيل الدخول بنجاح!\n")
    print("انسخ السطر ده كامل وحطه في Railway -> Variables -> TELEGRAM_SESSION_STRING:\n")
    print(session_string)
    print("\n⚠️ متشاركش السطر ده مع حد - هو زي كلمة سر حسابك بالكامل.")
