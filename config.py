import os
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")
TOP_N = 15
DEFAULT_WALLETS = {"MEXC": "0x4982085c9e2f89f2ecb8131eca71afad896e89cb", "Wallet 2": "0x0d0707963952f2fba59dd06f2b425ace40b492fe"}
TIME_PERIODS = [("5 دقائق", 5), ("15 دقيقة", 15), ("30 دقيقة", 30), ("ساعة", 60), ("ساعتان", 120), ("4 ساعات", 240), ("6 ساعات", 360), ("8 ساعات", 480), ("12 ساعة", 720), ("24 ساعة", 1440)]
