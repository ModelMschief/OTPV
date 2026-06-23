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


def load_known_ranges() -> set[str]:
    if not os.path.exists(JSON_FILE):
        return set()
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("known_rids", []))
    except Exception as e:
        logger.error(f"Failed to load known ranges: {e}")
        return set()


def save_known_ranges(known_rids: set[str]) -> None:
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump({"known_rids": list(known_rids)}, f)
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
            known_rids = load_known_ranges()
            catalog = await get_catalog(session, force_refresh=True)
            ranges_data = catalog.get("ranges", {})
            current_rids = set(ranges_data.keys())

            new_rids = current_rids - known_rids

            if new_rids:
                logger.info(f"Found {len(new_rids)} new ranges to post.")
                
                for rid in sorted(list(new_rids)):
                    range_entry = ranges_data[rid]
                    flag = range_entry.get("flag", "🌍")
                    region_name = range_entry.get("region_name", "Unknown")
                    rid_val = range_entry.get("rid", rid)
                    services_list = list(range_entry.get("services", []))
                    
                    if services_list:
                        services_str = ", ".join(s.title() for s in services_list)
                        example_service = services_list[0].title()
                    else:
                        services_str = "Any Supported Service"
                        example_service = "App"
                        
                    example_sms = f"{example_service} code: 84920"
                    
                    text = (
                        "🌍 <b>New Range Available!</b>\n\n"
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
                    
                    known_rids.add(rid)
                    save_known_ranges(known_rids)
                    
                    await asyncio.sleep(SEND_DELAY_SECONDS)
                
                logger.info("Finished posting new ranges.")
            else:
                logger.debug("No new ranges found.")
                
        except Exception as e:
            logger.exception("Unexpected error in range notifier worker.")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_range_notifier(bot: Bot, session: aiohttp.ClientSession) -> asyncio.Task:
    return asyncio.create_task(_notifier_worker(bot, session))
