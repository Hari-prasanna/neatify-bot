# src/reminder.py — Friday reminder cron. Run by GitHub Actions delivery.yml every Friday 07:00 UTC.
# Checks only Task 1 (Entire Home) — Bathroom track is never included in this reminder.
#
# Sends two messages:
#   1. Group broadcast — visible to everyone, shows who is on deck
#   2. Private DM    — personal nudge directly to the scheduled person (if telegram_id is known)

import os
import sys
import json
import logging
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import (
    supabase, get_current_week, get_weekend_dates_from_week,
    log_to_db, peek_next_n_persons, mention_user,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder")

ENTIRE_HOME_TASK_ID = 1


def _send(token: str, chat_id: int, text: str) -> bool:
    """POST a message to Telegram. Returns True on success."""
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Telegram send failed (chat_id={chat_id}): {e}")
        return False


def run_reminder() -> None:
    token    = os.environ.get("TELEGRAM_TOKEN")
    group_id = os.environ.get("TELEGRAM_GROUP_ID")

    if not token or not group_id:
        logger.error("Missing TELEGRAM_TOKEN or TELEGRAM_GROUP_ID.")
        sys.exit(1)

    week_str     = get_current_week()
    next_persons = peek_next_n_persons(ENTIRE_HOME_TASK_ID, 1)

    if not next_persons:
        msg = f"Friday reminder skipped — no eligible person for task_id={ENTIRE_HOME_TASK_ID} in {week_str}."
        logger.warning(msg)
        log_to_db("WARNING", msg)
        return

    roomie  = next_persons[0]
    handle  = mention_user(roomie)
    weekend = get_weekend_dates_from_week(week_str)

    # Skip entirely if the person already logged done this week
    try:
        done_check = (
            supabase.table("fct_cleaning_logs").select("log_id")
            .eq("roommate_id", roomie["roommate_id"])
            .eq("task_id", ENTIRE_HOME_TASK_ID)
            .eq("week_number", week_str)
            .eq("is_volunteer", False)
            .execute()
        )
        if done_check.data:
            info = f"Friday reminder skipped — {roomie['name']} already logged done for {week_str}."
            logger.info(info)
            log_to_db("INFO", info)
            return
    except Exception as e:
        log_to_db("WARNING", f"Friday reminder done-check failed: {e}")

    # ── 1. Group broadcast ────────────────────────────────────────────────────
    group_msg = (
        f"🧹 <b>Friday Reminder</b>\n\n"
        f"• Week: <code>{week_str}</code>\n"
        f"• Dates: {weekend}\n"
        f"• Task: Entire Home\n"
        f"• On deck: {handle}\n\n"
        f"📱 Send <b>done</b> as a private message to the bot when finished!"
    )

    ok = _send(token, int(group_id), group_msg)
    if ok:
        logger.info(f"Group reminder sent: {roomie['name']} / {week_str}")
        log_to_db("INFO",
                  f"Friday reminder sent (group): {roomie['name']} / Entire Home / {week_str}")
    else:
        logger.error("Group reminder failed.")
        log_to_db("ERROR", "Friday reminder group send failed")
        sys.exit(1)

    # ── 2. Private DM to the scheduled person ─────────────────────────────────
    # Only possible if they've previously messaged the bot (telegram_id known).
    personal_tg_id = roomie.get("telegram_id")
    if not personal_tg_id:
        logger.info(f"Skipping private DM — no telegram_id for {roomie['name']}.")
        return

    dm_msg = (
        f"🧹 <b>Hey {roomie['name']}!</b>\n\n"
        f"You're on deck this weekend.\n"
        f"• Task: Entire Home\n"
        f"• Dates: {weekend}\n\n"
        f"Send <b>done</b> here when you're finished and I'll update the group. 🙌"
    )

    dm_ok = _send(token, int(personal_tg_id), dm_msg)
    if dm_ok:
        logger.info(f"Private DM sent to {roomie['name']} (tg_id={personal_tg_id})")
        log_to_db("INFO", f"Friday reminder sent (DM): {roomie['name']} / {week_str}")
    else:
        # DM failure is non-fatal — group message already went through
        logger.warning(f"Private DM failed for {roomie['name']} (tg_id={personal_tg_id})")
        log_to_db("WARNING",
                  f"Friday reminder DM failed: {roomie['name']} (tg_id={personal_tg_id})")


if __name__ == "__main__":
    run_reminder()
