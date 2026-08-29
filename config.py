import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TOP_N = 12
MIN_TRANSFERS = 10

DEFAULT_WALLETS = {
    "Bybit Hot 1": "0xf89d7b9c864f589bbF53a82105107622B35EaA40",
    "Bybit Hot 2": "0xA7A93fd0a276fc1C0197a5B5623eD117786eeD06",
    "Bybit": "0xee5B5B923fFcE93A870B3104b7CA09c3db80047A",
    "Whale A": "0x10b620f9720C0c6460484A81C59a6297Fa48F817",
    "Whale B": "0xa1ab382330d6b7a99ee3441e6594e49790294e4e",
    "Whale C": "0x1Db92e2EeBC8E0c075a02BeA49a2935BcD2dFCF4",
    "Whale D": "0x88a1493366d48225fc3cefbdae9ebb23e323ade3",
    "Whale E": "0x631fc1ea2270e98fbd9d92658ece0f5a269aa161",
    "Whale F": "0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573",
    "Whale G": "0x53f78a071d04224b8e254e243fffc6d9f2f3fa23",
}

TIME_PERIODS = [
    ("5 دقائق", 5),
    ("15 دقيقة", 15),
    ("30 دقيقة", 30),
    ("ساعة", 60),
    ("ساعتان", 120),
    ("4 ساعات", 240),
    ("6 ساعات", 360),
    ("8 ساعات", 480),
    ("12 ساعة", 720),
    ("24 ساعة", 1440),
]

# 5 EVM chains — public RPCs (rate-limited, no API key)
CHAINS = {
    "bsc": {
        "name": "BSC",
        "rpc": "https://rpc-bnb.blockmachine.io",
        "dex": "bsc",
        "explorer": "https://bscscan.com",
        "native": "BNB",
        "blocks_per_min": 20,
    },
    "ethereum": {
        "name": "ETH",
        "rpc": "https://eth.llamarpc.com",
        "dex": "ethereum",
        "explorer": "https://etherscan.io",
        "native": "ETH",
        "blocks_per_min": 5,
    },
    "base": {
        "name": "Base",
        "rpc": "https://mainnet.base.org",
        "dex": "base",
        "explorer": "https://basescan.org",
        "native": "ETH",
        "blocks_per_min": 30,
    },
    "arbitrum": {
        "name": "ARB",
        "rpc": "https://arb1.arbitrum.io/rpc",
        "dex": "arbitrum",
        "explorer": "https://arbiscan.io",
        "native": "ETH",
        "blocks_per_min": 15,
    },
    "polygon": {
        "name": "Polygon",
        "rpc": "https://polygon-rpc.com",
        "dex": "polygon",
        "explorer": "https://polygonscan.com",
        "native": "MATIC",
        "blocks_per_min": 30,
    },
}

ACTIVE_CHAINS = list(CHAINS.keys())
