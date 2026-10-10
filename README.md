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