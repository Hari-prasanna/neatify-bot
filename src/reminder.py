# src/reminder.py — Friday reminder cron. Run by GitHub Actions delivery.yml every Friday 07:00 UTC.
# Checks only Task 1 (Entire Home) — Bathroom track is never included in this reminder.

import os
import sys
import json
import logging
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import supabase, get_current_week, log_to_db, find_next_person, mention_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder")

ENTIRE_HOME_TASK_ID = 1


def run_reminder() -> None:
    token    = os.environ.get("TELEGRAM_TOKEN")
    group_id = os.environ.get("TELEGRAM_GROUP_ID")

    if not token or not group_id:
        logger.error("Missing TELEGRAM_TOKEN or TELEGRAM_GROUP_ID.")
        sys.exit(1)

    week_str    = get_current_week()
    next_config = find_next_person(ENTIRE_HOME_TASK_ID)

    if not next_config:
        msg = f"Friday reminder skipped — no eligible person for task_id={ENTIRE_HOME_TASK_ID} in {week_str}."
        logger.warning(msg)
        log_to_db("WARNING", msg)
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

    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": int(group_id), "text": message, "parse_mode": "HTML"}

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            telegram_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as response:
            if response.status == 200:
                logger.info(f"Reminder sent: {roomie['name']} / {week_str}")
                log_to_db("INFO",
                          f"Friday reminder sent: {roomie['name']} / "
                          f"{task['task_description']} / {week_str}")
            else:
                logger.error(f"Telegram returned status: {response.status}")
                sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")
        log_to_db("ERROR", "Friday reminder send failed", error_details=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run_reminder()
