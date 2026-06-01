import os
import logging
import asyncio
from datetime import datetime
from telegram import Bot
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_reminder():
    # 1. Access the secrets directly from the environment
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
    GROUP_ID = os.environ.get("TELEGRAM_GROUP_ID")

    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        bot = Bot(token=TELEGRAM_TOKEN)
        week_str = datetime.now().strftime("%Y-W%V")
        
        # 2. Check if anyone has cleaned this week
        logs = supabase.table("fct_cleaning_logs").select("*").eq("week_number", week_str).execute()
        
        if not logs.data:
            # 3. If no one cleaned, send the nudge
            await bot.send_message(
                chat_id=GROUP_ID, 
                text=f"📢 Happy Friday! The house hasn't been marked as 'done' for week {week_str} yet. Who's on it? 🧼"
            )
            logger.info("Reminder sent.")
        else:
            logger.info("Cleaning already logged. Skipping reminder.")

    except Exception as e:
        logger.error(f"Failed to run reminder: {e}")

if __name__ == "__main__":
    asyncio.run(run_reminder())