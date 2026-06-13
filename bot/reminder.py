"""
bot/reminder.py — Weekly Friday cleaning reminder.

This script is run by GitHub Actions every Friday at 07:00 UTC.
Think of it as an alarm clock: it wakes up, checks who's next in the
rotation, and sends one Telegram message to the group, then exits.

It uses Python's built-in urllib so no extra HTTP library is needed.
"""

import os
import sys
import json
import logging
import urllib.request
from datetime import datetime
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder")


def run_reminder():
    # Pull credentials from environment (injected by GitHub Actions secrets)
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    token = os.getenv("TELEGRAM_TOKEN")
    group_id = os.getenv("TELEGRAM_GROUP_ID")

    if not all([url, key, token, group_id]):
        logger.error("Missing environment variables. Check GitHub Actions secrets.")
        sys.exit(1)

    supabase: Client = create_client(url, key)

    # --- Step 1: Find who cleaned last (the rotation cursor) ---
    # Ignore volunteer entries so the cursor only advances on scheduled cleans.
    # Like reading only the official entries in a logbook, ignoring the bonus ones.
    last_log = (
        supabase.table("fct_cleaning_logs")
        .select("roommate_id")
        .eq("is_volunteer", False)
        .order("cleaned_at", desc=True)
        .limit(1)
        .execute()
    )

    if not last_log.data:
        logger.warning("No cleaning history found in fct_cleaning_logs. Nothing to remind about.")
        return

    last_id = last_log.data[0]["roommate_id"]

    # --- Step 2: Find that person's position in the rotation queue ---
    order_res = (
        supabase.table("rotation_config")
        .select("sequence_order")
        .eq("roommate_id", last_id)
        .execute()
    )
    if not order_res.data:
        logger.error(f"No rotation config found for roommate_id={last_id}.")
        sys.exit(1)

    current_order = order_res.data[0]["sequence_order"]

    # --- Step 3: Walk forward in the queue to find the next active person ---
    # The queue is circular (1 → 2 → ... → 5 → 1). Skip anyone on vacation.
    next_person = None
    check_order = current_order
    attempts = 0

    while not next_person and attempts < 10:
        check_order = (check_order % 5) + 1
        attempts += 1
        res = (
            supabase.table("rotation_config")
            .select("*, dim_roommates(*), dim_tasks(*)")
            .eq("sequence_order", check_order)
            .execute()
        )
        if res.data:
            potential = res.data[0]
            if not potential["dim_roommates"]["is_on_vacation"]:
                next_person = potential

    if not next_person:
        logger.warning("All roommates are on vacation. Skipping reminder.")
        return

    week_str = datetime.now().strftime("%Y-W%V")

    # --- Step 4: Build the reminder message ---
    message = (
        f"🧹 *Friday Cleaning Reminder!* 🧹\n\n"
        f"📅 *Week:* `{week_str}`\n"
        f"🔔 *On Deck:* {next_person['dim_roommates']['name']}\n"
        f"🧹 *Assigned Task:* {next_person['dim_tasks']['task_description']}\n\n"
        f"Let's keep the house fresh! Drop a 'done' message when finished."
    )

    # --- Step 5: Send message via Telegram Bot API ---
    # Using urllib.request instead of the requests library to keep dependencies minimal.
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(group_id),
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            telegram_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as response:
            if response.status == 200:
                logger.info(f"Reminder sent to group {group_id} for week {week_str}.")
            else:
                logger.error(f"Telegram returned unexpected status: {response.status}")
                sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_reminder()
