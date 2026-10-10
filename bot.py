from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx
import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, Numeric, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("coinsells")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(v.strip()) for v in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if v.strip().isdigit()}
DEPOSIT_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "").strip()
PUBLIC_CHANNEL_USERNAME = os.getenv("PUBLIC_CHANNEL_USERNAME", "@coinsells").strip()
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj").strip()
TRON_API_BASE = os.getenv("TRON_API_BASE", "https://api.trongrid.io").rstrip("/")
TRON_API_KEY = os.getenv("TRON_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()
USD_PER_USDT = Decimal(os.getenv("USD_PER_USDT", "1.0"))
FEE_PERCENT = Decimal(os.getenv("EXCHANGE_FEE_PERCENT", "1.0"))
MIN_USDT = Decimal(os.getenv("MIN_USDT", "1000"))
MAX_USDT = Decimal(os.getenv("MAX_USDT", "10000000"))
ENCRYPTION_KEY = os.getenv("BANK_ENCRYPTION_KEY", "").strip()

TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
USDT_DECIMALS = Decimal("1000000")
CENT = Decimal("0.01")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Configure it in your local .env.")
if not ADMIN_IDS:
    raise RuntimeError("TELEGRAM_ADMIN_IDS is missing. Add at least one numeric Telegram user ID.")
if not DEPOSIT_ADDRESS:
    raise RuntimeError("USDT_TRC20_ADDRESS is missing. Set the verified receiving wallet address.")
if not DATABASE_URL.startswith("postgresql+asyncpg://"):
    raise RuntimeError("DATABASE_URL must be a PostgreSQL URL beginning postgresql+asyncpg://.")
if not ENCRYPTION_KEY:
    raise RuntimeError("BANK_ENCRYPTION_KEY is missing. Generate a Fernet key and store it in .env.")
if not Decimal("0") <= FEE_PERCENT < Decimal("100"):
    raise RuntimeError("EXCHANGE_FEE_PERCENT must be at least 0 and less than 100.")
if USD_PER_USDT <= 0 or MIN_USDT <= 0 or MAX_USDT < MIN_USDT:
    raise RuntimeError("Check USD_PER_USDT, MIN_USDT, and MAX_USDT.")

try:
    cipher = Fernet(ENCRYPTION_KEY.encode())
except (ValueError, TypeError) as exc:
    raise RuntimeError("BANK_ENCRYPTION_KEY is not a valid Fernet key.") from exc

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class ExchangeOrder(Base):
    __tablename__ = "exchange_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_chat_id: Mapped[int] = mapped_column(Integer)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    payout_usd: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    txid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    bank_details_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="awaiting_bank", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OrderFlow(StatesGroup):
    amount = State()
    txid = State()
    bank_details = State()


router = Router()


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def usdt_units(value: Decimal) -> int:
    return int((value * USDT_DECIMALS).to_integral_value(rounding=ROUND_HALF_UP))


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Create exchange order", callback_data="order:new")]
    ])


def order_admin_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="View payout details", callback_data=f"order:view:{order_id}")],
        [
            InlineKeyboardButton(text="Approve for USD payout", callback_data=f"order:approve:{order_id}"),
            InlineKeyboardButton(text="Reject", callback_data=f"order:reject:{order_id}"),
        ],
    ])


def payout_admin_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Mark USD payout sent", callback_data=f"order:paid:{order_id}")]
    ])


async def verify_usdt_transfer(txid: str, expected_amount: Decimal) -> tuple[bool, str]:
    """Check a confirmed USDT TRC20 Transfer event to the configured address."""
    headers = {"TRON-PRO-API-KEY": TRON_API_KEY} if TRON_API_KEY else {}
    url = f"{TRON_API_BASE}/v1/transactions/{txid}/events"
    params = {"only_confirmed": "true", "event_name": "Transfer", "limit": 200}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("TRON API check failed for %s: %s", txid, exc)
        return False, "The blockchain service is temporarily unavailable. Please try again shortly."

    events = payload.get("data") or []
    if not events:
        return False, "No confirmed transfer event was found yet. Check the TXID and try again later."
    expected_units = usdt_units(expected_amount)
    destination_seen = False
    for event in events:
        contract = str(event.get("contract_address", ""))
        result = event.get("result") or {}
        recipient = str(result.get("to", ""))
        if contract != USDT_CONTRACT or recipient != DEPOSIT_ADDRESS:
            continue
        destination_seen = True
        try:
            actual_units = int(str(result.get("value", "0")))
        except ValueError:
            continue
        if actual_units == expected_units:
            return True, "Confirmed USDT TRC20 transfer found."
    if destination_seen:
        return False, "A transfer to the wallet was found, but its amount does not exactly match your order."
    return False, "This TXID does not show a confirmed USDT TRC20 transfer to the CoinSells wallet."


async def get_order(session: AsyncSession, order_id: int) -> ExchangeOrder | None:
    return await session.get(ExchangeOrder, order_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Welcome to CoinSells.\n\n"
        "Exchange USDT TRC20 for a manual USD bank payout.\n"
        f"Order limits: {money(MIN_USDT)}–{money(MAX_USDT)} USDT.\n"
        f"Fee: {FEE_PERCENT}% (payout estimate uses {USD_PER_USDT} USD per USDT).\n\n"
        "Payouts are reviewed by an administrator and are not automatic. "
        "Never send a seed phrase, wallet private key, card PIN, or CVV.",
        reply_markup=start_keyboard(),
    )


@router.callback_query(F.data == "order:new")
async def new_order(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(OrderFlow.amount)
    await callback.message.answer(
        f"Enter the USDT amount to exchange (from {money(MIN_USDT)} to {money(MAX_USDT)}). "
        "Use up to 6 decimal places."
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Use /start when you want to begin again.")


@router.message(OrderFlow.amount)
async def receive_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        await message.answer("Please enter a valid number, for example: 1000")
        return
    if not amount.is_finite() or amount < MIN_USDT or amount > MAX_USDT:
        await message.answer(f"Amount must be between {money(MIN_USDT)} and {money(MAX_USDT)} USDT.")
        return
    if amount.as_tuple().exponent < -6:
        await message.answer("USDT supports up to 6 decimal places. Please enter the amount again.")
        return

    payout = (amount * (Decimal("1") - FEE_PERCENT / Decimal("100")) * USD_PER_USDT).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    await state.update_data(amount=str(amount), payout=str(payout))
    await state.set_state(OrderFlow.txid)

    qr = qrcode.make(DEPOSIT_ADDRESS)
    qr_buffer = io.BytesIO()
    qr.save(qr_buffer, format="PNG")
    await message.answer_photo(
        BufferedInputFile(qr_buffer.getvalue(), filename="coinsells-usdt-trc20.png"),
        caption=(
            "Send the exact amount using the TRON (TRC20) network only.\n\n"
            f"Amount: {amount:f} USDT\n"
            f"Receiving address:\n<code>{DEPOSIT_ADDRESS}</code>\n\n"
            f"Estimated payout after {FEE_PERCENT}% fee: ${money(payout)} USD. "
            "The final payout is subject to administrator review and the configured rate.\n\n"
            "After sending, paste the transaction ID (TXID). Do not send funds on another network."
        ),
        parse_mode="HTML",
    )
    await message.answer("Paste the 64-character transaction ID (TXID) after the transfer is confirmed on-chain.")


@router.message(OrderFlow.txid)
async def receive_txid(message: Message, state: FSMContext) -> None:
    txid = (message.text or "").strip()
    if not TXID_RE.fullmatch(txid):
        await message.answer("That TXID format is invalid. Send the 64-character hexadecimal transaction ID.")
        return
    async with Session() as session:
        existing = await session.scalar(select(ExchangeOrder.id).where(ExchangeOrder.txid == txid))
        if existing:
            await message.answer("This TXID has already been used for an order. Contact support if you think this is an error.")
            return

    data = await state.get_data()
    amount = Decimal(data["amount"])
    payout = Decimal(data["payout"])
    await message.answer("Checking the confirmed transaction on the TRON network. Please wait…")
    ok, reason = await verify_usdt_transfer(txid, amount)
    if not ok:
        await message.answer(reason + "\nYou can submit the TXID again or use /cancel.")
        return

    async with Session() as session:
        order = ExchangeOrder(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            amount_usdt=amount,
            payout_usd=payout,
            txid=txid,
            status="awaiting_bank",
        )
        session.add(order)
        try:
            await session.commit()
            await session.refresh(order)
        except IntegrityError:
            await session.rollback()
            await message.answer("This TXID has already been registered. Contact support if needed.")
            return
        order_id = order.id

    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.bank_details)
    await message.answer(
        f"On-chain transfer confirmed. Order #{order_id} created.\n\n"
        "Now send the bank details needed to receive USD (for example: account holder name, bank name, "
        "routing number and account number, as applicable to your bank). Send only information necessary "
        "for a transfer. Never send your online banking password, card PIN, CVV, or one-time codes.\n\n"
        "Your details will be encrypted in the bot database and shown only to configured administrators. "
        "Note: Telegram chat messages themselves are not end-to-end encrypted in a bot chat."
    )


@router.message(OrderFlow.bank_details)
async def receive_bank_details(message: Message, state: FSMContext, bot: Bot) -> None:
    details = (message.text or "").strip()
    if len(details) < 8 or len(details) > 2000:
        await message.answer("Please send the necessary bank transfer details in one message (8–2000 characters).")
        return
    if not message.from_user:
        return
    data = await state.get_data()
    order_id = int(data["order_id"])
    encrypted = cipher.encrypt(details.encode("utf-8")).decode("ascii")

    async with Session() as session:
        order = await get_order(session, order_id)
        if not order or order.telegram_user_id != message.from_user.id or order.status != "awaiting_bank":
            await state.clear()
            await message.answer("I couldn't match this step to an active order. Use /start to begin again.")
            return
        order.bank_details_encrypted = encrypted
        order.status = "awaiting_admin_review"
        await session.commit()
        amount, payout, txid = order.amount_usdt, order.payout_usd, order.txid

    await state.clear()
    await message.answer(
        f"Order #{order_id} submitted for administrator review.\n"
        f"Deposit: {amount:f} USDT\nEstimated USD payout: ${money(payout)}\n"
        "Please wait for the administrator to review your order and arrange the bank payout."
    )
    admin_text = (
        f"CoinSells order #{order_id}\n"
        "Status: awaiting admin review\n"
        f"Customer Telegram ID: {message.from_user.id}\n"
        f"Deposit: {amount:f} USDT (confirmed on-chain)\n"
        f"Estimated payout: ${money(payout)} USD\n"
        f"TXID: {txid}\n"
        "Use View payout details to review the encrypted bank details."
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=order_admin_keyboard(order_id))
        except Exception:
            log.exception("Failed to notify admin %s about order %s", admin_id, order_id)


@router.message(Command("channelcheck"))
async def channel_check(message: Message, bot: Bot) -> None:
    """Check whether the bot can access the configured public channel and post there."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is for administrators only.")
        return
    if not PUBLIC_CHANNEL_USERNAME:
        await message.answer("PUBLIC_CHANNEL_USERNAME is not configured.")
        return
    try:
        chat = await bot.get_chat(PUBLIC_CHANNEL_USERNAME)
        member = await bot.get_chat_member(chat.id, bot.id)
        status = getattr(member, "status", "unknown")
        can_post = getattr(member, "can_post_messages", None)
        await message.answer(
            "Public channel connection check:\n"
            f"Channel: {chat.title}\n"
            f"Username: {PUBLIC_CHANNEL_USERNAME}\n"
            f"Channel ID: {chat.id}\n"
            f"Bot status: {status}\n"
            f"Can post messages: {can_post if can_post is not None else 'Check admin permissions in Telegram'}\n\n"
            "If the bot is not an administrator, add it as a channel administrator and allow posting messages."
        )
    except Exception:
        log.exception("Public channel check failed for %s", PUBLIC_CHANNEL_USERNAME)
        await message.answer(
            "Could not access the configured channel. Confirm the username is correct and that the bot "
            "has been added to the channel. No message was published."
        )


@router.message(Command("orders"))
async def list_orders(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("This command is for administrators only.")
        return
    async with Session() as session:
        rows = (await session.scalars(select(ExchangeOrder).order_by(ExchangeOrder.id.desc()).limit(10))).all()
    if not rows:
        await message.answer("No orders yet.")
        return
    lines = ["Recent CoinSells orders:"]
    for order in rows:
        lines.append(f"#{order.id} — {order.amount_usdt:f} USDT → ${money(order.payout_usd)} — {order.status}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("order:"))
async def handle_admin_action(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Not authorized.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Invalid action.", show_alert=True)
        return
    action, order_id = parts[1], int(parts[2])

    async with Session() as session:
        order = await get_order(session, order_id)
        if not order:
            await callback.answer("Order not found.", show_alert=True)
            return
        if action == "view":
            if not order.bank_details_encrypted:
                await callback.answer("Payout details have not been submitted.", show_alert=True)
                return
            try:
                bank_details = cipher.decrypt(order.bank_details_encrypted.encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                await callback.answer("Could not decrypt payout details.", show_alert=True)
                return
            await callback.answer()
            await bot.send_message(
                callback.from_user.id,
                f"Private payout details for CoinSells order #{order.id}:\n\n{bank_details}\n\n"
                f"Deposit: {order.amount_usdt:f} USDT\nEstimated payout: ${money(order.payout_usd)} USD\n"
                f"Status: {order.status}"
            )
            return
        if action == "approve":
            if order.status != "awaiting_admin_review":
                await callback.answer(f"Cannot approve order in status: {order.status}", show_alert=True)
                return
            order.status = "approved_for_payout"
            await session.commit()
            user_chat_id, payout = order.telegram_chat_id, order.payout_usd
        elif action == "reject":
            if order.status not in {"awaiting_admin_review", "approved_for_payout"}:
                await callback.answer(f"Cannot reject order in status: {order.status}", show_alert=True)
                return
            order.status = "rejected"
            await session.commit()
            user_chat_id, payout = order.telegram_chat_id, order.payout_usd
        elif action == "paid":
            if order.status != "approved_for_payout":
                await callback.answer("Approve the order before marking payout as sent.", show_alert=True)
                return
            order.status = "payout_sent"
            await session.commit()
            user_chat_id, payout = order.telegram_chat_id, order.payout_usd
        else:
            await callback.answer("Unknown action.", show_alert=True)
            return

    await callback.answer("Order updated.")
    if action == "approve":
        await bot.send_message(
            user_chat_id,
            f"Order #{order_id} has passed administrator review. The USD bank payout is being handled manually. "
            f"Expected amount: ${money(payout)} USD."
        )
        await bot.send_message(
            callback.from_user.id,
            f"Order #{order_id} approved for manual USD payout. After you actually send the USD, press the button below.",
            reply_markup=payout_admin_keyboard(order_id),
        )
    elif action == "reject":
        await bot.send_message(
            user_chat_id,
            f"Order #{order_id} was not approved. Please contact CoinSells support before making another transfer."
        )
        await bot.send_message(callback.from_user.id, f"Order #{order_id} rejected.")
    elif action == "paid":
        await bot.send_message(
            user_chat_id,
            f"CoinSells marked the USD payout for order #{order_id} as sent. Please contact support if the funds do not arrive."
        )
        await bot.send_message(callback.from_user.id, f"Order #{order_id} marked as payout sent.")


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    storage = MemoryStorage()
    if REDIS_URL:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(REDIS_URL)
            log.info("Using Redis FSM storage.")
        except Exception:
            log.exception("Could not initialize Redis storage; falling back to in-memory state.")
            storage = MemoryStorage()
    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    log.info("CoinSells bot starting.")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
