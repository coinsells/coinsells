# CoinSells

Telegram bot for a semi-automatic USDT TRC20 to USD bank-payout workflow.

## Current scope
- Collect exchange orders within configured limits.
- Verify USDT TRC20 transfer details before collecting payout details.
- Encrypt bank payout details at rest.
- Send orders to configured Telegram administrators for manual USD payout review.
- Never automatically send fiat payouts.

## Important
This is an implementation scaffold and must be configured, tested, and reviewed before handling customer funds. Set a valid TRON API endpoint/API key as needed, confirm the correct receiving address, and review all local legal, licensing, KYC/AML, sanctions, privacy, and consumer-protection requirements before serving customers.

## Setup
See `.env.example` and the setup steps below.

1. Install Python 3.12+.
2. Create a virtual environment and install `requirements.txt`.
3. Copy `.env.example` to `.env` and fill in values locally. Never commit `.env` or share bot tokens in chat.
4. Generate a Fernet key using `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and put it in `BANK_ENCRYPTION_KEY`.
5. Configure a PostgreSQL database in `DATABASE_URL`.
6. Run `python bot.py`.

The bot will not start if required secrets/configuration are missing. Blockchain verification depends on the configured TRON API returning confirmed USDT TRC20 Transfer events.

## Telegram administrator setup
1. Add the numeric Telegram user ID of each trusted administrator to `TELEGRAM_ADMIN_IDS`, separated by commas.
2. Each administrator must open the bot in a private chat and press **Start** before the bot can DM order notifications.
3. Keep the receiving wallet under your own control. Confirm the address and network independently before enabling customer deposits.

## Payment verification behavior
The customer submits a TXID. The bot asks the TRON API for confirmed USDT TRC20 Transfer events and accepts only an exact-amount transfer to the configured receiving address using the configured token contract. If the API cannot verify it, the bot does not mark the payment confirmed. This version does **not** discover deposits without a customer-submitted TXID, and it never sends USD automatically.

## Run locally
```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Before running, edit `.env` and provide a PostgreSQL database URL, bot token, administrator IDs, receiving address, and a newly generated encryption key. On Windows, copy `.env.example` to `.env` manually if `cp` is unavailable. For production, use persistent Redis storage, backups, access controls, monitoring, and a security review; the in-memory FSM fallback loses active conversation state on restart.
