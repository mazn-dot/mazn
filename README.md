# MEXC SPOT Auto Trader

Single-file Python bot that monitors a public Telegram channel via Web Preview and executes **SPOT only** trades on MEXC.

## Important Warnings
- SPOT BUY only (LONG signals). SHORT is ignored.
- Leverage is ignored completely.
- No Futures / Margin / Swap / Perpetual.
- No Telegram API ID / Hash / Telethon / Pyrogram.
- Paper mode is ON by default.
- You are fully responsible for any losses. Trading involves high risk of capital loss.
- Past signals do not guarantee future results.

## Files
- `main.py` — complete logic in one file
- `requirements.txt`
- `.env.example`
- `README.md`

## Setup (GitHub + Railway)

1. Create a new GitHub repository.
2. Upload these 4 files.
3. On Railway: New Project → Deploy from GitHub repo.
4. Add Environment Variables (copy from `.env.example` and fill real values). Never put secrets in the code.
5. Deploy. The service runs 24/7.
6. Create a Telegram Bot via @BotFather → put the token in `TELEGRAM_BOT_TOKEN`.
7. Get your numeric Telegram user ID (via @userinfobot) → `TELEGRAM_ADMIN_ID`.
8. Start with `PAPER_MODE=true` and `TRADING_ENABLED=false`.
9. Message your bot: `/start` `/status` `/test`.
10. After thorough testing only: turn paper off (`/paper`) then enable trading (`/on`). Use a very small `TRADE_AMOUNT_USDT`.

## Bot Commands
- `/start` — help
- `/status` — system status
- `/on` — enable trading
- `/off` — disable trading
- `/stop` — kill switch (immediate stop)
- `/balance` — MEXC Spot balances
- `/positions` — open positions managed by the bot
- `/amount 15` — change trade size in USDT
- `/paper` — toggle paper mode
- `/settings` — current settings
- `/last` — last detected signal
- `/test` — connectivity test

## Safety Features
- Secrets only from environment variables
- Hard limit on trade size
- Skips all old messages on first start
- Idempotent (Message ID + hash) to prevent double execution
- Only the admin ID can control the bot
- SPOT market only, no leverage, no short
