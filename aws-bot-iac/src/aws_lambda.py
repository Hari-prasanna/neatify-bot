import os
import json
import logging
from datetime import datetime
import telegram
from telegram.request import HTTPXRequest
from supabase import create_client, Client

# --- 1. LOGGING SETUP ---
# CloudWatch will capture these logs for your 'Black Box' monitoring
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- 2. HELPER FUNCTIONS ---

def get_supabase_client() -> Client:
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def get_current_week() -> str:
    return datetime.now().strftime("%Y-W%V")

async def process_update(event):
    supabase = get_supabase_client()
    
    # Using a slightly longer connect_timeout for the cloud network
    t_request = HTTPXRequest(connection_pool_size=8, connect_timeout=15.0)
    bot = telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN"), request=t_request)

async def log_to_sys_logs(supabase, level, message):
    """Internal audit logging for system health stored in Supabase."""
    try:
        supabase.table("sys_logs").insert({
            "log_level": level,
            "message": message,
            "script_name": "aws_lambda"
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write to sys_logs: {e}")

# --- 3. THE HANDLER (The Sprinter) ---

async def process_update(event):
    """The core logic moved from main.py, adapted for Lambda."""
    
    # --- SECURITY CHECK: Secret Token ---
    # Only allow Telegram to ring our 'doorbell'
    headers = event.get("headers", {})
    expected_token = os.environ.get("TELEGRAM_SECRET_TOKEN")
    if expected_token and headers.get("x-telegram-bot-api-secret-token") != expected_token:
        logger.warning("Unauthorized access attempt detected.")
        return {"statusCode": 403, "body": "Forbidden"}

    supabase = get_supabase_client()
    bot = telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN"))
    
    body = json.loads(event.get("body", "{}"))
    update = telegram.Update.de_json(body, bot)
    
    if not update.message or not update.message.text:
        return {"statusCode": 200, "body": "No message to process"}
    
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    text = update.message.text.lower()
    chat_id = update.message.chat.id
    week_str = get_current_week()

    # --- COMMAND: /status ---
    if text == "/status":
        res = supabase.table("dim_roommates").select("name, is_on_vacation").execute()
        status_text = "📊 **Current Status:**\n"
        for r in res.data:
            icon = "🌴" if r['is_on_vacation'] else "✅"
            status_text += f"{icon} {r['name']}\n"
        await bot.send_message(chat_id=chat_id, text=status_text, parse_mode='Markdown')
        return

    # --- COMMAND: /vacation ---
    if text == "/vacation":
        supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        await bot.send_message(chat_id=chat_id, text=f"🌴 {user_name} is now on vacation!")
        return

    # --- COMMAND: /back ---
    if text == "/back":
        supabase.table("dim_roommates").update({"is_on_vacation": False}).eq("telegram_id", user_id).execute()
        await bot.send_message(chat_id=chat_id, text=f"🏠 Welcome back, {user_name}!")
        return

    # --- COMMAND: /volunteer ---
    if text == "/volunteer":
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if roomie_res.data:
            roomie = roomie_res.data[0]
            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie["roommate_id"],
                "task_id": 1, # Default to 'Entire Home'
                "week_number": week_str,
                "is_volunteer": True
            }).execute()
            await bot.send_message(chat_id=chat_id, text=f"🌟 Hero Alert! {user_name} volunteered. Rotation stays the same!")
        return

    # --- LOGIC: 'done' ---
    if "done" in text:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data: return

        roomie = roomie_res.data[0]
        
        # Idempotency Check
        existing = supabase.table("fct_cleaning_logs").select("*").eq("roommate_id", roomie["roommate_id"]).eq("week_number", week_str).execute()
        if existing.data:
            await bot.send_message(chat_id=chat_id, text=f"✨ {user_name}, already logged for this week!")
            return

        # Log Fact
        config_res = supabase.table("rotation_config").select("*, dim_tasks(task_description)").eq("roommate_id", roomie["roommate_id"]).execute()
        task = config_res.data[0]
        
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"],
            "task_id": task["task_id"],
            "week_number": week_str
        }).execute()

        # Next Person Logic
        check_order = task["sequence_order"]
        next_person = None
        while not next_person:
            check_order = (check_order % 5) + 1
            next_res = supabase.table("rotation_config").select("*, dim_roommates(*)").eq("sequence_order", check_order).execute()
            p = next_res.data[0]["dim_roommates"]
            if not p["is_on_vacation"]:
                next_person = p

        await bot.send_message(chat_id=chat_id, text=f"✅ Done! {user_name} cleaned: {task['dim_tasks']['task_description']}.\n🔔 Next: {next_person['name']}")
        await log_to_sys_logs(supabase, "INFO", f"Cleaning success: {user_name}")

def lambda_handler(event, context):
    import asyncio
    asyncio.run(process_update(event))
    return {
        'statusCode': 200,
        'body': json.dumps('Update processed')
    }