import os
import sys
import logging
import urllib.request
import json
from datetime import datetime
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reminder_script")

def run_reminder():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    token = os.getenv("TELEGRAM_TOKEN")
    group_id = os.getenv("TELEGRAM_GROUP_ID")

    if not all([url, key, token, group_id]):
        logger.error("Missing environment variables.")
        sys.exit(1)

    supabase: Client = create_client(url, key)

    # Fetch last log using your corrected database schema column: cleaned_at
    last_log = supabase.table("fct_cleaning_logs") \
        .select("roommate_id") \
        .eq("is_volunteer", False) \
        .order("cleaned_at", desc=True) \
        .limit(1).execute()
    
    if not last_log.data:
        logger.warning("No history found in fct_cleaning_logs.")
        return

    last_id = last_log.data[0]['roommate_id']
    current_order = supabase.table("rotation_config").select("sequence_order").eq("roommate_id", last_id).execute().data[0]['sequence_order']

    next_person = None
    check_order = current_order
    while not next_person:
        check_order = (check_order % 5) + 1
        res = supabase.table("rotation_config").select("*, dim_roommates(*), dim_tasks(*)").eq("sequence_order", check_order).execute()
        potential = res.data[0]
        if not potential["dim_roommates"]["is_on_vacation"]:
            next_person = potential

    week_str = datetime.now().strftime("%Y-W%V")
    message = (
        f"🧹 **Friday Cleaning Reminder!** 🧹\n\n"
        f"📅 **Week:** `{week_str}`\n"
        f"🔔 **On Deck:** {next_person['dim_roommates']['name']}\n"
        f"🧹 **Assigned Task:** {next_person['dim_tasks']['task_description']}\n\n"
        f"Let's keep the house fresh! Please drop a 'done' message when finished."
    )

    # Using Python's built-in urllib to avoid adding extra dependencies to requirements.txt
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(group_id),
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            telegram_url, 
            data=data, 
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10.0) as response:
            if response.status == 200:
                logger.info("Reminder notification sent successfully!")
            else:
                logger.error(f"Telegram returned status: {response.status}")
                sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_reminder()