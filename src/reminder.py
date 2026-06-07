import os
import sys
import logging
from datetime import datetime
import httpx
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder_script")

def run_reminder():
    # 1. Initialize Clients
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    token = os.getenv("TELEGRAM_TOKEN")
    group_id = os.getenv("TELEGRAM_GROUP_ID")

    if not all([url, key, token, group_id]):
        logger.error("Missing environment variables.")
        sys.exit(1)

    supabase: Client = create_client(url, key)

    # 2. Extract Last Cleaned Using Correct Schema
    last_log = supabase.table("fct_cleaning_logs") \
        .select("roommate_id") \
        .eq("is_volunteer", False) \
        .order("cleaned_at", desc=True) \
        .limit(1).execute()
    
    if not last_log.data:
        logger.warning("No cleaning history found in fct_cleaning_logs. Cannot calculate next person.")
        return

    last_id = last_log.data[0]['roommate_id']
    
    # 3. Process Sequence Relay
    current_order = supabase.table("rotation_config").select("sequence_order").eq("roommate_id", last_id).execute().data[0]['sequence_order']

    next_person = None
    check_order = current_order
    while not next_person:
        check_order = (check_order % 5) + 1
        res = supabase.table("rotation_config").select("*, dim_roommates(*), dim_tasks(*)").eq("sequence_order", check_order).execute()
        potential = res.data[0]
        if not potential["dim_roommates"]["is_on_vacation"]:
            next_person = potential

    # 4. Construct Payload
    week_str = datetime.now().strftime("%Y-W%V")
    message = (
        f"🧹 **Friday Cleaning Reminder!** 🧹\n\n"
        f"📅 **Week:** `{week_str}`\n"
        f"🔔 **On Deck:** {next_person['dim_roommates']['name']}\n"
        f"🧹 **Assigned Task:** {next_person['dim_tasks']['task_description']}\n\n"
        f"Let's keep the house fresh! Please drop a 'done' message when finished."
    )

    # 5. Push Direct to Telegram via Synchronous HTTP POST
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(group_id),
        "text": message,
        "parse_mode": "Markdown"
    }
    
    response = httpx.post(telegram_url, json=payload, timeout=10.0)
    
    if response.status_code == 200:
        logger.info("Reminder notification sent successfully!")
    else:
        logger.error(f"Telegram API Rejected Request: {response.text}")
        sys.exit(1)

if __name__ == "__main__":
    run_reminder()
