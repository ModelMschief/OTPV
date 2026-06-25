import asyncio
import json
import logging
import os
from contextlib import suppress

import aiohttp
from aiogram import Bot
from aiogram.types import CopyTextButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utility import get_catalog

logger = logging.getLogger("range-notifier")

NOTIFIER_GROUP_ID = "@NEWTON_RENGE_GROUP"
JSON_FILE = "known_ranges.json"
CHECK_INTERVAL_SECONDS = 3600  # 1 hour
SEND_DELAY_SECONDS = 5


def load_known_ranges() -> dict[str, list[str]]:
    if not os.path.exists(JSON_FILE):
        return {}
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("known_ranges", {})
    except Exception as e:
        logger.error(f"Failed to load known ranges: {e}")
        return {}


def save_known_ranges(known_ranges: dict[str, list[str]]) -> None:
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump({"known_ranges": known_ranges}, f)
    except Exception as e:
        logger.error(f"Failed to save known ranges: {e}")


def build_range_message_keyboard(rid: str, country: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Copy Range", copy_text=CopyTextButton(text=rid))
    builder.button(text="📋 Copy Country", copy_text=CopyTextButton(text=country))
    builder.adjust(2)
    return builder.as_markup()


async def _notifier_worker(bot: Bot, session: aiohttp.ClientSession) -> None:
    logger.info("Range notifier worker started.")
    while True:
        try:
            known_ranges_dict = load_known_ranges()
            catalog = await get_catalog(session, force_refresh=True)
            ranges_data = catalog.get("ranges", {})
            
            updates_found = False
            
            for rid, range_entry in ranges_data.items():
                current_services = set(range_entry.get("services", []))
                known_services = set(known_ranges_dict.get(rid, []))
                
                new_services = current_services - known_services
                
                if new_services:
                    updates_found = True
                    logger.info(f"Found new services for {rid}: {new_services}")
                    
                    flag = range_entry.get("flag", "🌍")
                    region_name = range_entry.get("region_name", "Unknown")
                    rid_val = range_entry.get("rid", rid)
                    
                    is_new_range = len(known_services) == 0
                    title = "🌍 <b>New Range Available!</b>" if is_new_range else "🌍 <b>New Platforms Supported!</b>"
                    
                    services_str = ", ".join(s.title() for s in current_services)
                    example_service = list(new_services)[0].title()
                    example_sms = f"{example_service} code: 84920"
                    
                    text = (
                        f"{title}\n\n"
                        "<blockquote>"
                        f"🏳️ <b>Country:</b> {flag} {region_name}\n"
                        f"🔢 <b>Range:</b> <code>{rid_val}xxx</code>\n"
                        f"📱 <b>Platforms:</b> {services_str}\n"
                        f"💬 <b>Example SMS:</b> <i>{example_sms}</i>\n"
                        "</blockquote>"
                    )
                    markup = build_range_message_keyboard(rid_val, region_name)
                    
                    try:
                        await bot.send_message(NOTIFIER_GROUP_ID, text, reply_markup=markup)
                    except Exception as send_err:
                        logger.error(f"Failed to send range {rid_val} to group: {send_err}")
                    
                    known_ranges_dict[rid] = list(current_services)
                    save_known_ranges(known_ranges_dict)
                    
                    await asyncio.sleep(SEND_DELAY_SECONDS)
                    
            if updates_found:
                logger.info("Finished posting range updates.")
            else:
                logger.debug("No new range updates found.")
                
        except Exception as e:
            logger.exception("Unexpected error in range notifier worker.")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_range_notifier(bot: Bot, session: aiohttp.ClientSession) -> asyncio.Task:
    return asyncio.create_task(_notifier_worker(bot, session))
