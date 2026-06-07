import os
import json
import logging
import asyncio
from datetime import datetime
import telegram
from telegram.request import HTTPXRequest
from supabase import create_client, Client

# --- 1. GLOBAL INITIALIZATION (Warm Start Optimization) ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Instantiating the client globally allows AWS to reuse database connections
supabase: Client = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def get_current_week() -> str:
    return datetime.now().strftime("%Y-W%V")

def get_last_cleaned_date(roommate_id):
    """Fetch the most recent cleaning date using the corrected column name."""
    res = supabase.table("fct_cleaning_logs") \
        .select("cleaned_at") \
        .eq("roommate_id", roommate_id) \
        .order("cleaned_at", desc=True) \
        .limit(1) \
        .execute()
    
    if res.data and res.data[0]['cleaned_at']:
        raw_date = res.data[0]['cleaned_at'].split("T")[0]
        return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %b")
    return "Never"

async def log_to_sys_logs(level, message):
    try:
        supabase.table("sys_logs").insert({
            "log_level": level, "message": message, "script_name": "aws_lambda"
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write to sys_logs: {e}")

# --- 2. THE PIPELINE HANDLER ---
async def process_update(event, bot):
    # Security Check: Secret Token Handshake
    headers = event.get("headers", {})
    expected_token = os.environ.get("TELEGRAM_SECRET_TOKEN")
    if expected_token and headers.get("x-telegram-bot-api-secret-token") != expected_token:
        logger.warning("Unauthorized access attempt detected.")
        return {"statusCode": 403, "body": "Forbidden"}

    body = json.loads(event.get("body", "{}"))
    update = telegram.Update.de_json(body, bot)
    
    if not update.message or not update.message.text:
        return {"statusCode": 200}
    
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    text = update.message.text.lower()
    chat_id = update.message.chat.id
    week_str = get_current_week()

    # --- COMMAND: /status ---
    if text == "/status":
        res = supabase.table("dim_roommates").select("roommate_id, name, is_on_vacation").execute()
        status_text = "📊 **House Observability**\n\n"
        for r in res.data:
            last_date = get_last_cleaned_date(r['roommate_id'])
            icon = "🌴" if r['is_on_vacation'] else "✅"
            status_text += f"{icon} **{r['name']}**\n└ Last: `{last_date}`\n"
        await bot.send_message(chat_id=chat_id, text=status_text, parse_mode='Markdown')
        return {"statusCode": 200}

    # --- COMMAND: /next ---
    if text == "/next":
        last_log = supabase.table("fct_cleaning_logs") \
            .select("roommate_id") \
            .eq("is_volunteer", False) \
            .order("cleaned_at", desc=True) \
            .limit(1).execute()
        
        if not last_log.data:
            await bot.send_message(chat_id=chat_id, text="🤔 No history found.")
            return {"statusCode": 200}

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

        msg = (
            f"📅 **Current Week:** `{week_str}`\n"
            f"🔔 **Upcoming:** {next_person['dim_roommates']['name']}\n"
            f"🧹 **Task:** {next_person['dim_tasks']['task_description']}"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        return {"statusCode": 200}

    # --- COMMAND: /last ---
    if text == "/last":
        res = supabase.table("fct_cleaning_logs").select("*, dim_roommates(name), dim_tasks(task_description)").order("cleaned_at", desc=True).limit(3).execute()
        if not res.data:
            await bot.send_message(chat_id=chat_id, text="📭 No logs found.")
            return {"statusCode": 200}

        msg = "🕒 **Recent Activity:**\n"
        for log in res.data:
            date_fmt = datetime.strptime(log['cleaned_at'].split("T")[0], "%Y-%m-%d").strftime("%d %b")
            msg += f"• `{date_fmt}`: {log['dim_roommates']['name']} ({log['dim_tasks']['task_description']})\n"
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        return {"statusCode": 200}

    # --- COMMAND: /vacation ---
    if text == "/vacation":
        supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        await bot.send_message(chat_id=chat_id, text=f"🌴 {user_name} is now on vacation!")
        return {"statusCode": 200}

    # --- COMMAND: /back ---
    if text == "/back":
        supabase.table("dim_roommates").update({"is_on_vacation": False}).eq("telegram_id", user_id).execute()
        await bot.send_message(chat_id=chat_id, text=f"🏠 Welcome back, {user_name}!")
        return {"statusCode": 200}

    # --- LOGIC: 'done' ---
    if "done" in text:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data: return {"statusCode": 200}

        roomie = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]
        
        existing = supabase.table("fct_cleaning_logs").select("*").eq("roommate_id", roomie_id).eq("week_number", week_str).execute()
        if existing.data:
            await bot.send_message(chat_id=chat_id, text=f"✨ {user_name}, already logged for this week!")
            return {"statusCode": 200}

        config_res = supabase.table("rotation_config").select("*, dim_tasks(task_description)").eq("roommate_id", roomie_id).execute()
        task = config_res.data[0]
        
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, "task_id": task["task_id"], "week_number": week_str
        }).execute()

        check_order = task["sequence_order"]
        next_person = None
        while not next_person:
            check_order = (check_order % 5) + 1
            next_res = supabase.table("rotation_config").select("*, dim_roommates(*)").eq("sequence_order", check_order).execute()
            p = next_res.data[0]["dim_roommates"]
            if not p["is_on_vacation"]:
                next_person = p

        await bot.send_message(chat_id=chat_id, text=f"✅ Done! {user_name} cleaned: {task['dim_tasks']['task_description']}.\n🔔 Next: {next_person['name']}")
        await log_to_sys_logs("INFO", f"Cleaning success: {user_name}")
        return {"statusCode": 200}

    return {"statusCode": 200}

# --- 3. AWS LAMBDA ENTRY POINT ---
def lambda_handler(event, context):
    # Setting up the explicit network engine for the cloud environment
    t_request = HTTPXRequest(connection_pool_size=8, connect_timeout=15.0)
    bot = telegram.Bot(token=os.environ.get("TELEGRAM_TOKEN"), request=t_request)
    
    try:
        response = asyncio.run(process_update(event, bot))
        return response if response else {"statusCode": 200, "body": "Success"}
    except Exception as e:
        logger.error(f"Handler Failure: {e}")
        return {"statusCode": 200, "body": json.dumps("Error processed")}
