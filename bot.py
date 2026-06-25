import asyncio
import html
import logging
from contextlib import suppress
from datetime import UTC, datetime

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    MAX_ACTIVE_ORDERS_PER_USER,
    MIN_WITHDRAW_BDT,
    OTP_CHECK_INTERVAL,
    OTP_REWARD_BDT,
    WITHDRAW_NETWORK_LABEL,
)
from db import Database
from utility import (
    ProviderAPIError,
    allocate_number,
    build_admin_menu,
    build_admin_settings,
    build_custom_range_prompt_keyboard,
    build_custom_ranges_keyboard,
    build_home_keyboard,
    build_help_keyboard,
    build_leaderboard_keyboard,
    build_profile_keyboard,
    build_cancel_keyboard,
    build_wallet_keyboard,
    build_withdrawal_review_keyboard,
    build_order_actions,
    build_orders_keyboard,
    build_platforms_keyboard,
    build_range_services_keyboard,
    build_service_regions_keyboard,
    extract_otp,
    fetch_otps,
    format_admin_stats,
    format_currency,
    format_dashboard,
    format_help,
    format_iso,
    format_leaderboard,
    format_order_card,
    format_withdrawal_admin,
    format_withdrawal_user,
    format_profile,
    format_wallet,
    format_unix_ms,
    get_catalog,
    get_range_services,
    get_service_regions,
    get_services,
    human_status,
    normalize_digits,
    pick_service_for_range,
    pick_rid_for_region_service,
    search_custom_ranges,
)
from range_notifier import start_range_notifier


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("otp-bot")

router = Router()
db = Database()
http_session: aiohttp.ClientSession | None = None
otp_worker_task: asyncio.Task | None = None
range_notifier_task: asyncio.Task | None = None


class AdminState(StatesGroup):
    waiting_balance = State()
    waiting_timeout = State()
    waiting_ban = State()
    waiting_unban = State()


class BuyState(StatesGroup):
    waiting_custom_range = State()


class WalletState(StatesGroup):
    waiting_address = State()
    waiting_withdraw_amount = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def get_session() -> aiohttp.ClientSession:
    if http_session is None:
        raise RuntimeError("HTTP session is not ready.")
    return http_session


async def ensure_user_record(message_user) -> bool:
    await db.ensure_user(message_user.id, message_user.username)
    if await db.is_banned(message_user.id):
        return False
    return True


async def ensure_callback_user(callback: CallbackQuery) -> bool:
    allowed = await ensure_user_record(callback.from_user)
    if not allowed:
        await callback.answer("Your access to this bot has been blocked.", show_alert=True)
        return False
    return True


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        logger.exception("Failed to edit message; falling back to safe plain text send.")
        fallback_text = html.escape(text)
        await callback.message.answer(
            f"<pre>{fallback_text}</pre>",
            reply_markup=reply_markup,
        )


async def show_home(target: Message | CallbackQuery) -> None:
    user = target.from_user
    allowed = await ensure_user_record(user)
    if not allowed:
        text = "🚫 Your access to this bot has been blocked."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return

    db_user = await db.get_user(user.id)
    stats = await db.user_stats(user.id)
    settings = await db.get_settings()
    text = format_dashboard(db_user, stats, settings)
    markup = build_home_keyboard(is_admin(user.id))
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def send_wallet_view(target: Message | CallbackQuery) -> None:
    user = target.from_user
    allowed = await ensure_user_record(user)
    if not allowed:
        text = "🚫 Your access to this bot has been blocked."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    balance = await db.get_balance(user.id)
    wallet_address = await db.get_wallet_address(user.id)
    pending_withdrawal = await db.get_pending_withdrawal_for_user(user.id)
    text = format_wallet(balance, wallet_address, pending_withdrawal)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.answer(text, reply_markup=build_wallet_keyboard())
    else:
        await target.answer(text, reply_markup=build_wallet_keyboard())


async def show_help(target: CallbackQuery) -> None:
    if not await ensure_callback_user(target):
        return
    await target.answer()
    await safe_edit(target, format_help(), build_help_keyboard())


async def show_profile(target: Message | CallbackQuery) -> None:
    user = target.from_user
    allowed = await ensure_user_record(user)
    if not allowed:
        text = "🚫 Your access to this bot has been blocked."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    db_user = await db.get_user(user.id)
    stats = await db.user_stats(user.id)
    balance = await db.get_balance(user.id)
    text = format_profile(db_user, stats, balance)
    if is_admin(user.id):
        admin_stats = await db.admin_stats()
        text = f"{text}\n\n{format_admin_stats(admin_stats)}"
    markup = build_profile_keyboard()
    if isinstance(target, CallbackQuery):
        await target.answer()
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def show_leaderboard(target: Message | CallbackQuery) -> None:
    user = target.from_user
    allowed = await ensure_user_record(user)
    if not allowed:
        text = "🚫 Your access to this bot has been blocked."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    entries = await db.top_users_by_otps(limit=5)
    text = format_leaderboard(entries, current_user_id=user.id)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.answer(text, reply_markup=build_leaderboard_keyboard())
    else:
        await target.answer(text, reply_markup=build_leaderboard_keyboard())


async def show_orders(target: CallbackQuery, kind: str = "active") -> None:
    if not await ensure_callback_user(target):
        return
    await target.answer()
    orders = await db.list_user_orders(target.from_user.id, kind=kind)
    if not orders:
        text = f"📦 <b>{kind.title()} Orders</b>\n\nNo {kind} orders found yet."
    else:
        lines = [f"📦 <b>{kind.title()} Orders</b>\n"]
        for order in orders:
            lines.append(
                f"#{order['order_id']} • {order['service']} • {human_status(order['status'])} • {format_iso(order['created_at'])}"
            )
        text = "\n".join(lines)
    await target.message.answer(text, reply_markup=build_orders_keyboard(orders, kind))


async def show_single_order(target: CallbackQuery, order_id: int) -> None:
    if not await ensure_callback_user(target):
        return
    order = await db.get_order_for_user(order_id, target.from_user.id, is_admin(target.from_user.id))
    if not order:
        await target.answer("Order not found.", show_alert=True)
        return
    await target.answer()
    reveal_otp = order["status"] != "otp_locked"
    await safe_edit(target, format_order_card(order, reveal_otp=reveal_otp), build_order_actions(order))


async def show_platforms(target: CallbackQuery, page: int = 0, force_refresh: bool = False) -> None:
    if not await ensure_callback_user(target):
        return
    session = await get_session()
    try:
        if force_refresh:
            await get_catalog(session, force_refresh=True)
        services = await get_services(session)
    except ProviderAPIError as error:
        await target.answer(str(error), show_alert=True)
        return
    if not services:
        await target.answer()
        await safe_edit(target, "No live platforms are available right now.", build_home_keyboard(is_admin(target.from_user.id)))
        return
    await target.answer()
    text = (
        "📱 <b>Select Platform</b>\n\n"
        "Choose the platform first, then we will show the live regions available for that platform."
    )
    await safe_edit(target, text, build_platforms_keyboard(services, page=page))


async def show_service_regions(target: CallbackQuery, service_token_value: str, page: int = 0) -> None:
    if not await ensure_callback_user(target):
        return
    session = await get_session()
    service, regions = await get_service_regions(session, service_token_value)
    if not service or not regions:
        await target.answer("This platform is not available anymore. Please refresh.", show_alert=True)
        return
    await target.answer()
    text = (
        f"{html.escape(service['name'])} <b>Platform</b>\n\n"
        "Select the live region you want to use for this platform."
    )
    await safe_edit(target, text, build_service_regions_keyboard(service, regions, page=page))


async def prompt_custom_range(target: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(target):
        return
    await state.set_state(BuyState.waiting_custom_range)
    await target.answer()
    text = (
        "🔎 <b>Custom Range Search</b>\n\n"
        "Send a numeric range prefix such as:\n"
        "<code>255</code>\n"
        "<code>22507</code>\n"
        "<code>26134</code>\n\n"
        "I will search the live provider ranges and show the matches as inline buttons."
    )
    await safe_edit(target, text, build_custom_range_prompt_keyboard())


async def show_custom_range_results(
    target: Message | CallbackQuery, query: str, page: int = 0
) -> None:
    session = await get_session()
    matches = await search_custom_ranges(session, query)
    if not matches:
        text = (
            "🔎 <b>Custom Range Search</b>\n\n"
            f"No live ranges matched <code>{html.escape(query)}</code>.\n"
            "Try a shorter or different numeric prefix."
        )
        if isinstance(target, CallbackQuery):
            await target.answer("No matches found.", show_alert=True)
            await safe_edit(target, text, build_custom_range_prompt_keyboard())
        else:
            await target.answer(text, reply_markup=build_custom_range_prompt_keyboard())
        return

    text = (
        "🔎 <b>Matching Ranges</b>\n\n"
        f"Query: <code>{html.escape(query)}</code>\n"
        f"Matches Found: <b>{len(matches)}</b>\n\n"
        "Choose a live range to continue."
    )
    markup = build_custom_ranges_keyboard(matches, query, page=page)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def show_range_services(target: CallbackQuery, rid: str, page: int = 0) -> None:
    if not await ensure_callback_user(target):
        return
    session = await get_session()
    range_entry, services = await get_range_services(session, rid)
    if not range_entry or not services:
        await target.answer("This custom range is no longer available. Search again.", show_alert=True)
        return
    await target.answer()
    text = (
        "🔢 <b>Custom Range Selected</b>\n\n"
        f"🌍 Region: <b>{html.escape(range_entry['region_name'])}</b>\n"
        f"📶 Range: <code>{range_entry['rid']}</code>\n\n"
        "Select the service you want for this exact live range."
    )
    await safe_edit(target, text, build_range_services_keyboard(range_entry, services, page=page))


async def allocate_for_user(target: CallbackQuery, service_token_value: str, region_code: str) -> None:
    if not await ensure_callback_user(target):
        return
    user_id = target.from_user.id
    if await db.count_active_orders(user_id) >= MAX_ACTIVE_ORDERS_PER_USER:
        await target.answer(
            f"Active order limit reached. Max allowed is {MAX_ACTIVE_ORDERS_PER_USER}.",
            show_alert=True,
        )
        return

    session = await get_session()
    try:
        service, region, rid = await pick_rid_for_region_service(session, service_token_value, region_code)
        allocated = await allocate_number(session, rid)
    except ProviderAPIError as error:
        await target.answer(str(error), show_alert=True)
        return

    number = normalize_digits(allocated.get("no_plus_number") or allocated.get("full_number") or "")
    if not number:
        await target.answer("Provider returned an invalid number.", show_alert=True)
        return

    order = await db.create_order(
        user_id=user_id,
        number=number,
        service=service["name"],
        region=region["name"],
        rid=rid,
        price=0.0,
    )
    text = (
        "📞 <b>Number Allocated</b>\n\n"
        f"🌍 Region: <b>{html.escape(region['name'])}</b>\n"
        f"🔹 Service: <b>{html.escape(service['name'])}</b>\n"
        f"📞 Number: <code>+{number}</code>\n"
        f"📌 Status: <b>Waiting for OTP</b>\n"
        f"🎁 Reward After OTP: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
    )
    await safe_edit(target, text, build_order_actions(order))


async def allocate_for_custom_range(target: CallbackQuery, rid: str, token: str) -> None:
    if not await ensure_callback_user(target):
        return
    user_id = target.from_user.id
    if await db.count_active_orders(user_id) >= MAX_ACTIVE_ORDERS_PER_USER:
        await target.answer(
            f"Active order limit reached. Max allowed is {MAX_ACTIVE_ORDERS_PER_USER}.",
            show_alert=True,
        )
        return

    session = await get_session()
    try:
        range_entry, service = await pick_service_for_range(session, rid, token)
        allocated = await allocate_number(session, rid)
    except ProviderAPIError as error:
        await target.answer(str(error), show_alert=True)
        return

    number = normalize_digits(allocated.get("no_plus_number") or allocated.get("full_number") or "")
    if not number:
        await target.answer("Provider returned an invalid number.", show_alert=True)
        return

    order = await db.create_order(
        user_id=user_id,
        number=number,
        service=service["name"],
        region=range_entry["region_name"],
        rid=rid,
        price=0.0,
    )
    text = (
        "📞 <b>Number Allocated</b>\n\n"
        f"🌍 Region: <b>{html.escape(range_entry['region_name'])}</b>\n"
        f"📶 Range: <code>{rid}</code>\n"
        f"🔹 Service: <b>{html.escape(service['name'])}</b>\n"
        f"📞 Number: <code>+{number}</code>\n"
        f"📌 Status: <b>Waiting for OTP</b>\n"
        f"🎁 Reward After OTP: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
    )
    await target.answer()
    await safe_edit(target, text, build_order_actions(order))


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await show_home(message)


@router.message(F.text == "🏠 Home")
async def home_text_handler(message: Message) -> None:
    await show_home(message)


@router.message(F.text == "📞 Get Number")
async def buy_text_handler(message: Message) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    session = await get_session()
    try:
        services = await get_services(session)
    except ProviderAPIError as error:
        await message.answer(str(error), reply_markup=build_home_keyboard(is_admin(message.from_user.id)))
        return
    if not services:
        await message.answer(
            "No live platforms are available right now.",
            reply_markup=build_home_keyboard(is_admin(message.from_user.id)),
        )
        return
    text = (
        "📱 <b>Select Platform</b>\n\n"
        "Choose the platform first, then select a live region for that platform."
    )
    await message.answer(text, reply_markup=build_platforms_keyboard(services, page=0))


@router.message(F.text == "🔎 Custom Range")
async def custom_range_text_handler(message: Message, state: FSMContext) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    await state.set_state(BuyState.waiting_custom_range)
    text = (
        "🔎 <b>Custom Range Search</b>\n\n"
        "Send a numeric range prefix such as:\n"
        "<code>255</code>\n"
        "<code>22507</code>\n"
        "<code>26134</code>\n\n"
        "I will search the live provider ranges and show the matches as inline buttons."
    )
    msg = await message.answer(text, reply_markup=build_custom_range_prompt_keyboard())
    await state.update_data(prompt_msg_id=msg.message_id)


@router.message(F.text == "💰 Wallet")
async def wallet_text_handler(message: Message) -> None:
    await send_wallet_view(message)


@router.message(F.text == "👤 Profile")
async def profile_text_handler(message: Message) -> None:
    await show_profile(message)


@router.message(F.text == "🏆 Leaderboard")
async def leaderboard_text_handler(message: Message) -> None:
    await show_leaderboard(message)


@router.message(F.text == "ℹ️ Help")
async def help_text_handler(message: Message) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    await message.answer(
        format_help(),
        reply_markup=build_help_keyboard(),
    )


@router.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("This command is only available to admins.")
        return
    settings = await db.get_settings()
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"OTP Reward: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
        f"Minimum Withdraw: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>\n"
        f"Network: <b>{WITHDRAW_NETWORK_LABEL}</b>"
    )
    await message.answer(text, reply_markup=build_admin_menu())


@router.message(F.text == "🛠 Admin")
async def admin_text_handler(message: Message) -> None:
    await admin_handler(message)


@router.message(Command("addbalance"))
async def add_balance_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("This command is only available to admins.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: <code>/addbalance USER_ID AMOUNT</code>")
        return
    try:
        user_id = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await message.answer("USER_ID must be an integer and AMOUNT must be a number.")
        return
    await db.ensure_user(user_id, None)
    balance = await db.add_balance(user_id, amount)
    await message.answer(
        f"Balance updated for <code>{user_id}</code>.\nNew balance: <b>{format_currency(balance)}</b>"
    )


@router.callback_query(F.data == "nav:home")
async def home_callback(callback: CallbackQuery) -> None:
    await show_home(callback)


@router.callback_query(F.data == "nav:wallet")
async def wallet_callback(callback: CallbackQuery) -> None:
    await send_wallet_view(callback)


@router.callback_query(F.data == "wallet:change_address")
async def wallet_change_address_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    await callback.answer()
    await state.set_state(WalletState.waiting_address)
    await state.update_data(next_action="wallet")
    await safe_edit(callback,
        f"Send your {WITHDRAW_NETWORK_LABEL} address now.",
        reply_markup=build_cancel_keyboard(),
    )


@router.callback_query(F.data == "wallet:withdraw")
async def wallet_withdraw_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    balance = await db.get_balance(callback.from_user.id)
    address = await db.get_wallet_address(callback.from_user.id)
    pending = await db.get_pending_withdrawal_for_user(callback.from_user.id)
    if pending:
        await callback.answer("You already have a pending withdrawal request.", show_alert=True)
        return
    if not address:
        await callback.answer()
        await state.set_state(WalletState.waiting_address)
        await state.update_data(next_action="withdraw")
        await safe_edit(callback,
            f"Before withdrawing, send your {WITHDRAW_NETWORK_LABEL} address.",
            reply_markup=build_cancel_keyboard(),
        )
        return
    if balance < MIN_WITHDRAW_BDT:
        await callback.answer(
            f"You need at least {format_currency(MIN_WITHDRAW_BDT)} to withdraw.",
            show_alert=True,
        )
        return
    await callback.answer()
    await state.set_state(WalletState.waiting_withdraw_amount)
    await safe_edit(callback,
        f"Send the amount you want to withdraw in BDT.\nMinimum: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>",
        reply_markup=build_cancel_keyboard(),
    )


@router.callback_query(F.data == "nav:help")
async def help_callback(callback: CallbackQuery) -> None:
    await show_help(callback)


@router.callback_query(F.data == "nav:leaderboard")
async def leaderboard_callback(callback: CallbackQuery) -> None:
    await show_leaderboard(callback)


@router.callback_query(F.data == "nav:orders")
@router.callback_query(F.data == "nav:orders:active")
async def orders_callback(callback: CallbackQuery) -> None:
    await show_orders(callback, "active")


@router.callback_query(F.data == "nav:orders:completed")
async def completed_orders_callback(callback: CallbackQuery) -> None:
    await show_orders(callback, "completed")


@router.callback_query(F.data == "buy:start")
async def buy_start_callback(callback: CallbackQuery) -> None:
    await show_platforms(callback)


@router.callback_query(F.data.startswith("buy:platforms:"))
async def buy_platforms_page_callback(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    await show_platforms(callback, page=page)


@router.callback_query(F.data == "buy:platforms_refresh")
async def buy_platforms_refresh_callback(callback: CallbackQuery) -> None:
    await show_platforms(callback, force_refresh=True)


@router.callback_query(F.data == "buy:custom")
async def buy_custom_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await prompt_custom_range(callback, state)


@router.callback_query(F.data.startswith("buy:ranges:"))
async def buy_custom_ranges_page_callback(callback: CallbackQuery) -> None:
    _, _, query, page = callback.data.split(":")
    await show_custom_range_results(callback, query, page=int(page))


@router.callback_query(F.data.startswith("buy:platform:"))
async def buy_platform_callback(callback: CallbackQuery) -> None:
    _, _, token, page = callback.data.split(":")
    await show_service_regions(callback, token, page=int(page))


@router.callback_query(F.data.startswith("buy:range:"))
async def buy_range_callback(callback: CallbackQuery) -> None:
    _, _, rid, page = callback.data.split(":")
    await show_range_services(callback, rid, page=int(page))


@router.callback_query(F.data.startswith("buy:service_region:"))
async def buy_service_region_callback(callback: CallbackQuery) -> None:
    _, _, token, region_code, _page = callback.data.split(":")
    await allocate_for_user(callback, token, region_code)


@router.callback_query(F.data.startswith("buy:range_service:"))
async def buy_range_service_callback(callback: CallbackQuery) -> None:
    _, _, rid, token, _page = callback.data.split(":")
    await allocate_for_custom_range(callback, rid, token)


@router.callback_query(F.data.startswith("order:view:"))
async def order_view_callback(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[-1])
    await show_single_order(callback, order_id)


@router.callback_query(F.data.startswith("order:unlock:"))
async def order_unlock_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    await callback.answer("OTP unlock is no longer used. Rewards are credited automatically.", show_alert=True)


@router.callback_query(F.data.startswith("order:cancel:"))
async def order_cancel_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    order_id = int(callback.data.split(":")[-1])
    order = await db.get_order_for_user(order_id, callback.from_user.id, is_admin(callback.from_user.id))
    if not order:
        await callback.answer("Order not found.", show_alert=True)
        return
    if not await db.cancel_order(order_id):
        await callback.answer("Unable to cancel this order.", show_alert=True)
        return
    await callback.answer()
    updated = await db.get_order(order_id)
    await safe_edit(callback, format_order_card(updated, reveal_otp=False), build_order_actions(updated))


@router.callback_query(F.data.startswith("order:samerange:"))
async def order_samerange_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    user_id = callback.from_user.id
    order_id = int(callback.data.split(":")[-1])
    order = await db.get_order_for_user(order_id, user_id, is_admin(user_id))
    if not order:
        await callback.answer("Order not found.", show_alert=True)
        return
    
    if not order.get("rid"):
        await callback.answer("Range ID missing for this order. Cannot allocate same range.", show_alert=True)
        return

    if not await db.cancel_order(order_id):
        await callback.answer("Unable to cancel current order.", show_alert=True)
        return

    if await db.count_active_orders(user_id) >= MAX_ACTIVE_ORDERS_PER_USER:
        await callback.answer(f"Active order limit reached. Max allowed is {MAX_ACTIVE_ORDERS_PER_USER}.", show_alert=True)
        return

    session = await get_session()
    try:
        allocated = await allocate_number(session, order["rid"])
    except ProviderAPIError as error:
        await callback.answer(str(error), show_alert=True)
        return

    number = normalize_digits(allocated.get("no_plus_number") or allocated.get("full_number") or "")
    if not number:
        await callback.answer("Provider returned an invalid number.", show_alert=True)
        return

    new_order = await db.create_order(
        user_id=user_id,
        number=number,
        service=order["service"],
        region=order["region"],
        rid=order["rid"],
        price=0.0,
    )
    text = (
        "📞 <b>Number Allocated</b>\n\n"
        f"🌍 Region: <b>{html.escape(new_order['region'])}</b>\n"
        f"📶 Range: <code>{new_order['rid']}</code>\n"
        f"🔹 Service: <b>{html.escape(new_order['service'])}</b>\n"
        f"📞 Number: <code>+{number}</code>\n"
        f"📌 Status: <b>Waiting for OTP</b>\n"
        f"🎁 Reward After OTP: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
    )
    await callback.answer("New number allocated!")
    await safe_edit(callback, text, build_order_actions(new_order))


@router.callback_query(F.data.startswith("withdraw:approve:"))
async def withdraw_approve_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    withdrawal_id = int(callback.data.split(":")[-1])
    success, message, withdrawal = await db.approve_withdrawal(withdrawal_id, callback.from_user.id)
    if not success or not withdrawal:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer("Withdrawal approved.")
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    with suppress(Exception):
        await callback.bot.send_message(
            withdrawal["user_id"],
            format_withdrawal_user(withdrawal, approved=True),
            reply_markup=build_wallet_keyboard(),
        )


@router.callback_query(F.data.startswith("withdraw:reject:"))
async def withdraw_reject_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    withdrawal_id = int(callback.data.split(":")[-1])
    success, message, withdrawal = await db.reject_withdrawal(withdrawal_id, callback.from_user.id)
    if not success or not withdrawal:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer("Withdrawal rejected.")
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    with suppress(Exception):
        await callback.bot.send_message(
            withdrawal["user_id"],
            format_withdrawal_user(withdrawal, approved=False),
            reply_markup=build_wallet_keyboard(),
        )


@router.callback_query(F.data == "admin:menu")
async def admin_menu_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    settings = await db.get_settings()
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"OTP Reward: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
        f"Minimum Withdraw: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>\n"
        f"Network: <b>{WITHDRAW_NETWORK_LABEL}</b>"
    )
    await safe_edit(callback, text, build_admin_menu())


@router.callback_query(F.data == "admin:settings")
async def admin_settings_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    settings = await db.get_settings()
    text = (
        "⏱ <b>Order Timeout</b>\n\n"
        f"OTP Reward: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
        f"Minimum Withdraw: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>"
    )
    await safe_edit(callback, text, build_admin_settings(settings))


@router.callback_query(F.data == "admin:set_timeout")
async def admin_set_timeout_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_timeout)
    await safe_edit(callback, "Send the new order timeout in minutes, for example: <code>10</code>", build_cancel_keyboard())


@router.callback_query(F.data == "admin:balances")
async def admin_balances_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_balance)
    await safe_edit(callback, "Send: <code>USER_ID AMOUNT</code>", build_cancel_keyboard())


@router.callback_query(F.data == "admin:stats")
async def admin_stats_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    stats = await db.admin_stats()
    await safe_edit(callback, format_admin_stats(stats), build_admin_menu())


@router.callback_query(F.data == "admin:withdrawals")
async def admin_withdrawals_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    withdrawals = await db.list_pending_withdrawals()
    if not withdrawals:
        text = "💸 <b>Pending Withdrawals</b>\n\nNo pending withdrawals right now."
    else:
        lines = ["💸 <b>Pending Withdrawals</b>\n"]
        for withdrawal in withdrawals:
            lines.append(
                f"#{withdrawal['withdrawal_id']} • {withdrawal['user_id']} • {format_currency(float(withdrawal['amount']))}"
            )
        text = "\n".join(lines)
    await safe_edit(callback, text, build_admin_menu())


@router.callback_query(F.data == "admin:orders")
async def admin_orders_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    orders = await db.list_active_orders()
    if not orders:
        text = "📦 <b>Active Orders</b>\n\nNo active orders right now."
    else:
        lines = ["📦 <b>Active Orders</b>\n"]
        for order in orders:
            lines.append(
                f"#{order['order_id']} • {order['region']} • {order['service']} • {human_status(order['status'])}"
            )
        text = "\n".join(lines)
    await safe_edit(callback, text, build_admin_menu())


@router.callback_query(F.data == "admin:ban")
async def admin_ban_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_ban)
    await safe_edit(callback, "Send the user ID to ban.", build_cancel_keyboard())


@router.callback_query(F.data == "admin:unban")
async def admin_unban_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_unban)
    await safe_edit(callback, "Send the user ID to unban.", build_cancel_keyboard())

@router.callback_query(F.data == "nav:cancel_action")
async def cancel_action_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    await state.clear()
    await callback.answer("Action cancelled.")
    await safe_edit(callback, "❌ <b>Action cancelled.</b>", reply_markup=None)



@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    await callback.answer()


@router.message(AdminState.waiting_balance)
async def admin_balance_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Send exactly: <code>USER_ID AMOUNT</code>")
        return
    try:
        user_id = int(parts[0])
        amount = float(parts[1])
    except ValueError:
        await message.answer("USER_ID must be an integer and AMOUNT must be a number.")
        return
    await db.ensure_user(user_id, None)
    balance = await db.add_balance(user_id, amount)
    await state.clear()
    await message.answer(f"Updated balance: <b>{format_currency(balance)}</b> for <code>{user_id}</code>.")


@router.message(WalletState.waiting_address)
async def wallet_address_input(message: Message, state: FSMContext) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    address = (message.text or "").strip()
    if len(address) < 10 or " " in address:
        await message.answer(
            f"Send a valid {WITHDRAW_NETWORK_LABEL} address without spaces.",
            reply_markup=build_wallet_keyboard(),
        )
        return
    await db.set_wallet_address(message.from_user.id, address)
    data = await state.get_data()
    await state.clear()
    await message.answer(
        f"{WITHDRAW_NETWORK_LABEL} address saved successfully.",
        reply_markup=build_wallet_keyboard(),
    )
    if data.get("next_action") == "withdraw":
        balance = await db.get_balance(message.from_user.id)
        if balance < MIN_WITHDRAW_BDT:
            await message.answer(
                f"You need at least {format_currency(MIN_WITHDRAW_BDT)} to withdraw.",
                reply_markup=build_wallet_keyboard(),
            )
            return
        await state.set_state(WalletState.waiting_withdraw_amount)
        await message.answer(
            f"Now send the amount you want to withdraw in BDT.\nMinimum: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>",
            reply_markup=build_wallet_keyboard(),
        )


@router.message(WalletState.waiting_withdraw_amount)
async def wallet_withdraw_amount_input(message: Message, state: FSMContext) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    pending = await db.get_pending_withdrawal_for_user(message.from_user.id)
    if pending:
        await state.clear()
        await message.answer("You already have a pending withdrawal request.", reply_markup=build_wallet_keyboard())
        return
    try:
        amount = float((message.text or "").strip())
    except ValueError:
        await message.answer(
            "Send a valid BDT amount, for example <code>50</code>.",
            reply_markup=build_wallet_keyboard(),
        )
        return
    balance = await db.get_balance(message.from_user.id)
    address = await db.get_wallet_address(message.from_user.id)
    if not address:
        await state.set_state(WalletState.waiting_address)
        await state.update_data(next_action="withdraw")
        await message.answer(
            f"Please send your {WITHDRAW_NETWORK_LABEL} address first.",
            reply_markup=build_wallet_keyboard(),
        )
        return
    if amount < MIN_WITHDRAW_BDT:
        await message.answer(
            f"The minimum withdrawal is {format_currency(MIN_WITHDRAW_BDT)}.",
            reply_markup=build_wallet_keyboard(),
        )
        return
    if amount > balance:
        await message.answer(
            f"You only have {format_currency(balance)} available.",
            reply_markup=build_wallet_keyboard(),
        )
        return
    withdrawal = await db.create_withdrawal(message.from_user.id, amount, address)
    user = await db.get_user(message.from_user.id)
    admin_text = format_withdrawal_admin(withdrawal, user)
    for admin_id in ADMIN_IDS:
        with suppress(Exception):
            await message.bot.send_message(
                admin_id,
                admin_text,
                reply_markup=build_withdrawal_review_keyboard(withdrawal["withdrawal_id"]),
            )
    await state.clear()
    await message.answer(
        f"Withdrawal request submitted for <b>{format_currency(amount)}</b>.\nAdmin will review and pay it manually to your {WITHDRAW_NETWORK_LABEL} address.",
        reply_markup=build_wallet_keyboard(),
    )


@router.message(AdminState.waiting_timeout)
async def admin_timeout_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        minutes = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a valid integer number of minutes.")
        return
    if minutes < 1:
        await message.answer("Timeout must be at least 1 minute.")
        return
    await db.set_order_timeout(minutes)
    await state.clear()
    await message.answer(f"Order timeout updated to <b>{minutes} minutes</b>.")


@router.message(AdminState.waiting_ban)
async def admin_ban_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a valid numeric user ID.")
        return
    await db.ensure_user(user_id, None)
    await db.set_banned(user_id, True)
    await state.clear()
    await message.answer(f"User <code>{user_id}</code> has been banned.")


@router.message(AdminState.waiting_unban)
async def admin_unban_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a valid numeric user ID.")
        return
    await db.ensure_user(user_id, None)
    await db.set_banned(user_id, False)
    await state.clear()
    await message.answer(f"User <code>{user_id}</code> has been unbanned.")


@router.message(BuyState.waiting_custom_range)
async def custom_range_input(message: Message, state: FSMContext) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    query = normalize_digits(message.text or "")
    if len(query) < 2:
        await message.answer(
            "Send at least 2 digits for the custom range search, for example <code>255</code>.",
            reply_markup=build_custom_range_prompt_keyboard(),
        )
        return
    await state.clear()
    await show_custom_range_results(message, query, page=0)


@router.message()
async def fallback_message_handler(message: Message) -> None:
    allowed = await ensure_user_record(message.from_user)
    if not allowed:
        await message.answer("🚫 Your access to this bot has been blocked.")
        return
    await message.answer("Use the menu below to continue.", reply_markup=build_home_keyboard(is_admin(message.from_user.id)))


async def otp_worker(bot: Bot) -> None:
    logger.info("OTP worker started.")
    while True:
        try:
            settings = await db.get_settings()
            expired_count = await db.expire_waiting_orders(settings["order_timeout_minutes"])
            if expired_count:
                logger.info("Expired %s waiting orders.", expired_count)

            otps = await fetch_otps(await get_session())
            for otp in otps:
                number = normalize_digits(str(otp.get("number", "")))
                otp_id = str(otp.get("otp_id", ""))
                message = str(otp.get("message", ""))
                if not number or not otp_id or not message:
                    continue
                received_at = datetime.fromtimestamp(
                    int(otp.get("time", 0)) / 1000, tz=UTC
                ).replace(microsecond=0).isoformat()
                order = await db.attach_otp(
                    number=number,
                    provider_otp_id=otp_id,
                    message=message,
                    code=extract_otp(message),
                    received_at=received_at,
                )
                if not order:
                    continue

                logger.info("Matched OTP for order %s and number %s", order["order_id"], number)
                text = (
                    f"{format_order_card(order, reveal_otp=True)}\n\n"
                    f"🎁 Reward Added: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
                    f"🕒 Received: <b>{format_unix_ms(int(otp.get('time', 0)))}</b>"
                )
                with suppress(Exception):
                    await bot.send_message(
                        order["user_id"],
                        text,
                        reply_markup=build_order_actions(order),
                    )
        except ProviderAPIError as error:
            logger.warning("Provider API warning: %s", error)
        except Exception:
            logger.exception("Unexpected error inside OTP worker.")
        await asyncio.sleep(OTP_CHECK_INTERVAL)


async def on_startup(bot: Bot) -> None:
    global http_session, otp_worker_task, range_notifier_task
    await db.initialize()
    http_session = aiohttp.ClientSession()
    try:
        await get_catalog(http_session, force_refresh=True)
        logger.info("Loaded live provider catalog successfully.")
    except Exception as error:
        logger.warning("Could not preload provider catalog: %s", error)
    otp_worker_task = asyncio.create_task(otp_worker(bot))
    range_notifier_task = start_range_notifier(bot, http_session)


async def on_shutdown(_bot: Bot) -> None:
    global otp_worker_task, range_notifier_task, http_session
    if otp_worker_task:
        otp_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await otp_worker_task
    if range_notifier_task:
        range_notifier_task.cancel()
        with suppress(asyncio.CancelledError):
            await range_notifier_task
    if http_session:
        await http_session.close()
    await db.close()


async def main() -> None:
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN":
        raise RuntimeError("Please set BOT_TOKEN in config.py before starting the bot.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Bot is starting.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
