"""
lambda/src/function.py — PRODUCTION instance (AWS Lambda + Telegram webhook).

Telegram pushes each message as an HTTPS POST to our API Gateway URL.
AWS Lambda runs this handler for every incoming request — think of it
as a door buzzer: Telegram rings it, Lambda answers, processes the
message, and returns a 200 OK so Telegram knows we received it.

Unlike the dev polling bot, this never stays running — each invocation
is stateless and exits after handling one update.
"""

import os
import json
import logging
import asyncio
import traceback
from datetime import datetime

import telegram
from telegram.request import HTTPXRequest
from supabase import create_client, Client

# --- 1. GLOBAL INITIALIZATION (warm-start optimization) ---
# AWS Lambda reuses the same container for repeated invocations.
# Creating the Supabase client here (outside the handler) means the
# DB connection is reused across calls — like keeping a hotline open
# rather than dialling from scratch every time.

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "")  # Your personal Telegram ID

# Identifies this instance in sys_logs rows — "prod" here, "dev" in bot/
ENV = "prod"

supabase: Client = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)


# --- 2. HELPER FUNCTIONS ---

def get_current_week() -> str:
    """Returns the ISO week string, e.g. '2026-W23'. Used as a dedup key."""
    return datetime.now().strftime("%Y-W%V")


def get_last_cleaned_date(roommate_id: int) -> str:
    """
    Looks up the most recent cleaning timestamp for a roommate.
    Like checking a person's last stamp in a passport.
    """
    res = (
        supabase.table("fct_cleaning_logs")
        .select("cleaned_at")
        .eq("roommate_id", roommate_id)
        .order("cleaned_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data and res.data[0]["cleaned_at"]:
        raw_date = res.data[0]["cleaned_at"].split("T")[0]
        return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %b")
    return "Never"


def is_admin(user_id: int) -> bool:
    """
    Returns True if this Telegram user ID matches the admin set in ADMIN_TELEGRAM_ID.
    Like a VIP badge check — only works if the badge number is set in the first place.
    Admin identity lives in an env var, not the DB, so it can't be accidentally toggled.
    """
    return bool(ADMIN_TELEGRAM_ID) and str(user_id) == ADMIN_TELEGRAM_ID


def log_to_db(level: str, message: str, error_details: str = None) -> None:
    """
    Write an audit row to sys_logs. Falls back to stderr on DB failure so that
    a logging error never crashes the bot's primary user experience.
    Think of it as a black box recorder — it always tries to write, but if the
    recorder itself malfunctions, the plane (the bot) keeps flying.
    """
    try:
        supabase.table("sys_logs").insert({
            "log_level": level,
            "message": message,
            "error_details": error_details,
            "environment": ENV
        }).execute()
    except Exception as e:
        logger.error(f"log_to_db failed (falling back to stderr). Cause: {e}")


def find_next_person(starting_sequence_order: int) -> dict | None:
    """
    The central rotation engine — determines who is next in line to clean.

    Evaluation order (three layers):

    1. PRIORITY OVERRIDE — any active, non-vacation roommate with is_priority_next=TRUE
       jumps the queue immediately. This is a one-time pass granted by /back when
       returning from vacation. Like the Fast Lane at a theme park: you re-enter
       at the front, but only once.

    2. STANDARD CIRCULAR WALK — steps forward from `starting_sequence_order`
       through the ring (positions 1→2→…→5→1), skipping:
       - Vacationers (is_on_vacation=TRUE)
       - Overtime-pass holders (skip_next_turn=TRUE): flag is consumed on the spot
         (set back to FALSE) so they re-enter the regular queue next cycle.
         Like a free-pass ticket — it gets punched when used.

    3. Returns None if no eligible person is found within 10 steps (e.g. all
       on vacation AND all overtime-passes simultaneously).

    Side effect: consuming skip_next_turn writes to the database mid-walk.

    Returns the full rotation_config row joined with dim_roommates(*) and
    dim_tasks(*), which is the standard shape used throughout the codebase.
    """
    # --- Layer 1: Priority override (vacation returnee jumps to the front) ---
    try:
        priority_res = (
            supabase.table("dim_roommates")
            .select("roommate_id, name")
            .eq("is_priority_next", True)
            .eq("is_active", True)
            .eq("is_on_vacation", False)
            .execute()
        )
        if priority_res.data:
            p_id = priority_res.data[0]["roommate_id"]
            p_name = priority_res.data[0]["name"]
            config = (
                supabase.table("rotation_config")
                .select("*, dim_roommates(*), dim_tasks(*)")
                .eq("roommate_id", p_id)
                .execute()
            )
            if config.data:
                return config.data[0]
            # Priority person exists but has no rotation config — log and fall through
            log_to_db("WARNING", f"Priority person {p_name} (id={p_id}) has no rotation_config — falling through to standard walk")
    except Exception:
        log_to_db("ERROR", "find_next_person priority check failed", error_details=traceback.format_exc())

    # --- Layer 2: Standard circular walk with skip/vacation handling ---
    check_order = starting_sequence_order
    attempts = 0

    while attempts < 10:
        check_order = (check_order % 5) + 1
        attempts += 1

        try:
            res = (
                supabase.table("rotation_config")
                .select("*, dim_roommates(*), dim_tasks(*)")
                .eq("sequence_order", check_order)
                .execute()
            )
        except Exception:
            log_to_db("ERROR", f"find_next_person walk failed at order={check_order}", error_details=traceback.format_exc())
            continue

        if not res.data:
            continue  # Empty slot in the sequence ring — skip it

        candidate = res.data[0]["dim_roommates"]

        if candidate["is_on_vacation"]:
            continue  # Out of office — skip to next position

        if candidate.get("skip_next_turn", False):
            # Overtime pass: this person volunteered, so their next scheduled turn
            # is waived. Punch the ticket (set flag to FALSE) and keep walking.
            try:
                supabase.table("dim_roommates").update({"skip_next_turn": False}).eq(
                    "roommate_id", candidate["roommate_id"]
                ).execute()
                log_to_db("INFO", f"skip_next_turn consumed for {candidate['name']} — skipped this cycle")
            except Exception:
                log_to_db("ERROR", f"Failed to reset skip_next_turn for {candidate['name']}", error_details=traceback.format_exc())
            continue  # Move to the next person even after consuming the flag

        return res.data[0]  # This person is eligible — return their full config row

    return None  # All 10 attempts exhausted — everyone is unavailable


# --- 3. COMMAND ROUTER ---

async def process_update(event: dict, bot: telegram.Bot):
    """
    Routes each incoming Telegram update to the right handler.
    Think of it as a telephone switchboard: reads the incoming message
    and connects it to the correct department.
    """

    # Security: verify the secret token header set during webhook registration
    headers = event.get("headers", {})
    expected_token = os.environ.get("TELEGRAM_SECRET_TOKEN")
    if expected_token and headers.get("x-telegram-bot-api-secret-token") != expected_token:
        logger.warning("Unauthorized request — secret token mismatch.")
        return {"statusCode": 403, "body": "Forbidden"}

    body = json.loads(event.get("body", "{}"))
    update = telegram.Update.de_json(body, bot)

    if not update.message or not update.message.text:
        return {"statusCode": 200}

    user_id   = update.message.from_user.id
    user_name = update.message.from_user.first_name
    # Lowercase for reliable command matching; keep the raw text for arg parsing
    text     = update.message.text.strip().lower()
    raw_text = update.message.text.strip()
    chat_id  = update.message.chat.id
    week_str = get_current_week()

    # ------------------------------------------------------------------ /hi
    # Like a hotel check-in: record the guest's details on first visit.
    if text == "/hi":
        name = update.message.from_user.first_name or update.message.from_user.username or "Unknown"
        try:
            existing = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()

            if existing.data:
                roomie = existing.data[0]
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"👋 Hey {roomie['name']}! You're already registered.\n"
                        f"🆔 Your Telegram ID: `{user_id}`"
                    ),
                    parse_mode="Markdown"
                )
            else:
                # Insert as inactive — admin assigns the rotation slot via /activate
                supabase.table("dim_roommates").insert({
                    "name": name,
                    "telegram_id": user_id,
                    "is_active": False,
                    "is_on_vacation": False
                }).execute()
                log_to_db("INFO", f"New self-registration: {name} (telegram_id={user_id})")
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"👋 Hi {name}! I've captured your Telegram ID automatically.\n"
                        f"🆔 Your ID: `{user_id}`\n\n"
                        f"⏳ Ask the admin to activate your account and assign you a rotation slot!"
                    ),
                    parse_mode="Markdown"
                )
        except Exception:
            log_to_db("ERROR", f"handle_hi failed for {name}", error_details=traceback.format_exc())
            await bot.send_message(
                chat_id=chat_id,
                text=f"👋 Hi {name}! Your Telegram ID is: `{user_id}`\n⚠️ Auto-registration failed — share this ID with the admin.",
                parse_mode="Markdown"
            )
        return {"statusCode": 200}

    # --------------------------------------------------------------- /help
    # Like a restaurant menu — regular patrons see standard items; the manager
    # sees the staff-only specials too.
    if text == "/help":
        msg = (
            "📋 *Roommate Bot Commands*\n\n"
            "*Cleaning Rotation*\n"
            "• `done` — Log your cleaning task for this week\n"
            "• `/volunteer` — Log a bonus clean + earn a skip for your next turn\n"
            "• `/next` — Show who cleans next + your personal countdown\n"
            "• `/last` — Show the 3 most recent entries\n\n"
            "*Your Status*\n"
            "• `/status` — Everyone's status and last cleaned date\n"
            "• `/vacation` — Mark yourself away (rotation skips you)\n"
            "• `/back` — Return from vacation (priority pass granted)\n\n"
            "*Getting Started*\n"
            "• `/hi` — Register your Telegram ID with the bot\n"
            "• `/help` — Show this message"
        )
        if is_admin(user_id):
            msg += (
                "\n\n"
                "🔧 *Admin Tools*\n"
                "• `/activate [id] [order] [task\\_id]` — Activate a registered user and assign their rotation slot\n"
                "• `/deletelast` — Remove the most recent cleaning log entry"
            )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        return {"statusCode": 200}

    # ------------------------------------------------------------- /status
    # Bulletin board: everyone's state and last cleaned date.
    if text == "/status":
        try:
            res = supabase.table("dim_roommates").select("roommate_id, name, is_on_vacation").order("name").execute()
            status_text = "📊 **Roommate Status**\n\n"
            for r in res.data:
                last_date = get_last_cleaned_date(r["roommate_id"])
                icon = "🌴 [Vacation]" if r["is_on_vacation"] else "✅ [Active]"
                status_text += f"{icon} **{r['name']}**\n└ Last Cleaned: `{last_date}`\n\n"
            await bot.send_message(chat_id=chat_id, text=status_text, parse_mode="Markdown")
        except Exception:
            log_to_db("ERROR", "get_status failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Failed to fetch roommate status.")
        return {"statusCode": 200}

    # --------------------------------------------------------------- /next
    # Who's up after the last scheduled clean? Plus the caller's personal countdown.
    #
    # Global view: uses find_next_person() which respects priority overrides and
    # skip_next_turn flags (read-only — /next never mutates those flags).
    #
    # Personal countdown: steps = (caller_seq - next_seq) % 5
    # 0 → "You are up next!" | 1+ → "Your turn is in X week(s)."
    if text == "/next":
        try:
            # Find the rotation cursor (last non-volunteer clean)
            last_log = (
                supabase.table("fct_cleaning_logs")
                .select("roommate_id")
                .eq("is_volunteer", False)
                .order("cleaned_at", desc=True)
                .limit(1)
                .execute()
            )
            if not last_log.data:
                await bot.send_message(chat_id=chat_id, text="🤔 No history yet — say 'done' to start the rotation!")
                return {"statusCode": 200}

            last_id = last_log.data[0]["roommate_id"]
            order_res = (
                supabase.table("rotation_config")
                .select("sequence_order")
                .eq("roommate_id", last_id)
                .execute()
            )
            if not order_res.data:
                await bot.send_message(chat_id=chat_id, text="⚠️ Rotation config is missing for the last cleaner. Contact the admin.")
                return {"statusCode": 200}

            current_order = order_res.data[0]["sequence_order"]

            # Resolve the globally-next person (read-only — no state mutations here)
            next_config = find_next_person(current_order)
            if not next_config:
                await bot.send_message(chat_id=chat_id, text="🌴 Everyone is on vacation right now!")
                return {"statusCode": 200}

            next_name   = next_config["dim_roommates"]["name"]
            next_task   = next_config["dim_tasks"]["task_description"]
            next_seq    = next_config["sequence_order"]
            is_priority = next_config["dim_roommates"].get("is_priority_next", False)

            # Personal countdown for the caller.
            # Skipped if a priority override is active (the circular-position math
            # doesn't reflect reality when someone has jumped the queue).
            steps = None  # None means "couldn't calculate"

            if not is_priority:
                try:
                    caller_roomie = (
                        supabase.table("dim_roommates")
                        .select("roommate_id")
                        .eq("telegram_id", user_id)
                        .execute()
                    )
                    if caller_roomie.data:
                        caller_cfg = (
                            supabase.table("rotation_config")
                            .select("sequence_order")
                            .eq("roommate_id", caller_roomie.data[0]["roommate_id"])
                            .execute()
                        )
                        if caller_cfg.data:
                            caller_seq = caller_cfg.data[0]["sequence_order"]
                            # Modular distance on a ring of 5: gives steps 0..4
                            steps = (caller_seq - next_seq) % 5
                except Exception:
                    pass  # Countdown is non-critical — don't pollute sys_logs

            # Build the reply
            if steps == 0:
                reply = (
                    f"📅 **Current Week:** `{week_str}`\n"
                    f"🧹 **Task:** {next_task}\n"
                    f"🔔 You are up next!"
                )
            else:
                upcoming_line = f"🔔 **Upcoming:** {next_name}"
                if is_priority:
                    upcoming_line += " *(priority — returned from vacation)*"
                countdown_line = f"\n🕒 Your turn is in {steps} week(s)." if steps is not None else ""

                reply = (
                    f"📅 **Current Week:** `{week_str}`\n"
                    f"{upcoming_line}\n"
                    f"🧹 **Task:** {next_task}"
                    + countdown_line
                )

            await bot.send_message(chat_id=chat_id, text=reply, parse_mode="Markdown")
        except Exception:
            log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Failed to calculate the upcoming schedule.")
        return {"statusCode": 200}

    # --------------------------------------------------------------- /last
    # Last 3 entries in the house logbook.
    if text == "/last":
        try:
            res = (
                supabase.table("fct_cleaning_logs")
                .select("*, dim_roommates(name), dim_tasks(task_description)")
                .order("cleaned_at", desc=True)
                .limit(3)
                .execute()
            )
            if not res.data:
                await bot.send_message(chat_id=chat_id, text="📭 No logs yet.")
                return {"statusCode": 200}

            msg = "🕒 **Recent Activity:**\n"
            for log in res.data:
                date_fmt = datetime.strptime(log["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b")
                name = log["dim_roommates"]["name"] if log.get("dim_roommates") else "Unknown"
                task = log["dim_tasks"]["task_description"] if log.get("dim_tasks") else "General Duty"
                msg += f"• `{date_fmt}`: {name} ({task})\n"
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception:
            log_to_db("ERROR", "handle_last failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Failed to fetch recent cleaning history.")
        return {"statusCode": 200}

    # ---------------------------------------------------------- /vacation
    # Put an 'Out of Office' sign on the rotation.
    if text == "/vacation":
        try:
            res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
            if res.data:
                await bot.send_message(chat_id=chat_id, text="🌴 Enjoy your break! You've been skipped from the rotation.")
            else:
                await bot.send_message(chat_id=chat_id, text="❌ Your Telegram ID isn't registered. Send /hi first.")
        except Exception:
            log_to_db("ERROR", "set_vacation failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Could not update vacation status.")
        return {"statusCode": 200}

    # ------------------------------------------------------------- /back
    # Take down the 'Out of Office' sign AND grant a priority pass.
    # Sets is_on_vacation=FALSE and is_priority_next=TRUE simultaneously.
    # The priority flag means this person jumps to the front of the rotation queue
    # for their very next turn (see find_next_person, Layer 1).
    # It's a one-time Fast Pass: consumed automatically when they say 'done'.
    if text == "/back":
        try:
            res = supabase.table("dim_roommates").update({
                "is_on_vacation": False,
                "is_priority_next": True   # One-time queue-jump for returning from vacation
            }).eq("telegram_id", user_id).execute()

            if res.data:
                name = res.data[0]["name"]
                log_to_db("INFO", f"{name} returned from vacation — is_priority_next set")
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🏠 Welcome back! You're in the rotation again.\n"
                        "⚡ Priority pass activated — you're at the front of the queue for your next turn."
                    )
                )
            else:
                await bot.send_message(chat_id=chat_id, text="❌ Your Telegram ID isn't registered. Send /hi first.")
        except Exception:
            log_to_db("ERROR", "set_back failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Could not update status.")
        return {"statusCode": 200}

    # --------------------------------------------------------- /volunteer
    # Log an extra cleaning AND grant an overtime pass for next week.
    # Logs as is_volunteer=TRUE (doesn't advance the rotation cursor).
    # Sets skip_next_turn=TRUE so the upcoming scheduled turn is automatically
    # waived — a reward for the extra effort. Like earning a day off: the extra
    # shift is recorded, and you skip the next mandatory one.
    if text == "/volunteer":
        try:
            roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
            if not roomie_res.data:
                await bot.send_message(chat_id=chat_id, text="🚫 Unrecognized ID. Send /hi to register first.")
                return {"statusCode": 200}

            roomie = roomie_res.data[0]

            # Log the volunteer clean (marked separately so it doesn't shift the rotation cursor)
            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie["roommate_id"],
                "task_id":     1,  # Placeholder — admin can update in the DB if needed
                "week_number": week_str,
                "is_volunteer": True
            }).execute()

            # Grant the overtime pass — their next scheduled turn will be auto-skipped
            supabase.table("dim_roommates").update({"skip_next_turn": True}).eq(
                "roommate_id", roomie["roommate_id"]
            ).execute()

            log_to_db("INFO", f"Volunteer clean logged + skip_next_turn set for {roomie['name']}")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🌟 {roomie['name']} volunteered! Respect.\n"
                    f"🎟️ Overtime pass granted — your next scheduled turn will be skipped automatically."
                )
            )
        except Exception:
            log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Could not log your volunteer entry.")
        return {"statusCode": 200}

    # ---------------------------------------------------------- /activate
    # Admin: activate a self-registered user and assign their rotation slot.
    # Like a manager signing off on a new employee's access badge and work schedule.
    # Usage: /activate [telegram_id] [sequence_order] [task_id]
    if text.startswith("/activate"):
        if not is_admin(user_id):
            await bot.send_message(chat_id=chat_id, text="🚫 This command is restricted to the admin.")
            return {"statusCode": 200}

        # Parse args from raw (non-lowercased) text to be safe with large integers
        parts = raw_text.split()
        args = parts[1:]  # strip the command token itself

        if len(args) != 3:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ *Usage:* `/activate [telegram\\_id] [sequence\\_order] [task\\_id]`\n"
                    "Example: `/activate 123456789 3 2`\n\n"
                    "Find task IDs in Supabase → `dim_tasks` table."
                ),
                parse_mode="Markdown"
            )
            return {"statusCode": 200}

        try:
            target_tg_id = int(args[0])
            seq_order    = int(args[1])
            task_id      = int(args[2])
        except ValueError:
            await bot.send_message(chat_id=chat_id, text="❌ All three arguments must be whole numbers.")
            return {"statusCode": 200}

        try:
            # Step 1: Find the target user
            user_res = supabase.table("dim_roommates").select("*").eq("telegram_id", target_tg_id).execute()
            if not user_res.data:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ No user found with Telegram ID `{target_tg_id}`.\nHave them send /hi first.",
                    parse_mode="Markdown"
                )
                return {"statusCode": 200}

            target           = user_res.data[0]
            target_roomie_id = target["roommate_id"]

            # Step 2: Flip them to active
            supabase.table("dim_roommates").update({"is_active": True}).eq("roommate_id", target_roomie_id).execute()

            # Step 3: Upsert their rotation config slot
            existing_config = (
                supabase.table("rotation_config")
                .select("*")
                .eq("roommate_id", target_roomie_id)
                .execute()
            )

            if existing_config.data:
                supabase.table("rotation_config").update({
                    "sequence_order": seq_order,
                    "task_id":        task_id
                }).eq("roommate_id", target_roomie_id).execute()
                action_note = "updated existing slot"
            else:
                # Guard against stealing someone else's sequence position
                slot_check = (
                    supabase.table("rotation_config")
                    .select("*, dim_roommates(name)")
                    .eq("sequence_order", seq_order)
                    .execute()
                )
                if slot_check.data:
                    taken_by = slot_check.data[0]["dim_roommates"]["name"]
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⚠️ Slot `{seq_order}` is already taken by *{taken_by}*. Choose a different order number.",
                        parse_mode="Markdown"
                    )
                    return {"statusCode": 200}

                supabase.table("rotation_config").insert({
                    "roommate_id":    target_roomie_id,
                    "task_id":        task_id,
                    "sequence_order": seq_order
                }).execute()
                action_note = "new slot created"

            log_to_db("INFO", f"Admin activated {target['name']} (tg_id={target_tg_id}), slot={seq_order}, task={task_id}")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *{target['name']}* is now active!\n"
                    f"🔢 Rotation slot: `{seq_order}` ({action_note})\n"
                    f"🧹 Task ID: `{task_id}`"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            log_to_db("ERROR", f"handle_activate failed for tg_id={target_tg_id}", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Activation failed. Check the bot logs.")
        return {"statusCode": 200}

    # ------------------------------------------------------- /deletelast
    # Admin: erase the single newest cleaning log entry.
    # Like a red-pen correction in the logbook — only the manager can do this.
    if text == "/deletelast":
        if not is_admin(user_id):
            await bot.send_message(chat_id=chat_id, text="🚫 This command is restricted to the admin.")
            return {"statusCode": 200}

        try:
            newest = (
                supabase.table("fct_cleaning_logs")
                .select("*, dim_roommates(name), dim_tasks(task_description)")
                .order("cleaned_at", desc=True)
                .limit(1)
                .execute()
            )

            if not newest.data:
                await bot.send_message(chat_id=chat_id, text="📭 No cleaning logs found — nothing to delete.")
                return {"statusCode": 200}

            entry    = newest.data[0]
            log_id   = entry["log_id"]
            name     = entry["dim_roommates"]["name"] if entry.get("dim_roommates") else "Unknown"
            task     = entry["dim_tasks"]["task_description"] if entry.get("dim_tasks") else "Unknown task"
            date_fmt = datetime.strptime(entry["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b %Y")
            week     = entry.get("week_number", "N/A")

            # Delete by primary key — precise, no risk of removing the wrong row
            supabase.table("fct_cleaning_logs").delete().eq("log_id", log_id).execute()

            log_to_db("WARNING", f"Admin deleted log #{log_id}: {name} / {task} / {week}")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🗑️ *Deleted log entry* `#{log_id}`\n"
                    f"• Who: {name}\n"
                    f"• Task: {task}\n"
                    f"• Date: {date_fmt} ({week})"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ Failed to delete the last log entry.")
        return {"statusCode": 200}

    # ---------------------------------------------------------------- done
    # 'done' anywhere in the message — punch the timecard.
    # Logs the clean, consumes any priority pass, then calls find_next_person
    # to announce who is on deck (which handles skip_next_turn automatically).
    if "done" in text:
        try:
            roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
            if not roomie_res.data:
                await bot.send_message(chat_id=chat_id, text="🚫 Unrecognized ID. Send /hi to register first.")
                return {"statusCode": 200}

            roomie    = roomie_res.data[0]
            roomie_id = roomie["roommate_id"]

            # One log per person per week (one timecard punch per pay period)
            existing = (
                supabase.table("fct_cleaning_logs")
                .select("log_id")
                .eq("roommate_id", roomie_id)
                .eq("week_number", week_str)
                .execute()
            )
            if existing.data:
                await bot.send_message(chat_id=chat_id, text=f"✨ {user_name}, you already logged cleaning for {week_str}!")
                return {"statusCode": 200}

            config_res = (
                supabase.table("rotation_config")
                .select("*, dim_tasks(task_description)")
                .eq("roommate_id", roomie_id)
                .execute()
            )
            if not config_res.data:
                await bot.send_message(chat_id=chat_id, text="⚠️ You don't have an assigned rotation task. Ask the admin to run /activate.")
                return {"statusCode": 200}

            task = config_res.data[0]

            # Write the cleaning log entry
            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie_id,
                "task_id":     task["task_id"],
                "week_number": week_str
            }).execute()

            log_to_db("INFO", f"Cleaning logged: {user_name} / {task['dim_tasks']['task_description']} / {week_str}")

            # Consume this person's priority pass if they had one.
            # (They returned from vacation and jumped the queue — that turn is now served.)
            supabase.table("dim_roommates").update({"is_priority_next": False}).eq(
                "roommate_id", roomie_id
            ).execute()

            # Find the next eligible person using the full state-machine engine
            next_config = find_next_person(task["sequence_order"])

            next_msg = (
                f"🔔 NEXT: {next_config['dim_roommates']['name']} is up for next week!"
                if next_config
                else "🔔 NEXT: No active roommates found (everyone's on vacation or skipping!)."
            )

            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ {user_name} cleaned: {task['dim_tasks']['task_description']}.\n{next_msg}"
            )
        except Exception:
            log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
            await bot.send_message(chat_id=chat_id, text="❌ A database error occurred while logging your completion.")
        return {"statusCode": 200}

    # Unrecognized message — silently return 200 so Telegram doesn't retry
    return {"statusCode": 200}


# --- 4. AWS LAMBDA ENTRY POINT ---

def lambda_handler(event, context):
    """
    AWS calls this function for every incoming webhook POST.
    It sets up an async event loop (Lambda is synchronous by default),
    runs our async handler, and returns the HTTP response.
    """
    # HTTPXRequest is required in the Lambda environment — the default
    # aiohttp transport doesn't work without a running event loop at import time.
    t_request = HTTPXRequest(connection_pool_size=8, connect_timeout=15.0)
    bot = telegram.Bot(
        token=os.environ.get("TELEGRAM_TOKEN"),
        request=t_request
    )

    try:
        response = asyncio.run(process_update(event, bot))
        return response if response else {"statusCode": 200, "body": "OK"}
    except Exception as e:
        logger.error(f"Lambda handler error: {e}")
        # Always return 200 to Telegram — a non-200 response causes it to retry the same update
        return {"statusCode": 200, "body": json.dumps("Error handled")}
