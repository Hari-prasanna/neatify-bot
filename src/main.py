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

# --- 1. CONFIGURATION & LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# --- 2. HELPER FUNCTIONS ---

def get_supabase_client() -> Client:
    """Connects to the Supabase PostgreSQL database."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    return create_client(url, key)

def get_current_week() -> str:
    """Returns the current week (e.g., '2026-W22')."""
    return datetime.now().strftime("%Y-W%V")

# --- 3. COMMAND HANDLERS ---

async def handle_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs a cleaning task when a user says 'done'."""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    week_str = get_current_week()
    
    logger.info(f"Processing 'done' message from {user_name} ({user_id})")

    try:
        supabase = get_supabase_client()

        # A. Identity Check
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("🚫 I don't recognize your ID. Ask the admin to add you to the database.")
            return

        roomie = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        # B. Idempotency Check (Prevent double-logging)
        existing_log = supabase.table("fct_cleaning_logs") \
            .select("*") \
            .eq("roommate_id", roomie_id) \
            .eq("week_number", week_str) \
            .execute()

        if existing_log.data:
            await update.message.reply_text(f"✨ {roomie['name']}, you've already cleaned this week!")
            return

        # C. Get Task & Log Fact
        config_res = supabase.table("rotation_config") \
            .select("*, dim_tasks(task_description)") \
            .eq("roommate_id", roomie_id) \
            .execute()
        
        task = config_res.data[0]
        
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id,
            "task_id": task["task_id"],
            "week_number": week_str
        }).execute()

        # D. Next Person Logic (The Relay Race)
        current_order = task["sequence_order"]
        next_person = None
        check_order = current_order

        while not next_person:
            check_order = (check_order % 5) + 1 # Loops 1-5
            
            next_res = supabase.table("rotation_config") \
                .select("*, dim_roommates(*)") \
                .eq("sequence_order", check_order) \
                .execute()
            
            potential = next_res.data[0]["dim_roommates"]
            
            if not potential["is_on_vacation"]:
                next_person = potential

        await update.message.reply_text(
            f"✅ Success! {roomie['name']} cleaned the {task['dim_tasks']['task_description']}.\n"
            f"🔔 NEXT UP: {next_person['name']}, you are on deck for next week!"
        )

    except Exception as e:
        logger.error(f"Error in handle_done: {e}")
        await update.message.reply_text("🚨 Error saving to database. Check the logs.")

async def set_vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the user status to 'On Vacation'."""
    user_id = update.message.from_user.id
    try:
        supabase = get_supabase_client()
        supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        await update.message.reply_text("🌴 Status: On Vacation. I'll skip you until you type /back.")
    except Exception as e:
        logger.error(f"Vacation error: {e}")

async def set_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the user status to 'Active'."""
    user_id = update.message.from_user.id
    try:
        supabase = get_supabase_client()
        supabase.table("dim_roommates").update({"is_on_vacation": False}).eq("telegram_id", user_id).execute()
        await update.message.reply_text("🏠 Welcome back! You are now back in the rotation.")
    except Exception as e:
        logger.error(f"Back error: {e}")

async def get_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows current cleaning status of all roommates (Observability)."""
    try:
        supabase = get_supabase_client()
        res = supabase.table("dim_roommates").select("name, is_on_vacation").execute()
        
        status_text = "📊 **Current Status:**\n"
        for r in res.data:
            icon = "🌴" if r['is_on_vacation'] else "✅"
            status_text += f"{icon} {r['name']}\n"
        
        await update.message.reply_text(status_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Status error: {e}")
# ... (Previous imports stay the same)

async def log_to_db(level, message, script="main.py"):
    """Saves logs directly to Supabase so you can monitor the bot from anywhere."""
    try:
        supabase = get_supabase_client()
        supabase.table("sys_logs").insert({
            "log_level": level,
            "message": message,
            "script_name": script
        }).execute()
    except Exception as e:
        print(f"Failed to write to sys_logs: {e}")

async def handle_volunteer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles when someone cleans out of turn."""
    user_id = update.message.from_user.id
    week_str = get_current_week()

    try:
        supabase = get_supabase_client()
        # 1. Check if user exists
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("❌ You aren't in the database!")
            return
        
        roomie = roomie_res.data[0]

        # 2. Log as a volunteer cleaning
        # We assign them the 'Entire Home' task (Task ID 1) by default
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"],
            "task_id": 1, 
            "week_number": week_str,
            "is_volunteer": True
        }).execute()

        await log_to_db("INFO", f"Volunteer cleaning logged by {roomie['name']}")
        await update.message.reply_text(f"🌟 Legend! {roomie['name']} volunteered this week. The regular rotation remains the same!")

    except Exception as e:
        await log_to_db("ERROR", f"Volunteer command failed: {str(e)}")
        await update.message.reply_text("🚨 Snag in the volunteer logic.")

# --- Inside your main execution block, don't forget to add: ---
# app.add_handler(CommandHandler("volunteer", handle_volunteer))

# --- 4. MAIN EXECUTION ---

if __name__ == '__main__':
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID"))

    app = ApplicationBuilder().token(TOKEN).build()
    
    # 1. Message Handlers
    done_filter = filters.Chat(chat_id=GROUP_ID) & filters.Regex(r'(?i)done')
    app.add_handler(MessageHandler(done_filter, handle_done_command))
    
    # 2. Command Handlers
    app.add_handler(CommandHandler("vacation", set_vacation))
    app.add_handler(CommandHandler("back", set_back))
    app.add_handler(CommandHandler("status", get_status))
    app.add_handler(CommandHandler("volunteer", handle_volunteer))
    
    logger.info("Roomie Bot is live and listening...")
    app.run_polling()