# MEXC SPOT Auto Trader v2

Single-file bot with full Telegram control (inline buttons), multiple channels, 3 TPs + trailing stop, max positions limit, and SQLite database.

## Features
- **Buttons only control** from your Telegram bot
- Set trade amount, max open positions, paper mode, on/off from bot
- 3 Take-Profit levels (default +5% / +10% / +15%) with partial close
- When TP1 hits → move Stop Loss to Entry (breakeven trailing)
- Stop Loss is taken from the signal itself
- Add / remove signal channels from the bot
- All settings + open positions + trade history saved in SQLite
- SPOT only – no futures, no leverage, no short
- Reads public channels via Web Preview only (no API ID/Hash)

## Railway Notes
- Filesystem is ephemeral → attach a **Volume** and set `DATA_DIR=/data`
- Or the bot will create `./data/trader.db` (lost on redeploy)

## Quick Start
1. Upload files to GitHub
2. Deploy on Railway
3. Set env vars
4. Talk to bot → use the buttons
5. Start with Paper Mode ON and small amount
