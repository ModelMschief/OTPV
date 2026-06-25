import random
import re
import zlib
from datetime import UTC, datetime
from html import escape
from time import monotonic
from typing import Any

import aiohttp
from aiogram.types import CopyTextButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    API_BASE_URL,
    API_KEY,
    HTTP_TIMEOUT_SECONDS,
    LIVEACCESS_CACHE_SECONDS,
    MIN_WITHDRAW_BDT,
    OTP_REWARD_BDT,
    WITHDRAW_NETWORK_LABEL,
)


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
    "224": ("🇬🇳", "Guinea"),
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
OTP_GROUP_LINK = "https://t.me/NEWTON_RENGE_GROUP"


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
    rid = trim_range_to_rid(range_value)
    return rid, "🌍", f"Region +{rid}"


def service_token(service_name: str) -> str:
    return str(zlib.adler32(service_name.encode("utf-8")) & 0xFFFFFFFF)


def service_emoji(service_name: str) -> str:
    return SERVICE_EMOJIS.get(service_name, "🔹")


def format_currency(amount: float) -> str:
    return f"৳{amount:.2f}"


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
    catalog: dict[str, Any] = {"regions": {}, "ranges": {}}
    services = payload.get("data", {}).get("services", [])
    for service in services:
        sid = service.get("sid")
        last_at = int(service.get("last_at") or 0)
        for range_value in service.get("ranges", []):
            region = detect_region(range_value)
            if not region:
                continue
            code, flag, name = region
            rid = trim_range_to_rid(range_value)
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
            region_entry["services"].setdefault(sid, set()).add(rid)
            range_entry = catalog["ranges"].setdefault(
                rid,
                {
                    "rid": rid,
                    "range": range_value,
                    "flag": flag,
                    "region_code": code,
                    "region_name": name,
                    "last_at": 0,
                    "services": set(),
                },
            )
            range_entry["last_at"] = max(range_entry["last_at"], last_at)
            range_entry["services"].add(sid)
    return catalog


async def get_regions(session: aiohttp.ClientSession, force_refresh: bool = False) -> list[dict[str, Any]]:
    catalog = await get_catalog(session, force_refresh=force_refresh)
    regions = list(catalog["regions"].values())
    regions.sort(key=lambda item: (-item["last_at"], item["name"]))
    return regions


async def get_services(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    catalog = await get_catalog(session)
    service_map: dict[str, dict[str, Any]] = {}
    for region in catalog["regions"].values():
        for service_name, ranges in region["services"].items():
            entry = service_map.setdefault(
                service_name,
                {
                    "name": service_name,
                    "token": service_token(service_name),
                    "last_at": 0,
                    "regions": set(),
                    "range_count": 0,
                },
            )
            entry["last_at"] = max(entry["last_at"], region["last_at"])
            entry["regions"].add(region["code"])
            entry["range_count"] += len(ranges)
    services = [
        {
            "name": item["name"],
            "token": item["token"],
            "last_at": item["last_at"],
            "regions_count": len(item["regions"]),
            "range_count": item["range_count"],
        }
        for item in service_map.values()
    ]
    services.sort(key=lambda item: service_sort_key(item["name"]))
    return services


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


async def get_service_regions(
    session: aiohttp.ClientSession, service_token_value: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    catalog = await get_catalog(session)
    services = await get_services(session)
    service = next((item for item in services if item["token"] == service_token_value), None)
    if not service:
        return None, []
    regions = []
    for region in catalog["regions"].values():
        ranges = region["services"].get(service["name"])
        if not ranges:
            continue
        regions.append(
            {
                "code": region["code"],
                "flag": region["flag"],
                "name": region["name"],
                "last_at": region["last_at"],
                "ranges": sorted(ranges),
                "rid_count": len(ranges),
            }
        )
    regions.sort(key=lambda item: (-item["last_at"], item["name"]))
    return service, regions


async def search_custom_ranges(
    session: aiohttp.ClientSession, query: str
) -> list[dict[str, Any]]:
    digits = normalize_digits(query)
    if not digits:
        return []
    catalog = await get_catalog(session)
    matches = [
        {
            "rid": item["rid"],
            "range": item["range"],
            "flag": item["flag"],
            "region_code": item["region_code"],
            "region_name": item["region_name"],
            "last_at": item["last_at"],
            "services_count": len(item["services"]),
        }
        for item in catalog["ranges"].values()
        if item["rid"].startswith(digits)
    ]
    matches.sort(key=lambda item: (-item["last_at"], item["rid"]))
    return matches


async def get_range_services(
    session: aiohttp.ClientSession, rid: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    catalog = await get_catalog(session)
    range_entry = catalog["ranges"].get(rid)
    if not range_entry:
        return None, []
    services = [
        {
            "name": service_name,
            "token": service_token(service_name),
        }
        for service_name in sorted(range_entry["services"], key=service_sort_key)
    ]
    return (
        {
            "rid": range_entry["rid"],
            "range": range_entry["range"],
            "flag": range_entry["flag"],
            "region_code": range_entry["region_code"],
            "region_name": range_entry["region_name"],
            "last_at": range_entry["last_at"],
        },
        services,
    )


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


async def pick_rid_for_region_service(
    session: aiohttp.ClientSession, service_token_value: str, region_code: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    service, regions = await get_service_regions(session, service_token_value)
    if not service:
        raise ProviderAPIError("Selected platform is no longer available.")
    region = next((item for item in regions if item["code"] == region_code), None)
    if not region:
        raise ProviderAPIError("Selected region is no longer available for this platform.")
    rid = random.choice(region["ranges"])
    return service, region, rid


async def pick_service_for_range(
    session: aiohttp.ClientSession, rid: str, service_token_value: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    range_entry, services = await get_range_services(session, rid)
    if not range_entry:
        raise ProviderAPIError("Selected range is no longer available.")
    service = next((item for item in services if item["token"] == service_token_value), None)
    if not service:
        raise ProviderAPIError("Selected service is no longer available for this range.")
    return range_entry, service


async def allocate_number(session: aiohttp.ClientSession, rid: str) -> dict[str, Any]:
    response = await provider_request(session, "POST", "/getnum", {"rid": rid})
    return response.get("data", {})


async def fetch_otps(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    response = await provider_request(session, "GET", "/success-otp")
    return response.get("data", {}).get("otps", [])


def format_dashboard(user: dict[str, Any], stats: dict[str, Any], settings: dict[str, Any]) -> str:
    return (
        "🏠 <b>Home</b>\n\n"
        "🎉 <b>Welcome!</b>\n"
        "We don't enforce users to pay money. Instead, <b>we pay YOU</b>!\n"
        f"For every OTP you successfully receive from the bot, you will get a <b>{format_currency(OTP_REWARD_BDT)}</b> credit to your balance.\n\n"
        f"🆔 User ID: <code>{user['user_id']}</code>\n"
        f"💰 Balance: <b>{format_currency(float(user['balance']))}</b>\n"
        f"📦 Active Orders: <b>{stats['active_orders']}</b>\n"
        f"📈 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"💸 Minimum Withdraw: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>"
    )


def format_wallet(balance: float, wallet_address: str | None, pending_withdrawal: dict[str, Any] | None) -> str:
    address_text = f"<code>{escape(wallet_address)}</code>" if wallet_address else "<b>Not set</b>"
    pending_text = (
        f"\n🕒 Pending Withdrawal: <b>{format_currency(float(pending_withdrawal['amount']))}</b>"
        if pending_withdrawal
        else ""
    )
    return (
        "💰 <b>Wallet</b>\n\n"
        f"Available Balance: <b>{format_currency(balance)}</b>\n\n"
        f"Reward Per OTP: <b>{format_currency(OTP_REWARD_BDT)}</b>\n"
        f"Minimum Withdraw: <b>{format_currency(MIN_WITHDRAW_BDT)}</b>\n"
        f"{WITHDRAW_NETWORK_LABEL} Address: {address_text}"
        f"{pending_text}\n\n"
        "Use the buttons below to withdraw or update your address."
    )


def format_help() -> str:
    return (
        "ℹ️ <b>How to Use This Bot</b>\n\n"
        "🔹 <b>Step 1:</b> Tap on <b>Get Number</b> from the menu.\n\n"
        "🔹 <b>Step 2:</b> Select your desired platform and choose a live region.\n\n"
        f"🔹 <b>Step 3:</b> When the OTP arrives, you will automatically earn a <b>{format_currency(OTP_REWARD_BDT)}</b> reward!\n\n"
        f"🔹 <b>Step 4:</b> Once your balance reaches <b>{format_currency(MIN_WITHDRAW_BDT)}</b>, submit your <b>{WITHDRAW_NETWORK_LABEL}</b> address to request a withdrawal.\n\n"
        "⚠️ <i>Note: The bot matches OTPs only by the exact allocated number.</i>"
    )


def format_profile(user: dict[str, Any], stats: dict[str, Any], balance: float) -> str:
    username = f"@{user['username']}" if user.get("username") else "No username"
    return (
        "👤 <b>My Profile</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"👤 Name: <b>{escape(username)}</b>\n"
        f"💰 Balance: <b>{format_currency(balance)}</b>\n\n"
        f"📦 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"✅ Successful OTPs: <b>{stats['successful_otps']}</b>\n"
        f"⏳ Active Orders: <b>{stats['active_orders']}</b>"
    )


def format_leaderboard(entries: list[dict[str, Any]], current_user_id: int | None = None) -> str:
    lines = ["🏆 <b>Top OTP Users</b>", "", "Top 5 users by successful OTPs:"]
    if not entries:
        lines.append("")
        lines.append("No successful OTPs yet.")
        return "\n".join(lines)
    for index, entry in enumerate(entries, start=1):
        name = f"@{entry['username']}" if entry.get("username") else f"User {entry['user_id']}"
        marker = " ← You" if current_user_id and int(entry["user_id"]) == current_user_id else ""
        lines.append(f"{index}. <b>{escape(str(name))}</b> — {entry['otp_count']} OTPs{marker}")
    return "\n".join(lines)


def format_admin_stats(stats: dict[str, Any]) -> str:
    return (
        "📊 <b>Admin Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"📦 Total Orders: <b>{stats['total_orders']}</b>\n"
        f"🎁 Total Rewarded: <b>{format_currency(stats['revenue'])}</b>\n"
        f"⏳ Active Orders: <b>{stats['active_orders']}</b>\n"
        f"💸 Pending Withdrawals: <b>{stats.get('pending_withdrawals', 0)}</b>"
    )


def format_order_card(order: dict[str, Any], reveal_otp: bool = False) -> str:
    safe_region = escape(str(order["region"]))
    safe_service = escape(str(order["service"]))
    safe_message = escape(str(order["otp_message"])) if order.get("otp_message") else None
    lines = [
        f"📦 <b>Order #{order['order_id']}</b>",
        "",
        f"📞 Number: <code>{format_number(order['number'])}</code>",
        f"🌍 Region: <b>{safe_region}</b>",
        f"🔹 Service: <b>{safe_service}</b>",
        f"📌 Status: <b>{human_status(order['status'])}</b>",
        f"🕒 Created: <b>{format_iso(order['created_at'])}</b>",
    ]
    if order.get("completed_at"):
        lines.append(f"✅ Updated: <b>{format_iso(order['completed_at'])}</b>")
    if order.get("otp_message"):
        if reveal_otp:
            lines.append(f"📩 OTP Message: <code>{safe_message}</code>")
            if order.get("otp_code"):
                lines.append(f"🔐 OTP Code: <b>{order['otp_code']}</b>")
            lines.append(f"🎁 Reward Credited: <b>{format_currency(OTP_REWARD_BDT)}</b>")
        else:
            lines.append("🔒 OTP Message: <b>Locked</b>")
    else:
        lines.append("⏳ OTP Status: <b>Waiting for OTP</b>")
    return "\n".join(lines)


def build_home_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="📞 Get Number", style="primary"),
            KeyboardButton(text="🔎 Custom Range", style="success"),
        ],
        [
            KeyboardButton(text="💰 Wallet", style="success"),
            KeyboardButton(text="ℹ️ Help", style="primary"),
        ],
        [
            KeyboardButton(text="🏆 Leaderboard", style="primary"),
            KeyboardButton(text="👤 Profile", style="success"),
        ],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="🛠 Admin", style="danger")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option",
    )


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
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(text="⬅️ Prev", callback_data=f"buy:regions:{page - 1}", style="primary")
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(text="Next ➡️", callback_data=f"buy:regions:{page + 1}", style="primary")
    builder.button(text="🔎 Custom Range", callback_data="buy:custom", style="success")
    builder.button(text="🔄 Refresh", callback_data="buy:refresh", style="success")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def build_platforms_keyboard(services: list[dict[str, Any]], page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(services, page, 8)
    for service in chunk:
        builder.button(
            text=f"{service_emoji(service['name'])} {service['name']}",
            callback_data=f"buy:platform:{service['token']}:{page}",
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(text="⬅️ Prev", callback_data=f"buy:platforms:{page - 1}", style="primary")
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(text="Next ➡️", callback_data=f"buy:platforms:{page + 1}", style="primary")
    builder.button(text="🔄 Refresh", callback_data="buy:platforms_refresh", style="success")
    builder.button(text="🔎 Custom Range", callback_data="buy:custom", style="success")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def build_service_regions_keyboard(
    service: dict[str, Any], regions: list[dict[str, Any]], page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(regions, page, 8)
    for region in chunk:
        builder.button(
            text=f"{region['flag']} {region['name']}",
            callback_data=f"buy:service_region:{service['token']}:{region['code']}:{page}",
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(
                text="⬅️ Prev",
                callback_data=f"buy:platform:{service['token']}:{page - 1}",
                style="primary",
            )
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(
                text="Next ➡️",
                callback_data=f"buy:platform:{service['token']}:{page + 1}",
                style="primary",
            )
    builder.button(text="⬅️ Platforms", callback_data="buy:start", style="success")
    builder.button(text="🔎 Custom Range", callback_data="buy:custom", style="success")
    builder.adjust(1, 1, 1, 1)
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
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(
                text="⬅️ Prev", callback_data=f"buy:region:{region['code']}:{page - 1}"
                , style="primary"
            )
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(
                text="Next ➡️", callback_data=f"buy:region:{region['code']}:{page + 1}"
                , style="primary"
            )
    builder.button(text="⬅️ Regions", callback_data="buy:start", style="success")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def build_custom_range_prompt_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👀 See Ranges", url="https://t.me/NEWTON_RENGE_GROUP",style="primary")
    builder.button(text="❌ Cancel", callback_data="nav:cancel_action", style="danger")
    builder.adjust(1, 1)
    return builder.as_markup()


def build_custom_ranges_keyboard(
    matches: list[dict[str, Any]], query: str, page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(matches, page, 8)
    for match in chunk:
        label = f"{match['flag']} {match['region_name']} • {match['rid']}"
        builder.button(
            text=label,
            callback_data=f"buy:range:{match['rid']}:{page}",
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(text="⬅️ Prev", callback_data=f"buy:ranges:{query}:{page - 1}", style="primary")
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(text="Next ➡️", callback_data=f"buy:ranges:{query}:{page + 1}", style="primary")
    builder.button(text="🔎 Search Again", callback_data="buy:custom", style="success")
    builder.button(text="⬅️ Regions", callback_data="buy:start", style="success")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def build_range_services_keyboard(
    range_entry: dict[str, Any], services: list[dict[str, Any]], page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    chunk, total_pages = paginate(services, page, 8)
    for service in chunk:
        builder.button(
            text=f"{service_emoji(service['name'])} {service['name']}",
            callback_data=f"buy:range_service:{range_entry['rid']}:{service['token']}:{page}",
            style="primary",
        )
    if total_pages > 1:
        if page > 0:
            builder.button(
                text="⬅️ Prev", callback_data=f"buy:range:{range_entry['rid']}:{page - 1}"
                , style="primary"
            )
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop", style="success")
        if page < total_pages - 1:
            builder.button(
                text="Next ➡️", callback_data=f"buy:range:{range_entry['rid']}:{page + 1}"
                , style="primary"
            )
    builder.button(text="🔎 Custom Range", callback_data="buy:custom", style="success")
    builder.button(text="⬅️ Regions", callback_data="buy:start", style="success")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def build_orders_keyboard(orders: list[dict[str, Any]], kind: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.button(
            text=short_order_label(order),
            callback_data=f"order:view:{order['order_id']}",
            style="primary",
        )
    if kind == "active":
        builder.button(text="✅ Completed Orders", callback_data="nav:orders:completed", style="success")
    else:
        builder.button(text="⏳ Active Orders", callback_data="nav:orders:active", style="success")
    builder.adjust(1)
    return builder.as_markup()


def build_order_actions(order: dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if order["status"] == "waiting_otp":
        builder.button(
            text="🔄 Same Range",
            callback_data=f"order:samerange:{order['order_id']}",
            style="success",
        )
        builder.button(
            text="💬 OTP Group",
            url=OTP_GROUP_LINK,
        )
        builder.button(
            text="❌ Cancel",
            callback_data=f"order:cancel:{order['order_id']}",
            style="danger",
        )
        builder.button(text="⬅️ Back", callback_data="nav:orders", style="primary")
        builder.adjust(2, 1, 1)
    else:
        if order.get("otp_code"):
            builder.button(
                text="📋 Copy OTP",
                copy_text=CopyTextButton(text=str(order["otp_code"])),
                style="success",
            )
        builder.button(
            text="💬 OTP Group",
            url=OTP_GROUP_LINK,
        )
        if order.get("otp_code"):
            builder.button(
                text="🔄 Get Next OTP",
                callback_data=f"order:nextotp:{order['order_id']}",
                style="success",
            )
        builder.button(text="⬅️ Back", callback_data="nav:orders", style="primary")
        builder.adjust(2, 1, 1)
    return builder.as_markup()


def build_leaderboard_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh Leaderboard", callback_data="nav:leaderboard", style="success")
    builder.button(text="📦 My Orders", callback_data="nav:orders", style="primary")
    builder.adjust(1, 1)
    return builder.as_markup()


def build_wallet_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Withdraw", callback_data="wallet:withdraw", style="primary")
    builder.button(text="🪪 Change Address", callback_data="wallet:change_address", style="success")
    builder.button(text="🏆 Leaderboard", callback_data="nav:leaderboard", style="primary")
    builder.adjust(2, 1)
    return builder.as_markup()


def format_withdrawal_admin(withdrawal: dict[str, Any], user: dict[str, Any] | None) -> str:
    username = f"@{user['username']}" if user and user.get("username") else "No username"
    return (
        "💸 <b>Withdrawal Request</b>\n\n"
        f"🆔 User ID: <code>{withdrawal['user_id']}</code>\n"
        f"👤 User: <b>{escape(username)}</b>\n"
        f"💰 Amount: <b>{format_currency(float(withdrawal['amount']))}</b>\n"
        f"🏦 {WITHDRAW_NETWORK_LABEL} Address:\n<code>{escape(withdrawal['address'])}</code>\n"
        f"🕒 Requested: <b>{format_iso(withdrawal['created_at'])}</b>"
    )


def format_withdrawal_user(withdrawal: dict[str, Any], approved: bool) -> str:
    if approved:
        return (
            "✅ <b>Withdrawal Approved</b>\n\n"
            f"Amount: <b>{format_currency(float(withdrawal['amount']))}</b>\n"
            f"Destination: <code>{escape(withdrawal['address'])}</code>\n\n"
            f"Your balance has been deducted and the admin will pay your {WITHDRAW_NETWORK_LABEL} manually."
        )
    return (
        "❌ <b>Withdrawal Rejected</b>\n\n"
        f"Amount: <b>{format_currency(float(withdrawal['amount']))}</b>\n"
        "No balance was deducted. You can submit a new withdrawal request later."
    )


def build_withdrawal_review_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Approve",
        callback_data=f"withdraw:approve:{withdrawal_id}",
        style="success",
    )
    builder.button(
        text="❌ Reject",
        callback_data=f"withdraw:reject:{withdrawal_id}",
        style="danger",
    )
    builder.adjust(2)
    return builder.as_markup()


def build_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏱ Timeout", callback_data="admin:settings", style="primary")
    builder.button(text="💰 User Balances", callback_data="admin:balances", style="success")
    builder.button(text="💸 Withdrawals", callback_data="admin:withdrawals", style="success")
    builder.button(text="📦 Active Orders", callback_data="admin:orders", style="primary")
    builder.button(text="📊 Statistics", callback_data="admin:stats", style="success")
    builder.button(text="🚫 Ban User", callback_data="admin:ban", style="danger")
    builder.button(text="✅ Unban User", callback_data="admin:unban", style="success")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def build_admin_settings(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Set Timeout", callback_data="admin:set_timeout", style="primary")
    builder.button(text="⬅️ Admin Menu", callback_data="admin:menu", style="success")
    builder.adjust(1)
    return builder.as_markup()


def build_profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏳ Active Orders", callback_data="nav:orders:active", style="primary")
    builder.button(text="✅ Completed Orders", callback_data="nav:orders:completed", style="success")
    builder.adjust(1)
    return builder.as_markup()


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Cancel", callback_data="nav:cancel_action", style="danger")
    builder.adjust(1)
    return builder.as_markup()


def build_help_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Contact Admin (@dear_newton)", url="https://t.me/dear_newton",style="primary")
    builder.adjust(1)
    return builder.as_markup()
