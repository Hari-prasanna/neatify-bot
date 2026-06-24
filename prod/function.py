# prod/function.py — AWS Lambda webhook handler.
# deploy.yml copies src/ here before sam build so `from src.utils import ...` works on Lambda.
#
# Channel split:
#   Private DMs  → all commands and "done" are handled; "done" also broadcasts to GROUP_ID
#   Group chat   → only "done" and /hi, /activate, /deletelast are accepted
#   Everything else in the group gets a redirect warning (use DMs)

import os
import sys
import json
import html
import logging
import asyncio
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

import telegram
from telegram.request import HTTPXRequest

try:
    from src.constants import TASK_ENTIRE_HOME, TASK_BATHROOM, GROUP_ONLY_WARN
    from src.utils import (
        supabase, get_current_week, get_weekend_dates_from_week,
        log_to_db, find_next_person, peek_next_person_id, mention_user,
        is_admin, get_last_cleaned_date,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.constants import TASK_ENTIRE_HOME, TASK_BATHROOM, GROUP_ONLY_WARN
    from src.utils import (
        supabase, get_current_week, get_weekend_dates_from_week,
        log_to_db, find_next_person, peek_next_person_id, mention_user,
        is_admin, get_last_cleaned_date,
    )

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", "0"))


@dataclass(frozen=True)
class MessageContext:
    bot: telegram.Bot
    chat_id: int
    user_id: int
    username: str | None
    first_name: str
    is_private: bool
    week_str: str
    raw_text: str
    text_lower: str


# ── Command handlers ──────────────────────────────────────────────────────────


async def _handle_hi(ctx: MessageContext) -> None:
    name = html.escape(ctx.first_name)
    try:
        existing = supabase.table("dim_roommates").select("*").eq("telegram_id", ctx.user_id).execute()
        if existing.data:
            roomie = existing.data[0]
            if ctx.username:
                supabase.table("dim_roommates").update({"telegram_username": ctx.username}).eq(
                    "roommate_id", roomie["roommate_id"]
                ).execute()
            uline = f"• Username: @{ctx.username}" if ctx.username else "• No public @username set"
            msg = (
                f"👤 <b>Registration</b>\n\n"
                f"✅ Already registered!\n"
                f"• Name: {html.escape(roomie['name'])}\n"
                f"• Tg ID: <code>{ctx.user_id}</code>\n"
                f"{uline}"
            )
        else:
            supabase.table("dim_roommates").insert({
                "name": name, "telegram_id": ctx.user_id,
                "telegram_username": ctx.username,
                "is_active": False, "is_on_vacation": False,
            }).execute()
            log_to_db("INFO", f"New registration: {name} (tg_id={ctx.user_id})")
            msg = (
                f"👤 <b>Registration</b>\n\n"
                f"✅ Registered!\n"
                f"• Name: {name}\n"
                f"• Tg ID: <code>{ctx.user_id}</code>\n\n"
                f"⏳ Ask the admin to activate your account."
            )
    except Exception:
        log_to_db("ERROR", f"handle_hi failed for user_id={ctx.user_id}",
                  error_details=traceback.format_exc())
        msg = (
            f"👤 <b>Registration</b>\n\n"
            f"⚠️ Auto-save failed — share this ID with the admin.\n"
            f"• Tg ID: <code>{ctx.user_id}</code>"
        )
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_help(ctx: MessageContext) -> None:
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
        "• /skip — Skip your next turn\n"
        "• /back — Return (priority pass granted)\n\n"
        "<b>Getting Started</b>\n"
        "• /hi — Register your Telegram ID\n"
        "• /help — This message"
    )
    if is_admin(ctx.user_id):
        msg += (
            "\n\n<b>🔧 Admin Tools</b>\n"
            "• /activate [tg_id] [order] [task_id]\n"
            "• /deletelast — Remove newest log entry"
        )
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_status(ctx: MessageContext) -> None:
    try:
        res = supabase.table("dim_roommates").select(
            "roommate_id, name, telegram_id, telegram_username, is_on_vacation"
        ).order("name").execute()
        lines = []
        for roommate_row in res.data:
            icon = "🌴" if roommate_row["is_on_vacation"] else "✅"
            last = get_last_cleaned_date(roommate_row["roommate_id"])
            lines.append(f"{icon} {mention_user(roommate_row)}  —  last cleaned {last}")
        msg = (
            f"📊 <b>House Status</b>\n"
            f"• Weekend: {get_weekend_dates_from_week(ctx.week_str)}\n\n"
            + ("\n".join(lines) if lines else "No roommates found.")
        )
    except Exception:
        log_to_db("ERROR", "get_status failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_next(ctx: MessageContext) -> None:
    try:
        home_config   = find_next_person(TASK_ENTIRE_HOME)
        bath_config   = find_next_person(TASK_BATHROOM)
        home_handle   = mention_user(home_config["dim_roommates"]) if home_config else "No one scheduled"
        bath_handle   = mention_user(bath_config["dim_roommates"]) if bath_config else "No one scheduled"
        current_dates = get_weekend_dates_from_week(ctx.week_str)

        msg = (
            f"📅 <b>Upcoming Schedule</b>\n\n"
            f"• 🏠 Home: {home_handle}\n"
            f"• 🚿 Bathroom: {bath_handle}\n"
            f"• 📆 Weekend: {current_dates}"
        )

        # Personal countdown — fetch caller's rotation slot
        try:
            caller_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username"
            ).eq("telegram_id", ctx.user_id).execute()

            if caller_res.data:
                caller         = caller_res.data[0]
                caller_rid     = caller["roommate_id"]
                caller_mention = mention_user(caller)

                caller_cfg = supabase.table("rotation_config").select(
                    "sequence_order, task_id"
                ).eq("roommate_id", caller_rid).limit(1).execute()

                if caller_cfg.data:
                    caller_seq  = caller_cfg.data[0]["sequence_order"]
                    caller_task = caller_cfg.data[0]["task_id"]
                    track_next  = home_config if caller_task == TASK_ENTIRE_HOME else bath_config

                    if track_next:
                        next_rid    = track_next["dim_roommates"]["roommate_id"]
                        is_priority = track_next["dim_roommates"].get("is_priority_next", False)

                        if caller_rid == next_rid:
                            msg += f"\n\n🔔 {caller_mention} — your turn: {current_dates} (this weekend!)"
                        elif not is_priority:
                            steps       = (caller_seq - track_next["sequence_order"]) % 5 or 5
                            target_week = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
                            caller_dates = get_weekend_dates_from_week(target_week)
                            msg += (
                                f"\n\n🕒 {caller_mention} — your turn: {caller_dates}"
                                f" (in {steps} week{'s' if steps != 1 else ''})"
                            )
        except Exception:
            log_to_db("ERROR", "handle_next personal section failed",
                      error_details=traceback.format_exc())

    except Exception:
        log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_last(ctx: MessageContext) -> None:
    try:
        res = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name, telegram_id, telegram_username), dim_tasks(task_description)")
            .order("cleaned_at", desc=True).limit(3).execute()
        )
        if not res.data:
            msg = "🕒 <b>Recent Activity</b>\n\nNo cleaning logs yet."
        else:
            lines = []
            for entry in res.data:
                date_str = datetime.strptime(entry["cleaned_at"].split("T")[0], "%Y-%m-%d").strftime("%d %b")
                name     = mention_user(entry["dim_roommates"]) if entry.get("dim_roommates") else "Unknown"
                task     = html.escape(entry["dim_tasks"]["task_description"]) if entry.get("dim_tasks") else "Task"
                week     = entry.get("week_number", "")
                weekend  = f"  ({get_weekend_dates_from_week(week)})" if week else ""
                lines.append(f"• {date_str}  {name}  ({task}){weekend}")
            msg = "🕒 <b>Recent Activity</b>\n\n" + "\n".join(lines)
    except Exception:
        log_to_db("ERROR", "handle_last failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_vacation(ctx: MessageContext) -> None:
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq(
            "telegram_id", ctx.user_id
        ).execute()
        if res.data:
            msg = (
                f"🌴 <b>Vacation Mode</b>\n\n"
                f"✅ Marked as away.\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"Both rotation tracks will skip you.\n"
                f"Use /back when you return — you'll jump to the front."
            )
        else:
            msg = "🌴 <b>Vacation Mode</b>\n\n❌ ID not found. Send /hi to register first."
    except Exception:
        log_to_db("ERROR", "set_vacation failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_skip(ctx: MessageContext) -> None:
    try:
        res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq(
            "telegram_id", ctx.user_id
        ).execute()
        if res.data:
            log_to_db("INFO", f"{res.data[0]['name']} used /skip — marked is_on_vacation=True")
            msg = (
                f"⏭️ <b>Skip Turn</b>\n\n"
                f"✅ You've been skipped for the next rotation.\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"Both tracks will skip you until you run /back."
            )
        else:
            msg = "⏭️ <b>Skip Turn</b>\n\n❌ ID not found. Send /hi to register first."
    except Exception:
        log_to_db("ERROR", "handle_skip failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_back(ctx: MessageContext) -> None:
    try:
        res = supabase.table("dim_roommates").update({
            "is_on_vacation": False, "is_priority_next": True,
        }).eq("telegram_id", ctx.user_id).execute()
        if res.data:
            log_to_db("INFO", f"{res.data[0]['name']} returned — is_priority_next set")
            msg = (
                f"🏠 <b>Back in Rotation</b>\n\n"
                f"✅ Welcome back!\n"
                f"• User: {mention_user(res.data[0])}\n\n"
                f"⚡ Priority pass active — front of queue next turn."
            )
        else:
            msg = "🏠 <b>Back in Rotation</b>\n\n❌ ID not found. Send /hi to register first."
    except Exception:
        log_to_db("ERROR", "set_back failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_volunteer(ctx: MessageContext) -> None:
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq(
            "telegram_id", ctx.user_id
        ).execute()
        if not roomie_res.data:
            msg = "🌟 <b>Volunteer</b>\n\n❌ ID not found. Send /hi to register first."
        else:
            roomie = roomie_res.data[0]
            # Use the volunteer's own assigned task so the skip is task-specific.
            cfg_res = (
                supabase.table("rotation_config").select("task_id")
                .eq("roommate_id", roomie["roommate_id"]).limit(1).execute()
            )
            volunteer_task_id = cfg_res.data[0]["task_id"] if cfg_res.data else TASK_ENTIRE_HOME
            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie["roommate_id"], "task_id": volunteer_task_id,
                "week_number": ctx.week_str, "is_volunteer": True,
            }).execute()
            supabase.table("dim_roommates").update({
                "skip_next_turn": True, "skip_next_turn_task_id": volunteer_task_id,
            }).eq("roommate_id", roomie["roommate_id"]).execute()
            log_to_db("INFO", f"Volunteer + skip_next_turn(task={volunteer_task_id}) set for {roomie['name']}")
            msg = (
                f"🌟 <b>Volunteer Clean Logged</b>\n\n"
                f"✅ Extra clean recorded!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Weekend: {get_weekend_dates_from_week(ctx.week_str)}\n\n"
                f"🎟️ Skip pass granted — next scheduled turn is waived."
            )
    except Exception:
        log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_activate(ctx: MessageContext) -> None:
    if not is_admin(ctx.user_id):
        await ctx.bot.send_message(chat_id=ctx.chat_id,
                                   text="🔧 <b>Activation</b>\n\n🚫 Admin only.", parse_mode="HTML")
        return

    args = ctx.raw_text.split()[1:]
    if len(args) != 3:
        await ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text="🔧 <b>Activation</b>\n\nUsage: /activate [tg_id] [order] [task_id]\nExample: /activate 123456789 3 1",
            parse_mode="HTML"
        )
        return

    try:
        target_tg_id = int(args[0])
        seq_order    = int(args[1])
        task_id      = int(args[2])
    except ValueError:
        await ctx.bot.send_message(chat_id=ctx.chat_id,
                                   text="🔧 <b>Activation</b>\n\n❌ All three args must be integers.",
                                   parse_mode="HTML")
        return

    try:
        user_res = supabase.table("dim_roommates").select("*").eq(
            "telegram_id", target_tg_id
        ).execute()
        if not user_res.data:
            await ctx.bot.send_message(
                chat_id=ctx.chat_id,
                text=f"🔧 <b>Activation</b>\n\n❌ No user with Tg ID {target_tg_id}. Have them send /hi first.",
                parse_mode="HTML"
            )
            return

        target     = user_res.data[0]
        target_rid = target["roommate_id"]
        supabase.table("dim_roommates").update({"is_active": True}).eq(
            "roommate_id", target_rid
        ).execute()

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
                await ctx.bot.send_message(
                    chat_id=ctx.chat_id,
                    text=f"🔧 <b>Activation</b>\n\n⚠️ Slot {seq_order} (task {task_id}) taken by {taken_by}.",
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
        msg = (
            f"🔧 <b>Activation</b>\n\n"
            f"✅ {html.escape(target['name'])} activated!\n"
            f"• Task ID: {task_id}\n"
            f"• Slot: {seq_order}  ({action_note})"
        )
    except Exception:
        log_to_db("ERROR", f"handle_activate failed tg_id={target_tg_id}",
                  error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_deletelast(ctx: MessageContext) -> None:
    if not is_admin(ctx.user_id):
        await ctx.bot.send_message(chat_id=ctx.chat_id,
                                   text="🗑️ <b>Delete Log</b>\n\n🚫 Admin only.", parse_mode="HTML")
        return
    try:
        newest = (
            supabase.table("fct_cleaning_logs")
            .select("*, dim_roommates(name, telegram_id, telegram_username), dim_tasks(task_description)")
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if not newest.data:
            msg = "🗑️ <b>Delete Log</b>\n\n📭 No log entries found."
        else:
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
            msg = (
                f"🗑️ <b>Log Deleted</b>\n\n"
                f"✅ Entry removed.\n"
                f"• Log ID: #{log_id}\n"
                f"• By: {name}\n"
                f"• Task: {task}\n"
                f"• Date: {date_fmt}  ({get_weekend_dates_from_week(week)})"
            )
    except Exception:
        log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
        msg = "⚠️ Something went wrong. Check CloudWatch logs."
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_done(ctx: MessageContext) -> None:
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", ctx.user_id).execute()
        if not roomie_res.data:
            await ctx.bot.send_message(
                chat_id=ctx.chat_id,
                text="🧹 <b>Clean Log</b>\n\n❌ Unrecognized user. Send /hi to register first.",
                parse_mode="HTML"
            )
            return

        roomie    = roomie_res.data[0]
        roomie_id = roomie["roommate_id"]

        # Only count scheduled cleans (is_volunteer=False) — volunteer logs must not block done.
        existing = (
            supabase.table("fct_cleaning_logs").select("log_id")
            .eq("roommate_id", roomie_id)
            .eq("week_number", ctx.week_str)
            .eq("is_volunteer", False)
            .execute()
        )
        if existing.data:
            await ctx.bot.send_message(
                chat_id=ctx.chat_id,
                text=(
                    f"🧹 <b>Clean Log</b>\n\n"
                    f"✨ Already logged this week!\n"
                    f"• By: {mention_user(roomie)}\n"
                    f"• Weekend: {get_weekend_dates_from_week(ctx.week_str)}"
                ),
                parse_mode="HTML"
            )
            return

        config_res = (
            supabase.table("rotation_config").select("*, dim_tasks(task_description)")
            .eq("roommate_id", roomie_id).limit(1).execute()
        )
        if not config_res.data:
            await ctx.bot.send_message(
                chat_id=ctx.chat_id,
                text="🧹 <b>Clean Log</b>\n\n❌ No task assigned. Ask the admin to run /activate.",
                parse_mode="HTML"
            )
            return

        task_row   = config_res.data[0]
        task_id    = task_row["task_id"]
        task_desc  = html.escape(task_row["dim_tasks"]["task_description"])
        caller_seq = task_row["sequence_order"]

        # Turn guard: read-only check, does NOT consume skip_next_turn flags.
        expected_rid = peek_next_person_id(task_id)
        if expected_rid is not None and expected_rid != roomie_id:
            block_msg = "🧹 <b>Clean Log</b>\n\n⚠️ It's not your turn yet!\n"
            try:
                next_cfg = (
                    supabase.table("rotation_config").select("sequence_order")
                    .eq("roommate_id", expected_rid).eq("task_id", task_id).execute()
                )
                if next_cfg.data:
                    next_seq    = next_cfg.data[0]["sequence_order"]
                    steps       = (caller_seq - next_seq) % 5 or 5
                    target_week = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
                    block_msg  += f"• Your next turn: {get_weekend_dates_from_week(target_week)}"
            except Exception:
                log_to_db("ERROR", "done turn-guard calc failed", error_details=traceback.format_exc())
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=block_msg, parse_mode="HTML")
            return

        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, "task_id": task_id, "week_number": ctx.week_str,
        }).execute()
        log_to_db("INFO", f"Clean logged: {roomie['name']} / {task_desc} / {ctx.week_str}")

        # Consume priority pass — one-time use
        supabase.table("dim_roommates").update({"is_priority_next": False}).eq(
            "roommate_id", roomie_id
        ).execute()

        next_config = find_next_person(task_id)
        next_handle = mention_user(next_config["dim_roommates"]) if next_config else "No one available"
        weekend     = get_weekend_dates_from_week(ctx.week_str)

        if ctx.is_private:
            # DM: send private confirmation, then broadcast to the group
            private_msg = (
                f"🧹 <b>Clean Logged</b>\n\n"
                f"✅ Logged!\n"
                f"• Task: {task_desc}\n"
                f"• Weekend: {weekend}\n\n"
                f"🔔 Next ({task_desc}): {next_handle}"
            )
            broadcast_msg = (
                f"📢 <b>Cleaning Update</b>\n\n"
                f"{mention_user(roomie)} has completed <b>{task_desc}</b>!\n"
                f"🔔 Next up: {next_handle} — {weekend}"
            )
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=private_msg, parse_mode="HTML")
            await ctx.bot.send_message(chat_id=GROUP_ID, text=broadcast_msg, parse_mode="HTML")
        else:
            # Group: reply in-place (kept for users who still type there)
            msg = (
                f"🧹 <b>Clean Logged</b>\n\n"
                f"✅ Logged!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Weekend: {weekend}\n"
                f"• Task: {task_desc}\n\n"
                f"🔔 Next ({task_desc}): {next_handle}"
            )
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")

    except Exception:
        log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
        await ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text="⚠️ Something went wrong. Check CloudWatch logs.",
            parse_mode="HTML"
        )


# ── Dispatcher ────────────────────────────────────────────────────────────────


async def process_update(event: dict, bot: telegram.Bot) -> dict:
    headers  = event.get("headers", {})
    expected = os.environ.get("TELEGRAM_SECRET_TOKEN")
    if expected and headers.get("x-telegram-bot-api-secret-token") != expected:
        logger.warning("Unauthorized — secret token mismatch.")
        return {"statusCode": 403, "body": "Forbidden"}

    body   = json.loads(event.get("body", "{}"))
    update = telegram.Update.de_json(body, bot)

    if not update.message or not update.message.text or not update.message.from_user:
        return {"statusCode": 200}

    sender     = update.message.from_user
    raw_text   = update.message.text.strip()
    text_lower = raw_text.lower()

    ctx = MessageContext(
        bot        = bot,
        chat_id    = update.message.chat.id,
        user_id    = sender.id,
        username   = sender.username,
        first_name = sender.first_name or sender.username or "Unknown",
        is_private = update.message.chat.type == "private",
        week_str   = get_current_week(),
        raw_text   = raw_text,
        text_lower = text_lower,
    )

    # /hi works everywhere — needed for group registration flow
    if text_lower == "/hi":
        await _handle_hi(ctx)
        return {"statusCode": 200}

    # Admin commands work everywhere
    if text_lower.startswith("/activate"):
        await _handle_activate(ctx)
        return {"statusCode": 200}

    if text_lower == "/deletelast":
        await _handle_deletelast(ctx)
        return {"statusCode": 200}

    # "done" is accepted from private DMs and the group
    if text_lower == "done":
        await _handle_done(ctx)
        return {"statusCode": 200}

    # All remaining commands require a private DM
    if not ctx.is_private:
        await bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return {"statusCode": 200}

    dm_routes = {
        "/help":      _handle_help,
        "/status":    _handle_status,
        "/next":      _handle_next,
        "/last":      _handle_last,
        "/vacation":  _handle_vacation,
        "/skip":      _handle_skip,
        "/back":      _handle_back,
        "/volunteer": _handle_volunteer,
    }
    handler = dm_routes.get(text_lower)
    if handler:
        await handler(ctx)

    return {"statusCode": 200}


# ── Lambda entry point ────────────────────────────────────────────────────────


def lambda_handler(event, _context) -> dict:
    # HTTPXRequest is required — Lambda has no running event loop at import time.
    bot = telegram.Bot(
        token=os.environ.get("TELEGRAM_TOKEN"),
        request=HTTPXRequest(connection_pool_size=8, connect_timeout=15.0)
    )
    try:
        response = asyncio.run(process_update(event, bot))
        return response if response else {"statusCode": 200, "body": "OK"}
    except Exception as e:
        logger.error(f"Lambda handler error: {e}")
        # Always return 200 — non-200 makes Telegram retry the same update forever.
        return {"statusCode": 200, "body": json.dumps("Error handled")}
