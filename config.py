import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TOP_N = 10
MIN_TRANSFERS = 3

# Continuous monitoring (free-friendly)
MONITOR_INTERVAL_SEC = 300          # كل 5 دقايق
ALERT_COOLDOWN_SEC = 1200           # 20 دقيقة cooldown لنفس التوكن
MIN_WALLETS_FOR_ALERT = 1           # محفظتين على الأقل
MIN_SCORE_FOR_ALERT = 20000
MAX_WALLETS_PER_CYCLE = 2           # يراقب 4 محافظ فقط في الدورة الواحدة (بالتناوب)

DEFAULT_WALLETS = {
    "MEXC 1": "0x9642b23ed1e01df1092b92641051881a322f5d4e",
    "MEXC 2": "0x4982085c9e2f89f2ecb8131eca71afad896e89cb",
}

TIME_PERIODS = [
    ("5 دقائق", 5),
    ("15 دقيقة", 15),
    ("30 دقيقة", 30),
    ("ساعة", 60),
    ("ساعتان", 120),
    ("4 ساعات", 240),
    ("6 ساعات", 360),
    ("12 ساعة", 720),
    ("24 ساعة", 1440),
]

# Free-friendly chains only (most reliable public RPCs)
CHAINS = {
    "bsc": {
        "name": "BSC",
        "rpc": "https://bsc-dataseed.binance.org",
        "rpc_backup": "https://rpc-bnb.blockmachine.io",
        "dex": "bsc",
        "explorer": "https://bscscan.com",
        "native": "BNB",
        "blocks_per_min": 20,
    },
    "base": {
        "name": "Base",
        "rpc": "https://mainnet.base.org",
        "rpc_backup": "https://base.llamarpc.com",
        "dex": "base",
        "explorer": "https://basescan.org",
        "native": "ETH",
        "blocks_per_min": 30,
    },
}

ACTIVE_CHAINS = list(CHAINS.keys())
