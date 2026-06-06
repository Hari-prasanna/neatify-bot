import os
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# Telegram Imports
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# Database Imports
from supabase import create_client, Client

# --- 1. CONFIGURATION, ENV, & LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# Global configuration verification
TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_ID_STR = os.getenv("TELEGRAM_GROUP_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TOKEN, GROUP_ID_STR, SUPABASE_URL, SUPABASE_KEY]):
    logger.critical("Missing vital Environment Variables! Check your .env file.")
    exit(1)

GROUP_ID = int(GROUP_ID_STR)

# Initialize Supabase client globally to reuse connection pool efficiently
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- 2. HELPER FUNCTIONS ---

def get_current_week() -> str:
    """Returns the current week (e.g., '2026-W23')."""
    return datetime.now().strftime("%Y-W%V")

def get_last_cleaned_date(roommate_id: int) -> str:
    """Fetch the most recent cleaning date using 'cleaned_at'."""
    try:
        res = supabase.table("fct_cleaning_logs") \
            .select("cleaned_at") \
            .eq("roommate_id", roommate_id) \
            .order("cleaned_at", desc=True) \
            .limit(1) \
            .execute()
        
        if res.data:
            raw_date = res.data[0]['cleaned_at'].split("T")[0]
            return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %b")
    except Exception as e:
        logger.error(f"Error fetching last cleaned date for roomie {roommate_id}: {e}")
    return "Never"

# --- 3. COMMAND HANDLERS ---

async def handle_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs a cleaning task when a user says 'done'."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    week_str = get_current_week()
    
    try:
        # Check user registration
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("🚫 Unrecognized ID. Please register with the admin.")
            return

        roomie = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        # Duplicate Prevention
        existing = supabase.table("fct_cleaning_logs") \
            .select("*") \
            .eq("roommate_id", roomie_id) \
            .eq("week_number", week_str) \
            .execute()

        if existing.data:
            await update.message.reply_text(f"✨ {roomie['name']}, you already logged cleaning for {week_str}!")
            return

        # Fetch current assigned task
        config_res = supabase.table("rotation_config") \
            .select("*, dim_tasks(task_description)") \
            .eq("roommate_id", roomie_id) \
            .execute()
        
        if not config_res.data:
            await update.message.reply_text("⚠️ You don't have an assigned rotation task in the config table.")
            return
            
        task = config_res.data[0]
        
        # Log execution
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, 
            "task_id": task["task_id"], 
            "week_number": week_str
        }).execute()

        # Relay Logic: Safe loop breakout included
        check_order = task["sequence_order"]
        next_person = None
        attempts = 0
        
        while not next_person and attempts < 10:
            check_order = (check_order % 5) + 1
            attempts += 1
            next_res = supabase.table("rotation_config") \
                .select("*, dim_roommates(*)") \
                .eq("sequence_order", check_order) \
                .execute()
            
            if next_res.data:
                p = next_res.data[0]["dim_roommates"]
                if not p["is_on_vacation"]:
                    next_person = p

        next_msg = f"🔔 NEXT: {next_person['name']} is up for next week!" if next_person else "🔔 NEXT: No active roommates available (all on vacation!)."

        await update.message.reply_text(
            f"✅ Success! {roomie['name']} cleaned: {task['dim_tasks']['task_description']}.\n" + next_msg
        )

    except Exception as e:
        logger.error(f"Error in handle_done: {e}")
        await update.message.reply_text("❌ An internal database error occurred while processing your completion.")

async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculates who is on deck based on the last non-volunteer record."""
    try:
        last_log = supabase.table("fct_cleaning_logs") \
            .select("roommate_id") \
            .eq("is_volunteer", False) \
            .order("cleaned_at", desc=True) \
            .limit(1).execute()
        
        if not last_log.data:
            await update.message.reply_text("🤔 No history found. Start the rotation with 'done'!")
            return

        last_id = last_log.data[0]['roommate_id']
        current_order_res = supabase.table("rotation_config").select("sequence_order").eq("roommate_id", last_id).execute()
        
        if not current_order_res.data:
            await update.message.reply_text("⚠️ Rotation queue breakdown. Last person to clean is missing configuration setup.")
            return
            
        current_order = current_order_res.data[0]['sequence_order']

        next_person = None
        check_order = current_order
        attempts = 0
        
        while not next_person and attempts < 10:
            check_order = (check_order % 5) + 1
            attempts += 1
            res = supabase.table("rotation_config") \
                .select("*, dim_roommates(*), dim_tasks(*)") \
                .eq("sequence_order", check_order) \
                .execute()
            
            if res.data:
                potential = res.data[0]
                if not potential["dim_roommates"]["is_on_vacation"]:
                    next_person = potential

        if not next_person:
            await update.message.reply_text("🌴 Everyone seems to be on vacation right now!")
            return

        await update.message.reply_text(
            f"📅 **Current Week:** `{get_current_week()}`\n"
            f"🔔 **Upcoming:** {next_person['dim_roommates']['name']}\n"
            f"🧹 **Task:** {next_person['dim_tasks']['task_description']}",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in handle_next: {e}")
        await update.message.reply_text("❌ Failed to query upcoming schedule.")

async def get_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows active status and historical metrics for clarity."""
    try:
        res = supabase.table("dim_roommates").select("roommate_id, name, is_on_vacation").order("name").execute()
        
        status_text = "📊 **Roommate Status**\n\n"
        for r in res.data:
            last_date = get_last_cleaned_date(r['roommate_id'])
            icon = "🌴 [Vacation]" if r['is_on_vacation'] else "✅ [Active]"
            status_text += f"{icon} **{r['name']}**\n└ Last Cleaned: `{last_date}`\n\n"
        
        await update.message.reply_text(status_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Status error: {e}")
        await update.message.reply_text("❌ Failed to pull roster status.")

async def handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audit: Shows the 3 most recent cleaning logs."""
    try:
        res = supabase.table("fct_cleaning_logs") \
            .select("*, dim_roommates(name), dim_tasks(task_description)") \
            .order("cleaned_at", desc=True) \
            .limit(3) \
            .execute()

        if not res.data:
            await update.message.reply_text("📭 No logs found in history profiles.")
            return

        msg = "🕒 **Recent Activity:**\n"
        for log in res.data:
            date_fmt = datetime.strptime(log['cleaned_at'].split("T")[0], "%Y-%m-%d").strftime("%d %b")
            roomie_name = log['dim_roommates']['name'] if log.get('dim_roommates') else "Unknown"
            task_desc = log['dim_tasks']['task_description'] if log.get('dim_tasks') else "General Duty"
            msg += f"• `{date_fmt}`: {roomie_name} ({task_desc})\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Last error: {e}")
        await update.message.reply_text("❌ Failed to parse chronological audit trail logs.")

async def set_vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles roommate status to on-vacation."""
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        if res.data:
            await update.message.reply_text("🌴 Status updated: You're officially off the hook for now!")
        else:
            await update.message.reply_text("❌ Telegram user ID not registered in database profiles.")
    except Exception as e:
        logger.error(f"Vacation error: {e}")

async def set_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles roommate status back to active duty rotation."""
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": False}).eq("telegram_id", user_id).execute()
        if res.data:
            await update.message.reply_text("🏠 Welcome back! You are queued into future active loops.")
        else:
            await update.message.reply_text("❌ Telegram user ID not registered in database profiles.")
    except Exception as e:
        logger.error(f"Back error: {e}")

async def handle_volunteer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves a standalone volunteer exception cleaning event log."""
    user_id = update.message.from_user.id
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("🚫 Unrecognized ID. Please register with the admin to volunteer.")
            return
            
        roomie = roomie_res.data[0]
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"], 
            "task_id": 1, # Default placeholder fallback task 
            "week_number": get_current_week(), 
            "is_volunteer": True
        }).execute()
        await update.message.reply_text(f"🌟 {roomie['name']} just stepped up! Massive respect.")
    except Exception as e:
        logger.error(f"Volunteer error: {e}")
        await update.message.reply_text("❌ An entry error prevented registration of your volunteer activity.")

# --- 4. MAIN RUNNER ---

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Context match configuration: filters by target group and regex phrase "done"
    done_filter = filters.Chat(chat_id=GROUP_ID) & filters.Regex(r'(?i)\bdone\b')
    app.add_handler(MessageHandler(done_filter, handle_done_command))
    
    # Register core standard handlers
    app.add_handler(CommandHandler("status", get_status))
    app.add_handler(CommandHandler("next", handle_next))
    app.add_handler(CommandHandler("last", handle_last))
    app.add_handler(CommandHandler("vacation", set_vacation))
    app.add_handler(CommandHandler("back", set_back))
    app.add_handler(CommandHandler("volunteer", handle_volunteer))
    
    logger.info("Roomie Bot is officially operational...")
    app.run_polling()
