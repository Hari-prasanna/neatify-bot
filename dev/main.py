# dev/main.py — Local polling bot. Run with: python dev/main.py

import os
import sys
import html
import logging
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from src.utils import (
    supabase, BOT_ENV, get_current_week,
    log_to_db, find_next_person, mention_user,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN        = os.getenv("TELEGRAM_TOKEN")
GROUP_ID_STR = os.getenv("TELEGRAM_GROUP_ID")
ADMIN_ID     = os.getenv("ADMIN_TELEGRAM_ID", "")

if not all([TOKEN, GROUP_ID_STR]):
    logger.critical("Missing TELEGRAM_TOKEN or TELEGRAM_GROUP_ID.")
    sys.exit(1)

GROUP_ID         = int(GROUP_ID_STR)
TASK_ENTIRE_HOME = 1
TASK_BATHROOM    = 2


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def _err(label: str, exc: Exception) -> None:
    """Print raw exception to stdout so the exact DB error is visible in the terminal."""
    tb = traceback.format_exc()
    bar = "=" * 60
    print(f"\n{bar}", flush=True)
    print(f"[DEV ERROR] {label}", flush=True)
    print(f"  Type   : {type(exc).__name__}", flush=True)
    print(f"  Message: {exc}", flush=True)
    print(f"{'─' * 60}", flush=True)
    print(tb, flush=True)
    print(f"{bar}\n", flush=True)


def get_last_cleaned_date(roommate_id: int) -> str:
    try:
        res = (
            supabase.table("fct_cleaning_logs")
            .select("cleaned_at").eq("roommate_id", roommate_id)
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if res.data:
            raw = res.data[0]["cleaned_at"].split("T")[0]
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b")
    except Exception as e:
        print(f"\n[get_last_cleaned_date] roommate_id={roommate_id}: {type(e).__name__}: {e}", flush=True)
    return "Never"


async def handle_hi(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user     = update.message.from_user
    user_id  = user.id
    username = user.username
    name     = html.escape(user.first_name or username or "Unknown")

    try:
        existing = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if existing.data:
            roomie = existing.data[0]
            if username:
                supabase.table("dim_roommates").update({"telegram_username": username}).eq(
                    "roommate_id", roomie["roommate_id"]
                ).execute()
            uline = f"• Username: @{username}" if username else "• No public @username set"
            msg = (
                f"👤 <b>Registration</b>\n\n"
                f"✅ Already registered!\n"
                f"• Name: {html.escape(roomie['name'])}\n"
                f"• Tg ID: <code>{user_id}</code>\n"
                f"{uline}"
            )
        else:
            supabase.table("dim_roommates").insert({
                "name": name, "telegram_id": user_id,
                "telegram_username": username,
                "is_active": False, "is_on_vacation": False,
            }).execute()
            log_to_db("INFO", f"New registration: {name} (tg_id={user_id})")
            msg = (
                f"👤 <b>Registration</b>\n\n"
                f"✅ Registered!\n"
                f"• Name: {name}\n"
                f"• Tg ID: <code>{user_id}</code>\n\n"
                f"⏳ Ask the admin to activate your account."
            )
    except Exception as e:
        _err("handle_hi", e)
        log_to_db("ERROR", f"handle_hi failed for user_id={user_id}", error_details=traceback.format_exc())
        msg = (
            f"👤 <b>Registration</b>\n\n"
            f"⚠️ Auto-save failed — share this ID with the admin.\n"
            f"• Tg ID: <code>{user_id}</code>"
        )

    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_done_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id  = update.message.from_user.id
    week_str = get_current_week()

    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text(
                "🧹 <b>Clean Log</b>\n\n❌ Unrecognized user.\nSend /hi to register first.",
                parse_mode="HTML"
            )
            return

        roomie    = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        existing = (
            supabase.table("fct_cleaning_logs").select("log_id")
            .eq("roommate_id", roomie_id).eq("week_number", week_str).execute()
        )
        if existing.data:
            await update.message.reply_text(
                f"🧹 <b>Clean Log</b>\n\n"
                f"✨ Already logged this week!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Week: <code>{week_str}</code>",
                parse_mode="HTML"
            )
            return

        config_res = (
            supabase.table("rotation_config").select("*, dim_tasks(task_description)")
            .eq("roommate_id", roomie_id).limit(1).execute()
        )
        if not config_res.data:
            await update.message.reply_text(
                "🧹 <b>Clean Log</b>\n\n❌ No task assigned.\nAsk the admin to run /activate.",
                parse_mode="HTML"
            )
            return

        task      = config_res.data[0]
        task_id   = task["task_id"]
        task_desc = html.escape(task["dim_tasks"]["task_description"])

        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, "task_id": task_id, "week_number": week_str,
        }).execute()
        log_to_db("INFO", f"Clean logged: {roomie['name']} / {task_desc} / {week_str}")

        # Consume priority pass if active — one-time use
        supabase.table("dim_roommates").update({"is_priority_next": False}).eq(
            "roommate_id", roomie_id
        ).execute()

        next_config = find_next_person(task_id)
        next_handle = mention_user(next_config["dim_roommates"]) if next_config else "No one available"

        await update.message.reply_text(
            f"🧹 <b>Clean Logged</b>\n\n"
            f"✅ Logged!\n"
            f"• By: {mention_user(roomie)}\n"
            f"• Week: <code>{week_str}</code>\n"
            f"• Task: {task_desc}\n\n"
            f"🔔 Next ({task_desc}): {next_handle}",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_done_command", e)
        log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_next(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    week_str = get_current_week()
    user_id  = update.message.from_user.id

    try:
        home_config = find_next_person(TASK_ENTIRE_HOME)
        bath_config = find_next_person(TASK_BATHROOM)
        home_handle = mention_user(home_config["dim_roommates"]) if home_config else "No one scheduled"
        bath_handle = mention_user(bath_config["dim_roommates"]) if bath_config else "No one scheduled"

        countdown_line = ""
        try:
            caller_res = supabase.table("dim_roommates").select("roommate_id").eq(
                "telegram_id", user_id
            ).execute()
            if caller_res.data:
                caller_rid = caller_res.data[0]["roommate_id"]
                caller_cfg = supabase.table("rotation_config").select(
                    "sequence_order, task_id"
                ).eq("roommate_id", caller_rid).limit(1).execute()
                if caller_cfg.data:
                    caller_seq  = caller_cfg.data[0]["sequence_order"]
                    caller_task = caller_cfg.data[0]["task_id"]
                    track_next  = home_config if caller_task == TASK_ENTIRE_HOME else bath_config
                    is_priority = (
                        track_next["dim_roommates"].get("is_priority_next", False) if track_next else False
                    )
                    if track_next and not is_priority:
                        steps = (caller_seq - track_next["sequence_order"]) % 5
                        countdown_line = (
                            "\n🔔 You are up next!" if steps == 0
                            else f"\n🕒 Your turn in: {steps} week(s)"
                        )
        except Exception as e:
            print(f"\n[handle_next] countdown: {type(e).__name__}: {e}", flush=True)

        await update.message.reply_text(
            f"📅 <b>Upcoming Schedule</b>\n\n"
            f"• Week: <code>{week_str}</code>\n"
            f"• 🏠 Home: {home_handle}\n"
            f"• 🚿 Bathroom: {bath_handle}"
            f"{countdown_line}",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_next", e)
        log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def get_status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    week_str = get_current_week()
    try:
        res = supabase.table("dim_roommates").select(
            "roommate_id, name, telegram_id, telegram_username, is_on_vacation"
        ).order("name").execute()

        lines = []
        for r in res.data:
            icon = "🌴" if r["is_on_vacation"] else "✅"
            last = get_last_cleaned_date(r["roommate_id"])
            lines.append(f"{icon} {mention_user(r)}  —  last cleaned {last}")

        await update.message.reply_text(
            f"📊 <b>House Status</b>\n"
            f"Week: <code>{week_str}</code>\n\n"
            + ("\n".join(lines) if lines else "No roommates found."),
            parse_mode="HTML"
        )

    except Exception as e:
        _err("get_status", e)
        log_to_db("ERROR", "get_status failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_last(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        res = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name, telegram_id, telegram_username), dim_tasks(task_description)")
            .order("cleaned_at", desc=True).limit(3).execute()
        )
        if not res.data:
            await update.message.reply_text(
                "🕒 <b>Recent Activity</b>\n\nNo cleaning logs yet.", parse_mode="HTML"
            )
            return

        lines = []
        for entry in res.data:
            date_str = datetime.strptime(entry["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b")
            name = mention_user(entry["dim_roommates"]) if entry.get("dim_roommates") else "Unknown"
            task = html.escape(entry["dim_tasks"]["task_description"]) if entry.get("dim_tasks") else "Task"
            lines.append(f"• {date_str}  {name}  ({task})")

        await update.message.reply_text(
            "🕒 <b>Recent Activity</b>\n\n" + "\n".join(lines), parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_last", e)
        log_to_db("ERROR", "handle_last failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def set_vacation(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq(
            "telegram_id", user_id
        ).execute()
        if res.data:
            await update.message.reply_text(
                f"🌴 <b>Vacation Mode</b>\n\n"
                f"✅ Marked as away.\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"Both rotation tracks will skip you.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "🌴 <b>Vacation Mode</b>\n\n❌ ID not found. Send /hi to register first.",
                parse_mode="HTML"
            )
    except Exception as e:
        _err("set_vacation", e)
        log_to_db("ERROR", "set_vacation failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def set_back(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({
            "is_on_vacation": False, "is_priority_next": True,
        }).eq("telegram_id", user_id).execute()

        if res.data:
            log_to_db("INFO", f"{res.data[0]['name']} returned from vacation — is_priority_next set")
            await update.message.reply_text(
                f"🏠 <b>Back in Rotation</b>\n\n"
                f"✅ Welcome back!\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"⚡ Priority pass active — you're at the front of the queue next turn.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "🏠 <b>Back in Rotation</b>\n\n❌ ID not found. Send /hi to register first.",
                parse_mode="HTML"
            )
    except Exception as e:
        _err("set_back", e)
        log_to_db("ERROR", "set_back failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_volunteer(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.message.from_user.id
    week_str = get_current_week()
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
        if not roomie_res.data:
            await update.message.reply_text(
                "🌟 <b>Volunteer</b>\n\n❌ ID not found. Send /hi to register first.",
                parse_mode="HTML"
            )
            return

        roomie = roomie_res.data[0]
        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"], "task_id": TASK_ENTIRE_HOME,
            "week_number": week_str, "is_volunteer": True,
        }).execute()
        supabase.table("dim_roommates").update({"skip_next_turn": True}).eq(
            "roommate_id", roomie["roommate_id"]
        ).execute()
        log_to_db("INFO", f"Volunteer + skip_next_turn set for {roomie['name']}")

        await update.message.reply_text(
            f"🌟 <b>Volunteer Clean Logged</b>\n\n"
            f"✅ Extra clean recorded!\n"
            f"• By: {mention_user(roomie)}\n"
            f"• Week: <code>{week_str}</code>\n\n"
            f"🎟️ Skip pass granted — next scheduled turn is waived.",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_volunteer", e)
        log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    msg = (
        "📋 <b>Command Reference</b>\n\n"
        "<b>Cleaning Rotation</b>\n"
        "• done — Log your clean for this week\n"
        "• /volunteer — Bonus clean + earn a skip pass\n"
        "• /next — Upcoming schedule (both tracks)\n"
        "• /last — 3 most recent log entries\n\n"
        "<b>Your Status</b>\n"
        "• /status — Everyone's state + last cleaned\n"
        "• /vacation — Mark yourself away\n"
        "• /back — Return (priority pass granted)\n\n"
        "<b>Getting Started</b>\n"
        "• /hi — Register your Telegram ID\n"
        "• /help — This message"
    )
    if is_admin(user_id):
        msg += (
            "\n\n<b>🔧 Admin Tools</b>\n"
            "• /activate [tg_id] [order] [task_id]\n"
            "• /deletelast — Remove newest log entry"
        )
    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🔧 <b>Activation</b>\n\n🚫 Admin only.", parse_mode="HTML")
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "🔧 <b>Activation</b>\n\nUsage: /activate [tg_id] [order] [task_id]\nExample: /activate 123456789 3 1",
            parse_mode="HTML"
        )
        return

    try:
        target_tg_id = int(context.args[0])
        seq_order    = int(context.args[1])
        task_id      = int(context.args[2])
    except ValueError:
        await update.message.reply_text(
            "🔧 <b>Activation</b>\n\n❌ All three args must be integers.", parse_mode="HTML"
        )
        return

    try:
        user_res = supabase.table("dim_roommates").select("*").eq("telegram_id", target_tg_id).execute()
        if not user_res.data:
            await update.message.reply_text(
                f"🔧 <b>Activation</b>\n\n❌ No user with Tg ID {target_tg_id}. Have them send /hi first.",
                parse_mode="HTML"
            )
            return

        target     = user_res.data[0]
        target_rid = target["roommate_id"]
        supabase.table("dim_roommates").update({"is_active": True}).eq("roommate_id", target_rid).execute()

        existing_cfg = supabase.table("rotation_config").select("*").eq(
            "roommate_id", target_rid
        ).eq("task_id", task_id).execute()

        if existing_cfg.data:
            supabase.table("rotation_config").update({"sequence_order": seq_order}).eq(
                "roommate_id", target_rid
            ).eq("task_id", task_id).execute()
            action_note = "slot updated"
        else:
            slot_check = supabase.table("rotation_config").select(
                "*, dim_roommates(name)"
            ).eq("sequence_order", seq_order).eq("task_id", task_id).execute()
            if slot_check.data:
                taken_by = html.escape(slot_check.data[0]["dim_roommates"]["name"])
                await update.message.reply_text(
                    f"🔧 <b>Activation</b>\n\n⚠️ Slot {seq_order} (task {task_id}) taken by {taken_by}.",
                    parse_mode="HTML"
                )
                return
            supabase.table("rotation_config").insert({
                "roommate_id": target_rid, "task_id": task_id, "sequence_order": seq_order,
            }).execute()
            action_note = "new slot created"

        log_to_db("INFO",
                  f"Admin activated {target['name']} (tg_id={target_tg_id}), "
                  f"slot={seq_order}, task_id={task_id}")
        await update.message.reply_text(
            f"🔧 <b>Activation</b>\n\n"
            f"✅ {html.escape(target['name'])} activated!\n"
            f"• Task ID: {task_id}\n"
            f"• Slot: {seq_order}  ({action_note})",
            parse_mode="HTML"
        )

    except Exception as e:
        _err(f"handle_activate (tg_id={target_tg_id})", e)
        log_to_db("ERROR", f"handle_activate failed for tg_id={target_tg_id}",
                  error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_deletelast(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🗑️ <b>Delete Log</b>\n\n🚫 Admin only.", parse_mode="HTML")
        return

    try:
        newest = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name, telegram_id, telegram_username), dim_tasks(task_description)")
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if not newest.data:
            await update.message.reply_text(
                "🗑️ <b>Delete Log</b>\n\n📭 No log entries found.", parse_mode="HTML"
            )
            return

        entry    = newest.data[0]
        log_id   = entry["log_id"]
        name     = mention_user(entry["dim_roommates"]) if entry.get("dim_roommates") else "Unknown"
        task     = html.escape(entry["dim_tasks"]["task_description"]) if entry.get("dim_tasks") else "Task"
        date_fmt = datetime.strptime(entry["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b %Y")
        week     = entry.get("week_number", "N/A")

        supabase.table("fct_cleaning_logs").delete().eq("log_id", log_id).execute()
        log_to_db("WARNING",
                  f"Admin deleted log #{log_id}: "
                  f"{entry.get('dim_roommates', {}).get('name', '?')} / {week}")

        await update.message.reply_text(
            f"🗑️ <b>Log Deleted</b>\n\n"
            f"✅ Entry removed.\n"
            f"• Log ID: #{log_id}\n"
            f"• By: {name}\n"
            f"• Task: {task}\n"
            f"• Date: {date_fmt}  ({week})",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_deletelast", e)
        log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    done_filter = filters.Chat(chat_id=GROUP_ID) & filters.Regex(r"(?i)\bdone\b")
    app.add_handler(MessageHandler(done_filter, handle_done_command))
    app.add_handler(CommandHandler("hi",        handle_hi))
    app.add_handler(CommandHandler("help",      handle_help))
    app.add_handler(CommandHandler("status",    get_status))
    app.add_handler(CommandHandler("next",      handle_next))
    app.add_handler(CommandHandler("last",      handle_last))
    app.add_handler(CommandHandler("vacation",  set_vacation))
    app.add_handler(CommandHandler("back",      set_back))
    app.add_handler(CommandHandler("volunteer", handle_volunteer))
    app.add_handler(CommandHandler("activate",   handle_activate))
    app.add_handler(CommandHandler("deletelast", handle_deletelast))

    logger.info(f"DEV bot running (polling) | env={BOT_ENV}")
    app.run_polling()
