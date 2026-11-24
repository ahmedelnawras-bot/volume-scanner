# Spot ladder bot (buy only)

Separate Railway service. Root directory: `spot_ladder`. Start: `python spot_ladder.py`.
Full rules and settings: see the docstring at the top of `spot_ladder.py`.

Railway variables (set by Nour in the Railway dashboard, never in git):
- `LADDER_TELEGRAM_BOT_TOKEN`, `LADDER_TELEGRAM_CHAT_ID`  - the new Telegram bot
- `REDIS_URL`  - reference to the Railway Redis service (state survives restarts)
- `LADDER_LIVE=false`  - paper until decided otherwise
- live only: `LADDER_OKX_API_KEY`, `LADDER_OKX_API_SECRET`, `LADDER_OKX_API_PASSPHRASE`
  (key created inside the OKX sub-account `spoot`, Read + Trade, no Withdraw)
