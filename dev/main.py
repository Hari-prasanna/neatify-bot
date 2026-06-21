# dev/main.py — Local polling bot. Run with: python dev/main.py
#
# Channel split:
#   Private DMs  → all commands and "done" are handled here; "done" also broadcasts to GROUP_ID
#   Group chat   → only "done" is accepted; slash commands get a redirect warning
#   /hi, /activate, /deletelast work in both contexts

import os
import sys
import html
import logging
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from src.utils import (
    supabase, BOT_ENV, get_current_week, get_weekend_dates_from_week,
    log_to_db, log_interaction, peek_next_person_id, consume_skips_up_to,
    peek_next_n_persons, mention_user, compute_turn_offset,
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

# Sent when a user runs a slash command inside the group instead of a private DM
_GROUP_WARN = (
    "📱 Please send commands to me in a <b>private message</b> to keep this chat clean.\n"
    "Start a DM with me and send the same command there."
)


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
    # /hi works everywhere — needed for group registration flow
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


async def handle_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id    = update.message.from_user.id
    week_str   = get_current_week()
    is_private = update.message.chat.type == "private"

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

        # Only count scheduled cleans (is_volunteer=False) — volunteer logs must not block done.
        existing = (
            supabase.table("fct_cleaning_logs").select("log_id")
            .eq("roommate_id", roomie_id)
            .eq("week_number", week_str)
            .eq("is_volunteer", False)
            .execute()
        )
        if existing.data:
            await update.message.reply_text(
                f"🧹 <b>Clean Log</b>\n\n"
                f"✨ Already logged this week!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Weekend: {get_weekend_dates_from_week(week_str)}",
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

        # Turn guard: read-only check, does NOT decrement skip_turn_count.
        expected_rid = peek_next_person_id(task_id)
        if expected_rid is not None and expected_rid != roomie_id:
            block_msg = "🧹 <b>Clean Log</b>\n\n⚠️ It's not your turn yet!\n"
            try:
                steps       = compute_turn_offset(
                    roomie_id, roomie.get("skip_turn_count") or 0,
                    expected_rid, task_id,
                )
                target_week = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
                block_msg  += f"• Your next turn: {get_weekend_dates_from_week(target_week)}"
            except Exception as e:
                print(f"\n[handle_done_command] turn guard calc: {type(e).__name__}: {e}", flush=True)
            await update.message.reply_text(block_msg, parse_mode="HTML")
            return

        # Capture volunteer state before clearing — needed to pass priority to the replaced person
        is_volunteer_priority = bool(roomie.get("is_priority_next")) and bool(roomie.get("volunteer_replacing_id"))
        replacing_rid         = roomie.get("volunteer_replacing_id")

        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, "task_id": task_id, "week_number": week_str,
        }).execute()
        log_to_db("INFO", f"Clean logged: {roomie['name']} / {task_desc} / {week_str}")

        # Consume priority pass — one-time use; clear replacing pointer too
        supabase.table("dim_roommates").update({
            "is_priority_next":       False,
            "volunteer_replacing_id": None,
        }).eq("roommate_id", roomie_id).execute()

        # If this was a volunteer doing their donated clean, restore priority to the original person
        if is_volunteer_priority and replacing_rid:
            supabase.table("dim_roommates").update({"is_priority_next": True}).eq(
                "roommate_id", replacing_rid
            ).execute()
            log_to_db("INFO",
                      f"Priority passed to roommate_id={replacing_rid} after volunteer done ({roomie['name']})")

        consume_skips_up_to(task_id, roomie_id)
        next_persons = peek_next_n_persons(task_id, 1)
        next_handle  = mention_user(next_persons[0]) if next_persons else "No one available"
        weekend      = get_weekend_dates_from_week(week_str)
        next_weekend = get_weekend_dates_from_week(
            (datetime.now() + timedelta(weeks=1)).strftime("%Y-W%V")
        )

        vol_line    = "\n🎟️ Volunteer logged — your banked skip still applies." if is_volunteer_priority else ""
        private_msg = (
            f"🧹 <b>Clean Logged</b>\n\n"
            f"✅ Logged!\n"
            f"• Task: {task_desc}\n"
            f"• Weekend: {weekend}{vol_line}\n\n"
            f"🔔 Next ({task_desc}): {next_handle}"
        )
        # Public broadcast shown to the whole house
        broadcast_msg = (
            f"📢 <b>Cleaning Update</b>\n\n"
            f"{mention_user(roomie)} has completed <b>{task_desc}</b>!\n"
            f"🔔 Next up: {next_handle} — {next_weekend}"
        )

        await update.message.reply_text(private_msg, parse_mode="HTML")
        if is_private:
            # Send the group announcement separately when "done" comes from a DM
            await context.bot.send_message(chat_id=GROUP_ID, text=broadcast_msg, parse_mode="HTML")

    except Exception as e:
        _err("handle_done_command", e)
        log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_next(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

    user_id  = update.message.from_user.id
    week_str = get_current_week()

    try:
        home_turns = peek_next_n_persons(TASK_ENTIRE_HOME, 3)
        bath_turns = peek_next_n_persons(TASK_BATHROOM, 3)

        def _turns_section(turns: list[dict]) -> str:
            if not turns:
                return "• No one scheduled"
            lines = []
            for i, person in enumerate(turns):
                handle = mention_user(person)
                w      = (datetime.now() + timedelta(weeks=i)).strftime("%Y-W%V")
                dates  = get_weekend_dates_from_week(w)
                prefix = f"• 🔔 {handle} — this weekend ({dates})" if i == 0 else f"• {handle} — {dates}"
                lines.append(prefix)
            return "\n".join(lines)

        msg = (
            f"📅 <b>Upcoming Schedule</b>\n\n"
            f"🏠 <b>Entire Home</b>\n{_turns_section(home_turns)}\n\n"
            f"🚿 <b>Bathroom</b>\n{_turns_section(bath_turns)}"
        )

        # Personal countdown — fetch caller's rotation slot and banked skips
        try:
            caller_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).eq("telegram_id", user_id).execute()

            if caller_res.data:
                caller         = caller_res.data[0]
                caller_rid     = caller["roommate_id"]
                caller_mention = mention_user(caller)

                caller_cfg = supabase.table("rotation_config").select(
                    "task_id"
                ).eq("roommate_id", caller_rid).limit(1).execute()

                if caller_cfg.data:
                    caller_task = caller_cfg.data[0]["task_id"]
                    track_turns = home_turns if caller_task == TASK_ENTIRE_HOME else bath_turns

                    if track_turns:
                        next_rid = track_turns[0]["roommate_id"]
                        if caller_rid == next_rid:
                            current_dates = get_weekend_dates_from_week(week_str)
                            msg += f"\n\n🔔 {caller_mention} — your turn: {current_dates} (this weekend!)"
                        else:
                            steps        = compute_turn_offset(
                                caller_rid, caller.get("skip_turn_count") or 0,
                                next_rid,   caller_task,
                            )
                            target_week  = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
                            caller_dates = get_weekend_dates_from_week(target_week)
                            msg += (
                                f"\n\n🕒 {caller_mention} — your turn: {caller_dates}"
                                f" (in {steps} week{'s' if steps != 1 else ''})"
                            )
        except Exception as e:
            print(f"\n[handle_next] personal section: {type(e).__name__}: {e}", flush=True)

        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        _err("handle_next", e)
        log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def get_status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

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
            f"• Weekend: {get_weekend_dates_from_week(week_str)}\n\n"
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
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

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
            name     = mention_user(entry["dim_roommates"]) if entry.get("dim_roommates") else "Unknown"
            task     = html.escape(entry["dim_tasks"]["task_description"]) if entry.get("dim_tasks") else "Task"
            week     = entry.get("week_number", "")
            weekend  = f"  ({get_weekend_dates_from_week(week)})" if week else ""
            lines.append(f"• {date_str}  {name}  ({task}){weekend}")

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
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

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
                f"Both rotation tracks will skip you.\n"
                f"Use /back when you return — you'll jump to the front.",
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


async def handle_skip(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    # /skip is an alias for /vacation — marks you away for the next cycle
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq(
            "telegram_id", user_id
        ).execute()
        if res.data:
            log_to_db("INFO", f"{res.data[0]['name']} used /skip — marked is_on_vacation=True")
            await update.message.reply_text(
                f"⏭️ <b>Skip Turn</b>\n\n"
                f"✅ You've been skipped for the next rotation.\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"Both tracks will skip you until you run /back.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "⏭️ <b>Skip Turn</b>\n\n❌ ID not found. Send /hi to register first.",
                parse_mode="HTML"
            )
    except Exception as e:
        _err("handle_skip", e)
        log_to_db("ERROR", "handle_skip failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def set_back(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

    user_id = update.message.from_user.id
    try:
        res = supabase.table("dim_roommates").update({
            "is_on_vacation": False, "is_priority_next": True,
        }).eq("telegram_id", user_id).execute()

        if res.data:
            log_to_db("INFO", f"{res.data[0]['name']} returned — is_priority_next set")
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
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

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
        # Use the volunteer's own assigned task so the skip is task-specific.
        cfg_res = (
            supabase.table("rotation_config").select("task_id")
            .eq("roommate_id", roomie["roommate_id"]).limit(1).execute()
        )
        vol_task_id = cfg_res.data[0]["task_id"] if cfg_res.data else TASK_ENTIRE_HOME

        # Who is currently scheduled? Store them so priority can be handed back after volunteer's done.
        replacing_rid = peek_next_person_id(vol_task_id)
        if replacing_rid == roomie["roommate_id"]:
            replacing_rid = None  # Volunteer IS the scheduled person — no hand-off needed

        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie["roommate_id"], "task_id": vol_task_id,
            "week_number": week_str, "is_volunteer": True,
        }).execute()
        new_skip_count = (roomie.get("skip_turn_count") or 0) + 1
        supabase.table("dim_roommates").update({
            "skip_turn_count":        new_skip_count,
            "skip_next_turn_task_id": vol_task_id,
            "is_priority_next":       True,          # Lets volunteer pass the turn guard on 'done'
            "volunteer_replacing_id": replacing_rid, # Who to restore priority to after done
        }).eq("roommate_id", roomie["roommate_id"]).execute()
        log_to_db("INFO",
                  f"Volunteer: skip_turn_count → {new_skip_count} (task={vol_task_id}) "
                  f"for {roomie['name']}, replacing_rid={replacing_rid}")

        skip_word  = "skip pass" if new_skip_count == 1 else f"{new_skip_count} skip passes"
        skip_turns = "next turn" if new_skip_count == 1 else f"next {new_skip_count} turns"
        await update.message.reply_text(
            f"🌟 <b>Volunteer Clean Logged</b>\n\n"
            f"✅ Extra clean recorded!\n"
            f"• By: {mention_user(roomie)}\n"
            f"• Weekend: {get_weekend_dates_from_week(week_str)}\n\n"
            f"🎟️ {skip_word} banked — {skip_turns} waived.",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_volunteer", e)
        log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_myturn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

    user_id = update.message.from_user.id
    try:
        if context.args:
            target_username = context.args[0].lstrip("@")
            target_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).ilike("telegram_username", target_username).execute()
            if not target_res.data:
                await update.message.reply_text(
                    f"🕒 <b>Turn Lookup</b>\n\n"
                    f"❌ No user found with username @{html.escape(target_username)}.\n"
                    f"Make sure they've sent /hi so their @username is registered.",
                    parse_mode="HTML"
                )
                return
            target = target_res.data[0]
        else:
            target_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).eq("telegram_id", user_id).execute()
            if not target_res.data:
                await update.message.reply_text(
                    "🕒 <b>Turn Lookup</b>\n\n❌ Not registered. Send /hi first.",
                    parse_mode="HTML"
                )
                return
            target = target_res.data[0]

        target_rid     = target["roommate_id"]
        target_mention = mention_user(target)
        week_str       = get_current_week()

        slots = (
            supabase.table("rotation_config")
            .select("task_id, sequence_order, dim_tasks(task_description)")
            .eq("roommate_id", target_rid).execute()
        )
        if not slots.data:
            await update.message.reply_text(
                f"🕒 <b>Turn Lookup</b>\n\n{target_mention} has no tasks assigned.",
                parse_mode="HTML"
            )
            return

        lines = []
        for slot in slots.data:
            task_id   = slot["task_id"]
            task_seq  = slot["sequence_order"]
            task_desc = html.escape(slot["dim_tasks"]["task_description"])

            next_rid = peek_next_person_id(task_id)

            if next_rid is None:
                lines.append(f"• <b>{task_desc}</b>: no one scheduled right now")
                continue

            if next_rid == target_rid:
                weekend = get_weekend_dates_from_week(week_str)
                lines.append(f"• <b>{task_desc}</b>: 🔔 this weekend! ({weekend})")
                continue

            next_cfg = (
                supabase.table("rotation_config").select("sequence_order")
                .eq("roommate_id", next_rid).eq("task_id", task_id).execute()
            )
            if not next_cfg.data:
                lines.append(f"• <b>{task_desc}</b>: schedule unavailable")
                continue

            steps        = compute_turn_offset(
                target_rid, target.get("skip_turn_count") or 0,
                next_rid,   task_id,
            )
            target_week  = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
            dates        = get_weekend_dates_from_week(target_week)
            week_word    = "week" if steps == 1 else "weeks"
            lines.append(f"• <b>{task_desc}</b>: in {steps} {week_word} ({dates})")

        await update.message.reply_text(
            f"🕒 <b>Turn Lookup</b>\n\n{target_mention}\n\n" + "\n".join(lines),
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_myturn", e)
        log_to_db("ERROR", "handle_myturn failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def handle_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type != "private":
        await update.message.reply_text(_GROUP_WARN, parse_mode="HTML")
        return

    user_id = update.message.from_user.id
    msg = (
        "📋 <b>Command Reference</b>\n\n"
        "<b>Cleaning Rotation</b>\n"
        "• done — Log your clean for this week\n"
        "• /volunteer — Bonus clean + earn a skip pass\n"
        "• /next — Upcoming schedule (both tracks)\n"
        "• /myturn — Your turn countdown (or /myturn @name)\n"
        "• /last — 3 most recent log entries\n\n"
        "<b>Your Status</b>\n"
        "• /status — Everyone's state + last cleaned\n"
        "• /vacation — Mark yourself away\n"
        "• /skip — Skip your next turn\n"
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
    # Admin command — works in both group and DM
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
    # Admin command — works in both group and DM
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
            f"• Week: <code>{week}</code>\n"
            f"• Date: {date_fmt}  ({get_weekend_dates_from_week(week)})",
            parse_mode="HTML"
        )

    except Exception as e:
        _err("handle_deletelast", e)
        log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
        await update.message.reply_text(
            "⚠️ Something went wrong — check the terminal for the full error.", parse_mode="HTML"
        )


async def _log_private_interaction(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user or not update.message.text:
        return
    user    = update.message.from_user
    name    = user.first_name or user.username or "Unknown"
    command = update.message.text.strip().split()[0]
    log_interaction(user.id, name, command)


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # Log every private text message before dispatching — runs in group=-1 ahead of all handlers
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, _log_private_interaction),
        group=-1
    )

    # Accept 'done' from private DMs and from the group — handled differently inside the handler
    done_filter = (filters.ChatType.PRIVATE | filters.Chat(chat_id=GROUP_ID)) & filters.Regex(r"(?i)^\s*done\s*$")
    app.add_handler(MessageHandler(done_filter, handle_done_command))

    app.add_handler(CommandHandler("hi",         handle_hi))
    app.add_handler(CommandHandler("help",       handle_help))
    app.add_handler(CommandHandler("status",     get_status))
    app.add_handler(CommandHandler("next",       handle_next))
    app.add_handler(CommandHandler("last",       handle_last))
    app.add_handler(CommandHandler("myturn",     handle_myturn))
    app.add_handler(CommandHandler("vacation",   set_vacation))
    app.add_handler(CommandHandler("skip",       handle_skip))
    app.add_handler(CommandHandler("back",       set_back))
    app.add_handler(CommandHandler("volunteer",  handle_volunteer))
    app.add_handler(CommandHandler("activate",   handle_activate))
    app.add_handler(CommandHandler("deletelast", handle_deletelast))

    logger.info(f"DEV bot running (polling) | env={BOT_ENV}")
    app.run_polling()
