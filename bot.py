import asyncio
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
    DEFAULT_OTP_PRICE,
    MAX_ACTIVE_ORDERS_PER_USER,
    OTP_CHECK_INTERVAL,
)
from db import Database
from utility import (
    ProviderAPIError,
    allocate_number,
    build_admin_menu,
    build_admin_settings,
    build_home_keyboard,
    build_order_actions,
    build_orders_keyboard,
    build_regions_keyboard,
    build_services_keyboard,
    extract_otp,
    fetch_otps,
    format_admin_stats,
    format_currency,
    format_dashboard,
    format_help,
    format_iso,
    format_order_card,
    format_user_stats,
    format_wallet,
    format_unix_ms,
    get_catalog,
    get_region_services,
    get_regions,
    human_status,
    normalize_digits,
    pick_rid_for_service,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("otp-bot")

router = Router()
db = Database()
http_session: aiohttp.ClientSession | None = None
otp_worker_task: asyncio.Task | None = None


class AdminState(StatesGroup):
    waiting_balance = State()
    waiting_price = State()
    waiting_timeout = State()
    waiting_ban = State()
    waiting_unban = State()


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
        await callback.message.answer(text, reply_markup=reply_markup)


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
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def show_wallet(target: CallbackQuery) -> None:
    if not await ensure_callback_user(target):
        return
    await target.answer()
    balance = await db.get_balance(target.from_user.id)
    await safe_edit(target, format_wallet(balance), build_home_keyboard(is_admin(target.from_user.id)))


async def show_help(target: CallbackQuery) -> None:
    if not await ensure_callback_user(target):
        return
    await target.answer()
    await safe_edit(target, format_help(), build_home_keyboard(is_admin(target.from_user.id)))


async def show_user_stats(target: CallbackQuery) -> None:
    if not await ensure_callback_user(target):
        return
    await target.answer()
    stats = await db.user_stats(target.from_user.id)
    balance = await db.get_balance(target.from_user.id)
    text = format_user_stats(stats, balance)
    if is_admin(target.from_user.id):
        admin_stats = await db.admin_stats()
        text = f"{text}\n\n{format_admin_stats(admin_stats)}"
    await safe_edit(target, text, build_home_keyboard(is_admin(target.from_user.id)))


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
    await safe_edit(target, text, build_orders_keyboard(orders, kind))


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


async def show_regions(target: CallbackQuery, page: int = 0, force_refresh: bool = False) -> None:
    if not await ensure_callback_user(target):
        return
    session = await get_session()
    try:
        regions = await get_regions(session, force_refresh=force_refresh)
    except ProviderAPIError as error:
        await target.answer(str(error), show_alert=True)
        return
    if not regions:
        await target.answer()
        await safe_edit(target, "No live regions are available right now.", build_home_keyboard(is_admin(target.from_user.id)))
        return
    await target.answer()
    text = "🌍 <b>Select Region</b>\n\nLive regions are pulled from the provider access feed."
    await safe_edit(target, text, build_regions_keyboard(regions, page=page))


async def show_services(target: CallbackQuery, region_code: str, page: int = 0) -> None:
    if not await ensure_callback_user(target):
        return
    session = await get_session()
    region, services = await get_region_services(session, region_code)
    if not region or not services:
        await target.answer("This region is not available anymore. Please refresh.", show_alert=True)
        return
    await target.answer()
    text = (
        f"{region['flag']} <b>{region['name']}</b>\n\n"
        "Select the service you want to use for this number."
    )
    await safe_edit(target, text, build_services_keyboard(region, services, page=page))


async def allocate_for_user(target: CallbackQuery, region_code: str, token: str) -> None:
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
    settings = await db.get_settings()
    try:
        region, service, rid = await pick_rid_for_service(session, region_code, token)
        allocated = await allocate_number(session, rid)
    except ProviderAPIError as error:
        await target.answer(str(error), show_alert=True)
        return

    number = normalize_digits(allocated.get("no_plus_number") or allocated.get("full_number") or "")
    if not number:
        await target.answer("Provider returned an invalid number.", show_alert=True)
        return

    price = 0.0 if settings["free_mode"] else float(settings["otp_price"] or DEFAULT_OTP_PRICE)
    order = await db.create_order(
        user_id=user_id,
        number=number,
        service=service["name"],
        region=region["name"],
        price=price,
    )
    text = (
        "📞 <b>Number Allocated</b>\n\n"
        f"🌍 Region: <b>{region['name']}</b>\n"
        f"🔹 Service: <b>{service['name']}</b>\n"
        f"📞 Number: <code>+{number}</code>\n"
        f"📌 Status: <b>Waiting for OTP</b>\n"
    )
    if price > 0:
        text += f"💳 Unlock Price: <b>{format_currency(price)}</b>\n"
    else:
        text += "🎁 Free Mode: <b>OTP will be delivered automatically</b>\n"
    await safe_edit(target, text, build_order_actions(order))


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await show_home(message)


@router.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("This command is only available to admins.")
        return
    settings = await db.get_settings()
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"Free Mode: <b>{'ON' if settings['free_mode'] else 'OFF'}</b>\n"
        f"OTP Price: <b>{format_currency(settings['otp_price'])}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>"
    )
    await message.answer(text, reply_markup=build_admin_menu())


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
    await show_wallet(callback)


@router.callback_query(F.data == "nav:help")
async def help_callback(callback: CallbackQuery) -> None:
    await show_help(callback)


@router.callback_query(F.data == "nav:stats")
async def stats_callback(callback: CallbackQuery) -> None:
    await show_user_stats(callback)


@router.callback_query(F.data == "nav:orders")
@router.callback_query(F.data == "nav:orders:active")
async def orders_callback(callback: CallbackQuery) -> None:
    await show_orders(callback, "active")


@router.callback_query(F.data == "nav:orders:completed")
async def completed_orders_callback(callback: CallbackQuery) -> None:
    await show_orders(callback, "completed")


@router.callback_query(F.data == "buy:start")
async def buy_start_callback(callback: CallbackQuery) -> None:
    await show_regions(callback)


@router.callback_query(F.data.startswith("buy:regions:"))
async def buy_regions_page_callback(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    await show_regions(callback, page=page)


@router.callback_query(F.data == "buy:refresh")
async def buy_refresh_callback(callback: CallbackQuery) -> None:
    await show_regions(callback, force_refresh=True)


@router.callback_query(F.data.startswith("buy:region:"))
async def buy_region_callback(callback: CallbackQuery) -> None:
    _, _, region_code, page = callback.data.split(":")
    await show_services(callback, region_code, page=int(page))


@router.callback_query(F.data.startswith("buy:service:"))
async def buy_service_callback(callback: CallbackQuery) -> None:
    _, _, region_code, token, _page = callback.data.split(":")
    await allocate_for_user(callback, region_code, token)


@router.callback_query(F.data.startswith("order:view:"))
async def order_view_callback(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[-1])
    await show_single_order(callback, order_id)


@router.callback_query(F.data.startswith("order:unlock:"))
async def order_unlock_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    order_id = int(callback.data.split(":")[-1])
    success, message, order = await db.unlock_order(order_id, callback.from_user.id)
    if not success or not order:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, format_order_card(order, reveal_otp=True), build_order_actions(order))


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
        f"Free Mode: <b>{'ON' if settings['free_mode'] else 'OFF'}</b>\n"
        f"OTP Price: <b>{format_currency(settings['otp_price'])}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>"
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
        "⚙ <b>Settings</b>\n\n"
        f"Free Mode: <b>{'ON' if settings['free_mode'] else 'OFF'}</b>\n"
        f"OTP Price: <b>{format_currency(settings['otp_price'])}</b>\n"
        f"Timeout: <b>{settings['order_timeout_minutes']} minutes</b>"
    )
    await safe_edit(callback, text, build_admin_settings(settings))


@router.callback_query(F.data == "admin:toggle_free")
async def admin_toggle_free_callback(callback: CallbackQuery) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    settings = await db.get_settings()
    await db.set_free_mode(not settings["free_mode"])
    await admin_settings_callback(callback)


@router.callback_query(F.data == "admin:set_price")
async def admin_set_price_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_price)
    await callback.message.answer("Send the new OTP price in USD, for example: <code>0.25</code>")


@router.callback_query(F.data == "admin:set_timeout")
async def admin_set_timeout_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_timeout)
    await callback.message.answer("Send the new order timeout in minutes, for example: <code>10</code>")


@router.callback_query(F.data == "admin:balances")
async def admin_balances_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_balance)
    await callback.message.answer("Send: <code>USER_ID AMOUNT</code>")


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
    await callback.message.answer("Send the user ID to ban.")


@router.callback_query(F.data == "admin:unban")
async def admin_unban_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_callback_user(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Admins only.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminState.waiting_unban)
    await callback.message.answer("Send the user ID to unban.")


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


@router.message(AdminState.waiting_price)
async def admin_price_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        price = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a valid number, for example: <code>0.25</code>")
        return
    if price < 0:
        await message.answer("Price must be zero or greater.")
        return
    await db.set_otp_price(price)
    await state.clear()
    await message.answer(f"OTP price updated to <b>{format_currency(price)}</b>.")


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
                if order["status"] == "otp_locked":
                    text = (
                        "🔒 <b>OTP Received</b>\n\n"
                        f"Order #{order['order_id']} now has an OTP waiting.\n"
                        f"Unlock Price: <b>{format_currency(float(order['price']))}</b>\n"
                        f"Received: <b>{format_unix_ms(int(otp.get('time', 0)))}</b>"
                    )
                else:
                    text = format_order_card(order, reveal_otp=True)
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
    global http_session, otp_worker_task
    await db.initialize()
    http_session = aiohttp.ClientSession()
    try:
        await get_catalog(http_session, force_refresh=True)
        logger.info("Loaded live provider catalog successfully.")
    except Exception as error:
        logger.warning("Could not preload provider catalog: %s", error)
    otp_worker_task = asyncio.create_task(otp_worker(bot))


async def on_shutdown(_bot: Bot) -> None:
    global otp_worker_task, http_session
    if otp_worker_task:
        otp_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await otp_worker_task
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
