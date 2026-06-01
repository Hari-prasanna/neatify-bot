import os
import logging
from dotenv import load_dotenv
from telegram import Bot
from supabase import create_client, Client
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

async def run_reminder():
    try:
        # Connect to the Pantry
        supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
        group_id = os.getenv("TELEGRAM_GROUP_ID")
        
        # Determine the current week (The 'Time Partition')
        week_str = datetime.now().strftime("%Y-W%V")
        
        # STEP A: Check if anyone has cleaned yet
        logs = supabase.table("fct_cleaning_logs").select("*").eq("week_number", week_str).execute()
        
        if logs.data:
            logger.info(f"Cleaning already completed for {week_str}. No reminder needed.")
            return

        # STEP B: If no log found, find out whose turn it is
        # We need to find the person whose rotation order is next based on the LAST log
        last_log = supabase.table("fct_cleaning_logs") \
            .select("roommate_id") \
            .order("cleaned_at", desc=True) \
            .limit(1).execute()
            
        # [Logic to find the next active person in the rotation_config would go here]
        # For simplicity, let's assume we fetch the 'Current assigned' person
        
        reminder_msg = "🚨 Friday Morning Reminder! The house hasn't been marked as 'done' yet. Is everything okay?"
        await bot.send_message(chat_id=group_id, text=reminder_msg)
        logger.info("Reminder sent to group.")

    except Exception as e:
        logger.error(f"Reminder Job Failed: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(run_reminder())