# src/reminder.py — Friday reminder cron. Run by GitHub Actions delivery.yml every Friday 07:00 UTC.
# Checks only Task 1 (Entire Home) — Bathroom track is never included in this reminder.

import os
import sys
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram

from src.constants import TASK_ENTIRE_HOME
from src.utils import get_current_week, log_to_db, find_next_person, mention_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder")

_TELEGRAM_SEND_TIMEOUT: float = 10.0


async def _send_reminder_message(token: str, group_id: int, message: str) -> None:
    bot = telegram.Bot(token=token)
    async with bot:
        await bot.send_message(
            chat_id=group_id, text=message, parse_mode="HTML",
            read_timeout=_TELEGRAM_SEND_TIMEOUT,
        )


def run_reminder() -> None:
    token    = os.environ.get("TELEGRAM_TOKEN")
    group_id = os.environ.get("TELEGRAM_GROUP_ID")

    if not token or not group_id:
        logger.error("Missing TELEGRAM_TOKEN or TELEGRAM_GROUP_ID.")
        sys.exit(1)

    week_str    = get_current_week()
    next_config = find_next_person(TASK_ENTIRE_HOME)

    if not next_config:
        skipped_msg = f"Friday reminder skipped — no eligible person for task_id={TASK_ENTIRE_HOME} in {week_str}."
        logger.warning(skipped_msg)
        log_to_db("WARNING", skipped_msg)
        return

    roomie = next_config["dim_roommates"]
    task   = next_config["dim_tasks"]
    handle = mention_user(roomie)

    message = (
        f"🧹 <b>Friday Reminder</b>\n\n"
        f"• Week: <code>{week_str}</code>\n"
        f"• Task: {task['task_description']}\n"
        f"• On deck: {handle}\n\n"
        f"Reply <b>done</b> in the group when finished! 🙌"
    )

    try:
        asyncio.run(_send_reminder_message(token, int(group_id), message))
        logger.info(f"Reminder sent: {roomie['name']} / {week_str}")
        log_to_db("INFO",
                  f"Friday reminder sent: {roomie['name']} / "
                  f"{task['task_description']} / {week_str}")
    except Exception as reminder_error:
        logger.error(f"Failed to send reminder: {reminder_error}")
        log_to_db("ERROR", "Friday reminder send failed", error_details=str(reminder_error))
        sys.exit(1)


if __name__ == "__main__":
    run_reminder()
