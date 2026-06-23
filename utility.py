import random
import re
import zlib
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import aiohttp
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import API_BASE_URL, API_KEY, HTTP_TIMEOUT_SECONDS, LIVEACCESS_CACHE_SECONDS


class ProviderAPIError(Exception):
    pass


COUNTRY_PREFIXES = {
    "261": ("🇲🇬", "Madagascar"),
    "256": ("🇺🇬", "Uganda"),
    "255": ("🇹🇿", "Tanzania"),
    "254": ("🇰🇪", "Kenya"),
    "244": ("🇦🇴", "Angola"),
    "236": ("🇨🇫", "Central African Republic"),
    "232": ("🇸🇱", "Sierra Leone"),
    "229": ("🇧🇯", "Benin"),
    "228": ("🇹🇬", "Togo"),
    "225": ("🇨🇮", "Côte d'Ivoire"),
    "223": ("🇲🇱", "Mali"),
    "216": ("🇹🇳", "Tunisia"),
    "996": ("🇰🇬", "Kyrgyzstan"),
    "995": ("🇬🇪", "Georgia"),
    "992": ("🇹🇯", "Tajikistan"),
    "959": ("🇲🇲", "Myanmar"),
    "937": ("🇦🇫", "Afghanistan"),
    "855": ("🇰🇭", "Cambodia"),
    "849": ("🇻🇳", "Vietnam"),
    "642": ("🇳🇿", "New Zealand"),
    "639": ("🇵🇭", "Philippines"),
    "628": ("🇮🇩", "Indonesia"),
    "591": ("🇧🇴", "Bolivia"),
    "447": ("🇬🇧", "United Kingdom"),
    "380": ("🇺🇦", "Ukraine"),
    "374": ("🇦🇲", "Armenia"),
}

PRIORITY_SERVICES = ["TELEGRAM", "WHATSAPP", "FACEBOOK", "INSTAGRAM", "OPENAI", "DISCORD"]
SERVICE_EMOJIS = {
    "Telegram": "📱",
    "WhatsApp": "📱",
    "Facebook": "📘",
    "Instagram": "📸",
    "OPENAI": "🤖",
    "Discord": "💬",
    "DISCORD": "💬",
    "SLACK": "💼",
    "Stripe": "💳",
}
STATUS_LABELS = {
    "waiting_otp": "Waiting for OTP",
    "otp_locked": "OTP Locked",
    "completed": "Completed",
    "expired": "Expired",
    "cancelled": "Cancelled",
}
STATUS_BADGES = {
    "waiting_otp": "⏳",
    "otp_locked": "🔒",
    "completed": "✅",
    "expired": "⌛",
    "cancelled": "❌",
}

_catalog_cache: dict[str, Any] = {"at": 0.0, "catalog": None}


def provider_headers() -> dict[str, str]:
    return {"mauthapi": API_KEY}


async def provider_request(
    session: aiohttp.ClientSession, method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with session.request(
        method=method,
        url=url,
        headers=provider_headers(),
        json=payload,
        timeout=timeout,
    ) as response:
        data = await response.json(content_type=None)
    meta = data.get("meta", {})
    if meta.get("code") != 200:
        raise ProviderAPIError(data.get("message") or meta.get("status") or "Provider request failed.")
    return data


def normalize_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def trim_range_to_rid(range_value: str) -> str:
    return normalize_digits(range_value.replace("XXX", ""))


def detect_region(range_value: str) -> tuple[str, str, str] | None:
    digits = normalize_digits(range_value)
    for prefix in sorted(COUNTRY_PREFIXES, key=len, reverse=True):
        if digits.startswith(prefix):
            flag, name = COUNTRY_PREFIXES[prefix]
            return prefix, flag, name
    return None


def service_token(service_name: str) -> str:
    return str(zlib.adler32(service_name.encode("utf-8")) & 0xFFFFFFFF)


def service_emoji(service_name: str) -> str:
    return SERVICE_EMOJIS.get(service_name, "🔹")


def format_currency(amount: float) -> str:
    return f"${amount:.2f}"


def format_number(number: str) -> str:
    digits = normalize_digits(number)
    return f"+{digits}" if digits else "N/A"


def format_iso(iso_value: str | None) -> str:
    if not iso_value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_unix_ms(value: int | None) -> str:
    if not value:
        return "N/A"
    dt = datetime.fromtimestamp(value / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def extract_otp(message: str | None) -> str | None:
    if not message:
        return None
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", message)
    return match.group(1) if match else None


def human_status(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").title())


def short_order_label(order: dict[str, Any]) -> str:
    badge = STATUS_BADGES.get(order["status"], "•")
    return f"{badge} #{order['order_id']} {order['service']}"


async def get_liveaccess(session: aiohttp.ClientSession) -> dict[str, Any]:
    return await provider_request(session, "GET", "/liveaccess")


def build_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    catalog: dict[str, Any] = {"regions": {}}
    services = payload.get("data", {}).get("services", [])
    for service in services:
        sid = service.get("sid")
        last_at = int(service.get("last_at") or 0)
        for range_value in service.get("ranges", []):
            region = detect_region(range_value)
            if not region:
                continue
            code, flag, name = region
            region_entry = catalog["regions"].setdefault(
                code,
                {
                    "code": code,
                    "flag": flag,
                    "name": name,
                    "last_at": 0,
                    "services": {},
                },
            )
            region_entry["last_at"] = max(region_entry["last_at"], last_at)
            region_entry["services"].setdefault(sid, set()).add(trim_range_to_rid(range_value))
    return catalog


async def get_regions(session: aiohttp.ClientSession, force_refresh: bool = False) -> list[dict[str, Any]]:
    catalog = await get_catalog(session, force_refresh=force_refresh)
    regions = list(catalog["regions"].values())
    regions.sort(key=lambda item: (-item["last_at"], item["name"]))
    return regions


async def get_catalog(session: aiohttp.ClientSession, force_refresh: bool = False) -> dict[str, Any]:
    if (
        not force_refresh
        and _catalog_cache["catalog"] is not None
        and monotonic() - _catalog_cache["at"] < LIVEACCESS_CACHE_SECONDS
    ):
        return _catalog_cache["catalog"]
    payload = await get_liveaccess(session)
    catalog = build_catalog(payload)
    _catalog_cache.update({"at": monotonic(), "catalog": catalog})
    return catalog


async def get_region_services(
    session: aiohttp.ClientSession, region_code: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    catalog = await get_catalog(session)
    region = catalog["regions"].get(region_code)
    if not region:
        return None, []
    services = []
    for service_name, ranges in region["services"].items():
        services.append(
            {
                "name": service_name,
                "token": service_token(service_name),
                "ranges": sorted(ranges),
            }
        )
    services.sort(key=lambda item: service_sort_key(item["name"]))
    return region, services


def service_sort_key(service_name: str) -> tuple[int, str]:
    upper = service_name.upper()
    if upper in PRIORITY_SERVICES:
        return (PRIORITY_SERVICES.index(upper), service_name.lower())
    return (len(PRIORITY_SERVICES) + 1, service_name.lower())


async def pick_rid_for_service(
    session: aiohttp.ClientSession, region_code: str, service_token_value: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    region, services = await get_region_services(session, region_code)
    if not region:
        raise ProviderAPIError("Selected region is no longer available.")
    service = next((item for item in services if item["token"] == service_token_value), None)
    if not service:
        raise ProviderAPIError("Selected service is no longer available.")
    rid = random.choice(service["ranges"])
    return region, service, rid


async def allocate_number(session: aiohttp.ClientSession, rid: str) -> dict[str, Any]:
    response = await provider_request(session, "POST", "/getnum", {"rid": rid})
    return response.get("data", {})


async def fetch_otps(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    response = await provider_request(session, "GET", "/success-otp")
    return response.get("data", {}).get("otps", [])


def format_dashboard(user: dict[str, Any], stats: dict[str, Any], settings: dict[str, Any]) -> str:
    mode = "FREE MODE" if settings["free_mode"] else "PAID MODE"
    return (
        "🏠 <b>Home</b>\n\n"
        f"🆔 User ID: <code>{user['user_id']}</code>\n"
        f"💰 Balance: <b>{format_currency(float(user['balance']))}</b>\n"
        f"📦 Active Orders: <b>{stats['active_orders']}</b>\n"
        f"📈 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"⚙ Mode: <b>{mode}</b>"
    )


def format_wallet(balance: float) -> str:
    return (
        "💰 <b>Wallet</b>\n\n"
        f"Available Balance: <b>{format_currency(balance)}</b>\n\n"
        "Admin can top up manually with <code>/addbalance USER_ID AMOUNT</code>."
    )


def format_help() -> str:
    return (
        "ℹ️ <b>Help</b>\n\n"
        "1. Choose <b>Get Number</b>.\n"
        "2. Pick a region and service.\n"
        "3. Wait for the OTP worker to detect your code.\n"
        "4. In paid mode, unlock the OTP from your wallet balance.\n\n"
        "The bot matches OTPs only by the exact allocated number."
    )


def format_user_stats(stats: dict[str, Any], balance: float) -> str:
    return (
        "📊 <b>Your Statistics</b>\n\n"
        f"📦 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"✅ Successful OTPs: <b>{stats['successful_otps']}</b>\n"
        f"⏳ Active Orders: <b>{stats['active_orders']}</b>\n"
        f"💰 Balance: <b>{format_currency(balance)}</b>"
    )


def format_admin_stats(stats: dict[str, Any]) -> str:
    return (
        "📊 <b>Admin Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"📦 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"💵 Revenue: <b>{format_currency(stats['revenue'])}</b>\n"
        f"⏳ Active Orders: <b>{stats['active_orders']}</b>"
    )


def format_order_card(order: dict[str, Any], reveal_otp: bool = False) -> str:
    lines = [
        f"📦 <b>Order #{order['order_id']}</b>",
        "",
        f"📞 Number: <code>{format_number(order['number'])}</code>",
        f"🌍 Region: <b>{order['region']}</b>",
        f"🔹 Service: <b>{order['service']}</b>",
        f"📌 Status: <b>{human_status(order['status'])}</b>",
        f"🕒 Created: <b>{format_iso(order['created_at'])}</b>",
    ]
    if order.get("completed_at"):
        lines.append(f"✅ Updated: <b>{format_iso(order['completed_at'])}</b>")
    if order["status"] == "otp_locked":
        lines.append(f"💳 Unlock Price: <b>{format_currency(float(order['price']))}</b>")
    if order.get("otp_message"):
        if reveal_otp:
            lines.append(f"📩 OTP Message: <code>{order['otp_message']}</code>")
            if order.get("otp_code"):
                lines.append(f"🔐 OTP Code: <b>{order['otp_code']}</b>")
        else:
            lines.append("🔒 OTP Message: <b>Locked</b>")
    else:
        lines.append("⏳ OTP Status: <b>Waiting for OTP</b>")
    return "\n".join(lines)


def build_home_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📞 Get Number", callback_data="buy:start")
    builder.button(text="💰 Wallet", callback_data="nav:wallet")
    builder.button(text="📦 My Orders", callback_data="nav:orders")
    builder.button(text="📊 Statistics", callback_data="nav:stats")
    builder.button(text="ℹ️ Help", callback_data="nav:help")
    builder.button(text="🏠 Home", callback_data="nav:home")
    if is_admin:
        builder.button(text="🛠 Admin", callback_data="admin:menu")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * page_size
    return items[start : start + page_size], total_pages


def build_regions_keyboard(regions: list[dict[str, Any]], page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(regions, page, 8)
    for region in chunk:
        builder.button(
            text=f"{region['flag']} {region['name']}",
            callback_data=f"buy:region:{region['code']}:{page}",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(text="⬅️ Prev", callback_data=f"buy:regions:{page - 1}")
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
        if page < total_pages - 1:
            builder.button(text="Next ➡️", callback_data=f"buy:regions:{page + 1}")
    builder.button(text="🔄 Refresh", callback_data="buy:refresh")
    builder.button(text="🏠 Home", callback_data="nav:home")
    builder.adjust(1, 1, 1, 2)
    return builder.as_markup()


def build_services_keyboard(
    region: dict[str, Any], services: list[dict[str, Any]], page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(services, page, 8)
    for service in chunk:
        builder.button(
            text=f"{service_emoji(service['name'])} {service['name']}",
            callback_data=f"buy:service:{region['code']}:{service['token']}:{page}",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(
                text="⬅️ Prev", callback_data=f"buy:region:{region['code']}:{page - 1}"
            )
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
        if page < total_pages - 1:
            builder.button(
                text="Next ➡️", callback_data=f"buy:region:{region['code']}:{page + 1}"
            )
    builder.button(text="⬅️ Regions", callback_data="buy:start")
    builder.button(text="🏠 Home", callback_data="nav:home")
    builder.adjust(1, 1, 1, 2)
    return builder.as_markup()


def build_orders_keyboard(orders: list[dict[str, Any]], kind: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.button(text=short_order_label(order), callback_data=f"order:view:{order['order_id']}")
    if kind == "active":
        builder.button(text="✅ Completed Orders", callback_data="nav:orders:completed")
    else:
        builder.button(text="⏳ Active Orders", callback_data="nav:orders:active")
    builder.button(text="🏠 Home", callback_data="nav:home")
    builder.adjust(1)
    return builder.as_markup()


def build_order_actions(order: dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if order["status"] == "otp_locked":
        builder.button(text="💳 Unlock OTP", callback_data=f"order:unlock:{order['order_id']}")
        builder.button(text="❌ Cancel", callback_data=f"order:cancel:{order['order_id']}")
    elif order["status"] == "waiting_otp":
        builder.button(text="🔄 Refresh", callback_data=f"order:view:{order['order_id']}")
        builder.button(text="❌ Cancel", callback_data=f"order:cancel:{order['order_id']}")
    else:
        builder.button(text="📦 My Orders", callback_data="nav:orders")
    builder.button(text="🏠 Home", callback_data="nav:home")
    builder.adjust(2, 1)
    return builder.as_markup()


def build_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙ Settings", callback_data="admin:settings")
    builder.button(text="💰 User Balances", callback_data="admin:balances")
    builder.button(text="📦 Active Orders", callback_data="admin:orders")
    builder.button(text="📊 Statistics", callback_data="admin:stats")
    builder.button(text="🚫 Ban User", callback_data="admin:ban")
    builder.button(text="✅ Unban User", callback_data="admin:unban")
    builder.button(text="🏠 Home", callback_data="nav:home")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def build_admin_settings(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    mode = "ON" if settings["free_mode"] else "OFF"
    builder.button(text=f"Toggle Free Mode ({mode})", callback_data="admin:toggle_free")
    builder.button(text="Set OTP Price", callback_data="admin:set_price")
    builder.button(text="Set Timeout", callback_data="admin:set_timeout")
    builder.button(text="⬅️ Admin Menu", callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()
