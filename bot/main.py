"""
bot/main.py — DEV instance (polling mode).

Run this locally to test changes. It polls Telegram every few seconds
for new messages — like a receptionist who keeps checking the mailbox.
In production, AWS Lambda replaces this with a webhook (Telegram pushes
messages to us instead of us pulling).
"""

import os
import logging
import asyncio
import traceback
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from supabase import create_client, Client

# --- 1. CONFIGURATION & LOGGING ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()  # Read the .env file (the secret key drawer)

TOKEN          = os.getenv("TELEGRAM_TOKEN")
GROUP_ID_STR   = os.getenv("TELEGRAM_GROUP_ID")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")  # Your personal Telegram ID

# Identifies this instance in sys_logs rows — "dev" here, "prod" in lambda/
ENV = "dev"

if not all([TOKEN, GROUP_ID_STR, SUPABASE_URL, SUPABASE_KEY]):
    logger.critical("Missing environment variables! Check your .env file.")
    exit(1)

GROUP_ID = int(GROUP_ID_STR)

# One shared DB client — like one open phone line to Supabase, reused for every query
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- 2. HELPER FUNCTIONS ---

def get_current_week() -> str:
    """Returns the ISO week string, e.g. '2026-W23'. Used as a dedup key."""
    return datetime.now().strftime("%Y-W%V")


def get_last_cleaned_date(roommate_id: int) -> str:
    """
    Looks up the most recent cleaning timestamp for a roommate.
    Like checking a person's last stamp in a passport.
    """
    try:
        res = (
            supabase.table("fct_cleaning_logs")
            .select("cleaned_at")
            .eq("roommate_id", roommate_id)
            .order("cleaned_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            raw_date = res.data[0]["cleaned_at"].split("T")[0]
            return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %b")
    except Exception as e:
        logger.error(f"Error fetching last cleaned date for roommate {roommate_id}: {e}")
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


# --- 3. COMMAND HANDLERS ---

async def handle_hi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /hi — Auto-registers a user by capturing their Telegram ID.
    Like a hotel check-in: the bot records your details on first visit.
    If you're already checked in, it just waves hello.
    """
    if not update.message or not update.message.from_user:
        return

    user   = update.message.from_user
    user_id = user.id
    name   = user.first_name or user.username or "Unknown"

    try:
        existing = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()

        if existing.data:
            roomie = existing.data[0]
            await update.message.reply_text(
                f"👋 Hey {roomie['name']}! You're already registered.\n"
                f"🆔 Your Telegram ID: `{user_id}`",
                parse_mode="Markdown"
            )
        else:
            # Insert as inactive — admin still needs to assign a rotation slot via /activate
            supabase.table("dim_roommates").insert({
                "name": name,
                "telegram_id": user_id,
                "is_active": False,
                "is_on_vacation": False
            }).execute()
            log_to_db("INFO", f"New self-registration: {name} (telegram_id={user_id})")

            await update.message.reply_text(
                f"👋 Hi {name}! I've captured your Telegram ID automatically.\n"
                f"🆔 Your ID: `{user_id}`\n\n"
                f"⏳ Ask the admin to activate your account and assign you a rotation slot!",
                parse_mode="Markdown"
            )
    except Exception as e:
        log_to_db("ERROR", f"handle_hi failed for {name}", error_details=traceback.format_exc())
        logger.error(f"Error in /hi handler: {e}")
        # Fallback: at minimum tell them their ID so the admin can add them manually
        await update.message.reply_text(
            f"👋 Hi {name}! Your Telegram ID is: `{user_id}`\n"
            f"⚠️ Auto-registration failed — share this ID with the admin.",
            parse_mode="Markdown"
        )


async def handle_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triggered when someone says 'done' in the group chat.
    Like punching out on a timecard: it logs the work, checks who did it,
    and announces who's on deck next. Handles the three state-machine flags:
    - Resets this person's is_priority_next (their one-time fast-pass is now used).
    - find_next_person() handles skip_next_turn for whoever comes after them.
    """
    if not update.message or not update.message.from_user:
        return

    user_id  = update.message.from_user.id
    week_str = get_current_week()

    try:
        # Step 1: Is this person in our roster?
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("🚫 Unrecognized ID. Send /hi to register, then ask the admin to activate you.")
            return

        roomie    = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        # Step 2: Already logged this week? (One punch per timecard period)
        existing = (
            supabase.table("fct_cleaning_logs")
            .select("log_id")
            .eq("roommate_id", roomie_id)
            .eq("week_number", week_str)
            .execute()
        )
        if existing.data:
            await update.message.reply_text(f"✨ {roomie['name']}, you already logged cleaning for {week_str}!")
            return

        # Step 3: What task is assigned to this person?
        config_res = (
            supabase.table("rotation_config")
            .select("*, dim_tasks(task_description)")
            .eq("roommate_id", roomie_id)
            .execute()
        )
        if not config_res.data:
            await update.message.reply_text("⚠️ You don't have an assigned rotation task. Ask the admin to run /activate.")
            return

        task = config_res.data[0]

        # Step 4: Write the cleaning log entry
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id,
            "task_id":     task["task_id"],
            "week_number": week_str
        }).execute()

        log_to_db("INFO", f"Cleaning logged: {roomie['name']} / {task['dim_tasks']['task_description']} / {week_str}")

        # Step 5: Consume this person's priority pass if they had one.
        # (They returned from vacation and jumped the queue — that turn is now served.)
        supabase.table("dim_roommates").update({"is_priority_next": False}).eq(
            "roommate_id", roomie_id
        ).execute()

        # Step 6: Find the next eligible person using the full state-machine engine.
        next_config = find_next_person(task["sequence_order"])

        next_msg = (
            f"🔔 NEXT: {next_config['dim_roommates']['name']} is up for next week!"
            if next_config
            else "🔔 NEXT: No active roommates found (everyone's on vacation or skipping!)."
        )

        await update.message.reply_text(
            f"✅ {roomie['name']} cleaned: {task['dim_tasks']['task_description']}.\n" + next_msg
        )

    except Exception as e:
        log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
        logger.error(f"Error in handle_done: {e}")
        await update.message.reply_text("❌ A database error occurred while logging your completion.")


async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /next — Who is scheduled to clean next, and how many weeks until the caller's turn?

    Global view: uses find_next_person() which respects priority overrides and
    skip_next_turn flags (read-only — /next never mutates those flags).

    Personal countdown: starting from the globally-next sequence position, counts
    circular steps to the caller's position. Each step = 1 week.
    Formula: steps = (caller_seq - next_seq) % 5
    - 0  → "You are up next!"
    - 1+ → "Your turn is in X week(s)."
    Like a countdown clock showing both who's on stage now and when your set is.
    """
    try:
        # --- Find the rotation cursor (last non-volunteer clean) ---
        last_log = (
            supabase.table("fct_cleaning_logs")
            .select("roommate_id")
            .eq("is_volunteer", False)
            .order("cleaned_at", desc=True)
            .limit(1)
            .execute()
        )
        if not last_log.data:
            await update.message.reply_text("🤔 No history yet — start the rotation by saying 'done'!")
            return

        last_id = last_log.data[0]["roommate_id"]
        current_order_res = (
            supabase.table("rotation_config")
            .select("sequence_order")
            .eq("roommate_id", last_id)
            .execute()
        )
        if not current_order_res.data:
            await update.message.reply_text("⚠️ Rotation config is missing for the last cleaner. Contact the admin.")
            return

        current_order = current_order_res.data[0]["sequence_order"]

        # --- Resolve the globally-next person (read-only — no state mutations here) ---
        next_config = find_next_person(current_order)
        if not next_config:
            await update.message.reply_text("🌴 Everyone is on vacation right now!")
            return

        next_name    = next_config["dim_roommates"]["name"]
        next_task    = next_config["dim_tasks"]["task_description"]
        next_seq     = next_config["sequence_order"]
        is_priority  = next_config["dim_roommates"].get("is_priority_next", False)

        # --- Personal countdown for the caller ---
        # Skipped if a priority override is active (the circular-position math
        # doesn't reflect reality when someone has jumped the queue).
        caller_id  = update.message.from_user.id
        steps      = None  # None means "couldn't calculate"

        if not is_priority:
            try:
                caller_roomie = (
                    supabase.table("dim_roommates")
                    .select("roommate_id")
                    .eq("telegram_id", caller_id)
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
                        # Modular distance on a ring of 5:
                        # (caller_seq - next_seq) % 5 gives steps 0..4
                        steps = (caller_seq - next_seq) % 5
            except Exception:
                # Countdown is non-critical — don't log to avoid sys_logs noise
                pass

        # --- Build the reply ---
        if steps == 0:
            # The caller IS the globally-next person — centre the message on them
            reply = (
                f"📅 **Current Week:** `{get_current_week()}`\n"
                f"🧹 **Task:** {next_task}\n"
                f"🔔 You are up next!"
            )
        else:
            upcoming_line = f"🔔 **Upcoming:** {next_name}"
            if is_priority:
                upcoming_line += " *(priority — returned from vacation)*"
            countdown_line = f"\n🕒 Your turn is in {steps} week(s)." if steps is not None else ""

            reply = (
                f"📅 **Current Week:** `{get_current_week()}`\n"
                f"{upcoming_line}\n"
                f"🧹 **Task:** {next_task}"
                + countdown_line
            )

        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
        logger.error(f"Error in handle_next: {e}")
        await update.message.reply_text("❌ Failed to calculate the upcoming schedule.")


async def get_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — Show everyone's current state and when they last cleaned.
    Like a bulletin board in the kitchen with each person's name and last shift date.
    """
    try:
        res = supabase.table("dim_roommates").select("roommate_id, name, is_on_vacation").order("name").execute()
        status_text = "📊 **Roommate Status**\n\n"
        for r in res.data:
            last_date = get_last_cleaned_date(r["roommate_id"])
            icon = "🌴 [Vacation]" if r["is_on_vacation"] else "✅ [Active]"
            status_text += f"{icon} **{r['name']}**\n└ Last Cleaned: `{last_date}`\n\n"
        await update.message.reply_text(status_text, parse_mode="Markdown")
    except Exception as e:
        log_to_db("ERROR", "get_status failed", error_details=traceback.format_exc())
        logger.error(f"Error in get_status: {e}")
        await update.message.reply_text("❌ Failed to fetch roommate status.")


async def handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /last — Show the 3 most recent cleaning log entries.
    Like flipping to the last page of a shared house logbook.
    """
    try:
        res = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name), dim_tasks(task_description)")
            .order("cleaned_at", desc=True)
            .limit(3)
            .execute()
        )
        if not res.data:
            await update.message.reply_text("📭 No cleaning history yet.")
            return

        msg = "🕒 **Recent Activity:**\n"
        for log in res.data:
            date_fmt = datetime.strptime(log["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b")
            name = log["dim_roommates"]["name"] if log.get("dim_roommates") else "Unknown"
            task = log["dim_tasks"]["task_description"] if log.get("dim_tasks") else "General Duty"
            msg += f"• `{date_fmt}`: {name} ({task})\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        log_to_db("ERROR", "handle_last failed", error_details=traceback.format_exc())
        logger.error(f"Error in handle_last: {e}")
        await update.message.reply_text("❌ Failed to fetch recent cleaning history.")


async def set_vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /vacation — Mark yourself as away so the rotation skips you.
    Like putting an 'Out of Office' sign on your door.
    """
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq("telegram_id", user_id).execute()
        if res.data:
            await update.message.reply_text("🌴 Enjoy your break! You've been skipped from the rotation.")
        else:
            await update.message.reply_text("❌ Your Telegram ID isn't registered. Send /hi first.")
    except Exception as e:
        log_to_db("ERROR", "set_vacation failed", error_details=traceback.format_exc())
        logger.error(f"Error in set_vacation: {e}")


async def set_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /back — Mark yourself as active again after vacation AND grant a priority pass.

    Sets is_on_vacation=FALSE and is_priority_next=TRUE simultaneously.
    The priority flag means this person jumps to the front of the rotation queue
    for their very next turn (see find_next_person, Layer 1).
    It's a one-time Fast Pass: consumed automatically when they say 'done'.
    """
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({
            "is_on_vacation": False,
            "is_priority_next": True   # One-time queue-jump for returning from vacation
        }).eq("telegram_id", user_id).execute()

        if res.data:
            name = res.data[0]["name"]
            log_to_db("INFO", f"{name} returned from vacation — is_priority_next set")
            await update.message.reply_text(
                "🏠 Welcome back! You're in the rotation again.\n"
                "⚡ Priority pass activated — you're at the front of the queue for your next turn."
            )
        else:
            await update.message.reply_text("❌ Your Telegram ID isn't registered. Send /hi first.")
    except Exception as e:
        log_to_db("ERROR", "set_back failed", error_details=traceback.format_exc())
        logger.error(f"Error in set_back: {e}")


async def handle_volunteer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /volunteer — Log an extra cleaning AND grant an overtime pass for next week.

    Logs as is_volunteer=TRUE (doesn't advance the rotation cursor).
    Sets skip_next_turn=TRUE so the upcoming scheduled turn is automatically
    waived — a reward for the extra effort.
    Like earning a day off: the extra shift is recorded, and you skip the
    next mandatory one. The skip is consumed by find_next_person().
    """
    user_id = update.message.from_user.id
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text("🚫 Unrecognized ID. Send /hi to register first.")
            return

        roomie = roomie_res.data[0]

        # Log the volunteer clean (marked separately so it doesn't shift the rotation cursor)
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"],
            "task_id":     1,  # Placeholder — admin can update in the DB if needed
            "week_number": get_current_week(),
            "is_volunteer": True
        }).execute()

        # Grant the overtime pass — their next scheduled turn will be auto-skipped
        supabase.table("dim_roommates").update({"skip_next_turn": True}).eq(
            "roommate_id", roomie["roommate_id"]
        ).execute()

        log_to_db("INFO", f"Volunteer clean logged + skip_next_turn set for {roomie['name']}")
        await update.message.reply_text(
            f"🌟 {roomie['name']} volunteered! Respect.\n"
            f"🎟️ Overtime pass granted — your next scheduled turn will be skipped automatically."
        )
    except Exception as e:
        log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
        logger.error(f"Error in handle_volunteer: {e}")
        await update.message.reply_text("❌ Could not log your volunteer entry.")


async def handle_help(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    /help — Display all commands. Admin sees an extra section at the bottom.
    Like a restaurant menu — regular patrons see the standard menu; the manager
    sees the staff-only specials too.
    """
    user_id = update.message.from_user.id

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

    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /activate [telegram_id] [sequence_order] [task_id]
    Admin command: sets a self-registered user as active and assigns their slot.
    Like a manager signing off on a new employee's access badge and work schedule.

    Args come from context.args (python-telegram-bot splits them automatically).
    Example: /activate 123456789 3 2
      → telegram_id=123456789, sequence_order=3, task_id=2
    Find task IDs in Supabase → dim_tasks table.
    """
    user_id = update.message.from_user.id

    if not is_admin(user_id):
        await update.message.reply_text("🚫 This command is restricted to the admin.")
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "⚠️ *Usage:* `/activate [telegram\\_id] [sequence\\_order] [task\\_id]`\n"
            "Example: `/activate 123456789 3 2`\n\n"
            "Find task IDs in Supabase → `dim_tasks` table.",
            parse_mode="Markdown"
        )
        return

    try:
        target_tg_id = int(context.args[0])
        seq_order    = int(context.args[1])
        task_id      = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ All three arguments must be whole numbers.")
        return

    try:
        # Step 1: Find the target user by their Telegram ID
        user_res = supabase.table("dim_roommates").select("*").eq("telegram_id", target_tg_id).execute()
        if not user_res.data:
            await update.message.reply_text(
                f"❌ No user found with Telegram ID `{target_tg_id}`.\n"
                f"Have them send /hi to the bot first.",
                parse_mode="Markdown"
            )
            return

        target          = user_res.data[0]
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
            slot_check = (
                supabase.table("rotation_config")
                .select("*, dim_roommates(name)")
                .eq("sequence_order", seq_order)
                .execute()
            )
            if slot_check.data:
                taken_by = slot_check.data[0]["dim_roommates"]["name"]
                await update.message.reply_text(
                    f"⚠️ Slot `{seq_order}` is already taken by *{taken_by}*. Choose a different order number.",
                    parse_mode="Markdown"
                )
                return

            supabase.table("rotation_config").insert({
                "roommate_id":   target_roomie_id,
                "task_id":       task_id,
                "sequence_order": seq_order
            }).execute()
            action_note = "new slot created"

        log_to_db("INFO", f"Admin activated {target['name']} (tg_id={target_tg_id}), slot={seq_order}, task={task_id}")

        await update.message.reply_text(
            f"✅ *{target['name']}* is now active!\n"
            f"🔢 Rotation slot: `{seq_order}` ({action_note})\n"
            f"🧹 Task ID: `{task_id}`",
            parse_mode="Markdown"
        )

    except Exception as e:
        log_to_db("ERROR", f"handle_activate failed for tg_id={target_tg_id}", error_details=traceback.format_exc())
        logger.error(f"Error in handle_activate: {e}")
        await update.message.reply_text("❌ Activation failed. Check the bot logs.")


async def handle_deletelast(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    /deletelast — Admin removes the single newest row from fct_cleaning_logs.
    Like a red-pen correction in the house logbook — only the manager can do this.
    Confirms exactly which record was dropped so the action is auditable.
    """
    user_id = update.message.from_user.id

    if not is_admin(user_id):
        await update.message.reply_text("🚫 This command is restricted to the admin.")
        return

    try:
        newest = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name), dim_tasks(task_description)")
            .order("cleaned_at", desc=True)
            .limit(1)
            .execute()
        )

        if not newest.data:
            await update.message.reply_text("📭 No cleaning logs found — nothing to delete.")
            return

        entry    = newest.data[0]
        log_id   = entry["log_id"]
        name     = entry["dim_roommates"]["name"] if entry.get("dim_roommates") else "Unknown"
        task     = entry["dim_tasks"]["task_description"] if entry.get("dim_tasks") else "Unknown task"
        date_fmt = datetime.strptime(entry["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b %Y")
        week     = entry.get("week_number", "N/A")

        # Delete by primary key — precise and safe, no risk of removing the wrong row
        supabase.table("fct_cleaning_logs").delete().eq("log_id", log_id).execute()

        log_to_db("WARNING", f"Admin deleted log #{log_id}: {name} / {task} / {week}")

        await update.message.reply_text(
            f"🗑️ *Deleted log entry* `#{log_id}`\n"
            f"• Who: {name}\n"
            f"• Task: {task}\n"
            f"• Date: {date_fmt} ({week})",
            parse_mode="Markdown"
        )

    except Exception as e:
        log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
        logger.error(f"Error in handle_deletelast: {e}")
        await update.message.reply_text("❌ Failed to delete the last log entry.")


# --- 4. MAIN RUNNER (DEV POLLING LOOP) ---

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # 'done' anywhere in a message, group chat only
    done_filter = filters.Chat(chat_id=GROUP_ID) & filters.Regex(r"(?i)\bdone\b")
    app.add_handler(MessageHandler(done_filter, handle_done_command))

    # Standard user commands
    app.add_handler(CommandHandler("hi",        handle_hi))
    app.add_handler(CommandHandler("help",      handle_help))
    app.add_handler(CommandHandler("status",    get_status))
    app.add_handler(CommandHandler("next",      handle_next))
    app.add_handler(CommandHandler("last",      handle_last))
    app.add_handler(CommandHandler("vacation",  set_vacation))
    app.add_handler(CommandHandler("back",      set_back))
    app.add_handler(CommandHandler("volunteer", handle_volunteer))

    # Admin-only commands (internal auth check, safe to register publicly)
    app.add_handler(CommandHandler("activate",   handle_activate))
    app.add_handler(CommandHandler("deletelast", handle_deletelast))

    logger.info("DEV bot running (polling mode)...")
    app.run_polling()
