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
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_current_week() -> str:
    """Returns the current week (e.g., '2026-W23')."""
    return datetime.now().strftime("%Y-W%V")

def get_last_cleaned_date(supabase, roommate_id):
    """Fetch the most recent cleaning date using 'cleaned_at'."""
    res = supabase.table("fct_cleaning_logs") \
        .select("cleaned_at") \
        .eq("roommate_id", roommate_id) \
        .order("cleaned_at", desc=True) \
        .limit(1) \
        .execute()
    
    if res.data:
        # Splitting the ISO timestamp to get the date part
        raw_date = res.data[0]['cleaned_at'].split("T")[0]
        return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %b")
    return "Never"

# --- 3. COMMAND HANDLERS ---

async def handle_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs a cleaning task when a user says 'done'."""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    week_str = get_current_week()
    
    try:
        supabase = get_supabase_client()
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        
        if not roomie_res.data:
            await update.message.reply_text("🚫 Unrecognized ID. Please register with the admin.")
            return

        roomie = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        # Check for duplicate logs in the same week
        existing = supabase.table("fct_cleaning_logs") \
            .select("*") \
            .eq("roommate_id", roomie_id) \
            .eq("week_number", week_str) \
            .execute()

        if existing.data:
            await update.message.reply_text(f"✨ {roomie['name']}, you already cleaned for {week_str}!")
            return

        # Fetch current task
        config_res = supabase.table("rotation_config") \
            .select("*, dim_tasks(task_description)") \
            .eq("roommate_id", roomie_id) \
            .execute()
        
        task = config_res.data[0]
        
        # Insert the log (Supabase will auto-fill 'cleaned_at')
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, 
            "task_id": task["task_id"], 
            "week_number": week_str
        }).execute()

        # Relay Logic: Find next person
        check_order = task["sequence_order"]
        next_person = None
        while not next_person:
            check_order = (check_order % 5) + 1
            next_res = supabase.table("rotation_config") \
                .select("*, dim_roommates(*)") \
                .eq("sequence_order", check_order) \
                .execute()
            
            p = next_res.data[0]["dim_roommates"]
            if not p["is_on_vacation"]:
                next_person = p

        await update.message.reply_text(
            f"✅ Success! {roomie['name']} cleaned: {task['dim_tasks']['task_description']}.\n"
            f"🔔 NEXT: {next_person['name']} is up for next week!"
        )

    except Exception as e:
        logger.error(f"Error in handle_done: {e}")

async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculates who is on deck based on the last 'cleaned_at' record."""
    try:
        supabase = get_supabase_client()
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
        current_order = current_order_res.data[0]['sequence_order']

        next_person = None
        check_order = current_order
        while not next_person:
            check_order = (check_order % 5) + 1
            res = supabase.table("rotation_config") \
                .select("*, dim_roommates(*), dim_tasks(*)") \
                .eq("sequence_order", check_order) \
                .execute()
            
            potential = res.data[0]
            if not potential["dim_roommates"]["is_on_vacation"]:
                next_person = potential

        await update.message.reply_text(
            f"📅 **Current Week:** `{get_current_week()}`\n"
            f"🔔 **Upcoming:** {next_person['dim_roommates']['name']}\n"
            f"🧹 **Task:** {next_person['dim_tasks']['task_description']}",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in handle_next: {e}")

async def get_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Observability: Shows status and last cleaned date for everyone."""
    try:
        supabase = get_supabase_client()
        res = supabase.table("dim_roommates").select("roommate_id, name, is_on_vacation").execute()
        
        status_text = "📊 **Roommate Status**\n\n"
        for r in res.data:
            last_date = get_last_cleaned_date(supabase, r['roommate_id'])
            icon = "🌴" if r['is_on_vacation'] else "✅"
            status_text += f"{icon} **{r['name']}**\n└ Last: `{last_date}`\n"
        
        await update.message.reply_text(status_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Status error: {e}")

async def handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audit: Shows the 3 most recent cleaning logs."""
    try:
        supabase = get_supabase_client()
        res = supabase.table("fct_cleaning_logs") \
            .select("*, dim_roommates(name), dim_tasks(task_description)") \
            .order("cleaned_at", desc=True) \
            .limit(3) \
            .execute()

        if not res.data:
            await update.message.reply_text("📭 No logs found.")
            return

        msg = "🕒 **Recent Activity:**\n"
        for log in res.data:
            date_fmt = datetime.strptime(log['cleaned_at'].split("T")[0], "%Y-%m-%d").strftime("%d %b")
            msg += f"• `{date_fmt}`: {log['dim_roommates']['name']} ({log['dim_tasks']['task_description']})\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Last error: {e}")

async def set_vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        get_supabase_client().table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        await update.message.reply_text("🌴 Status updated: You're on vacation!")
    except Exception as e:
        logger.error(f"Vacation error: {e}")

async def set_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        get_supabase_client().table("dim_roommates").update({"is_on_vacation": False}).eq("telegram_id", user_id).execute()
        await update.message.reply_text("🏠 Welcome back! You're in the rotation.")
    except Exception as e:
        logger.error(f"Back error: {e}")

async def handle_volunteer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        supabase = get_supabase_client()
        roomie = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute().data[0]
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"], 
            "task_id": 1, 
            "week_number": get_current_week(), 
            "is_volunteer": True
        }).execute()
        await update.message.reply_text(f"🌟 {roomie['name']} just volunteered! Hero move.")
    except Exception as e:
        logger.error(f"Volunteer error: {e}")

# --- 4. MAIN EXECUTION ---

if __name__ == '__main__':
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID"))

    app = ApplicationBuilder().token(TOKEN).build()
    
    # Logic: Regex to trigger 'done' handler
    done_filter = filters.Chat(chat_id=GROUP_ID) & filters.Regex(r'(?i)done')
    app.add_handler(MessageHandler(done_filter, handle_done_command))
    
    # Command List
    app.add_handler(CommandHandler("status", get_status))
    app.add_handler(CommandHandler("next", handle_next))
    app.add_handler(CommandHandler("last", handle_last))
    app.add_handler(CommandHandler("vacation", set_vacation))
    app.add_handler(CommandHandler("back", set_back))
    app.add_handler(CommandHandler("volunteer", handle_volunteer))
    
    logger.info("Roomie Bot (Local) is live...")
    app.run_polling()