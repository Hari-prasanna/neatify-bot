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
        log_to_db, log_interaction, is_admin, get_last_cleaned_date,
        peek_next_person_id, consume_skips_up_to,
        peek_next_n_persons, mention_user, compute_turn_offset,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.constants import TASK_ENTIRE_HOME, TASK_BATHROOM, GROUP_ONLY_WARN
    from src.utils import (
        supabase, get_current_week, get_weekend_dates_from_week,
        log_to_db, log_interaction, is_admin, get_last_cleaned_date,
        peek_next_person_id, consume_skips_up_to,
        peek_next_n_persons, mention_user, compute_turn_offset,
    )

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", "0"))


@dataclass(frozen=True)
class MessageContext:
    bot:        telegram.Bot
    chat_id:    int
    user_id:    int
    username:   str | None
    first_name: str
    is_private: bool
    week_str:   str
    raw_text:   str
    text:       str          # stripped + lowercased


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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
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
    if is_admin(ctx.user_id):
        msg += (
            "\n\n<b>🔧 Admin Tools</b>\n"
            "• /activate [tg_id] [order] [task_id]\n"
            "• /deletelast — Remove newest log entry"
        )
    await ctx.bot.send_message(chat_id=ctx.chat_id, text=msg, parse_mode="HTML")


async def _handle_status(ctx: MessageContext) -> None:
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
    try:
        res = supabase.table("dim_roommates").select(
            "roommate_id, name, telegram_id, telegram_username, is_on_vacation"
        ).order("name").execute()
        lines = []
        for r in res.data:
            icon = "🌴" if r["is_on_vacation"] else "✅"
            last = get_last_cleaned_date(r["roommate_id"])
            lines.append(f"{icon} {mention_user(r)}  —  last cleaned {last}")
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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
    try:
        home_turns = peek_next_n_persons(TASK_ENTIRE_HOME, 3)
        bath_turns = peek_next_n_persons(TASK_BATHROOM, 3)

        home_base = bath_base = 0
        try:
            if (supabase.table("fct_cleaning_logs").select("log_id")
                    .eq("week_number", ctx.week_str).eq("is_volunteer", False)
                    .eq("task_id", TASK_ENTIRE_HOME).execute()).data:
                home_base = 1
            if (supabase.table("fct_cleaning_logs").select("log_id")
                    .eq("week_number", ctx.week_str).eq("is_volunteer", False)
                    .eq("task_id", TASK_BATHROOM).execute()).data:
                bath_base = 1
        except Exception:
            pass

        def _turns_section(turns: list[dict], week_base: int) -> str:
            if not turns:
                return "• No one scheduled"
            lines = []
            for i, person in enumerate(turns):
                handle = mention_user(person)
                w      = (datetime.now() + timedelta(weeks=week_base + i)).strftime("%Y-W%V")
                dates  = get_weekend_dates_from_week(w)
                if i == 0:
                    label  = "next weekend" if week_base == 1 else "this weekend"
                    prefix = f"• 🔔 {handle} — {label} ({dates})"
                else:
                    prefix = f"• {handle} — {dates}"
                lines.append(prefix)
            return "\n".join(lines)

        msg = (
            f"📅 <b>Upcoming Schedule</b>\n\n"
            f"🏠 <b>Entire Home</b>\n{_turns_section(home_turns, home_base)}\n\n"
            f"🚿 <b>Bathroom</b>\n{_turns_section(bath_turns, bath_base)}"
        )

        try:
            caller_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).eq("telegram_id", ctx.user_id).execute()
            if caller_res.data:
                caller         = caller_res.data[0]
                caller_rid     = caller["roommate_id"]
                caller_mention = mention_user(caller)
                caller_cfg     = supabase.table("rotation_config").select("task_id").eq(
                    "roommate_id", caller_rid
                ).limit(1).execute()
                if caller_cfg.data:
                    caller_task     = caller_cfg.data[0]["task_id"]
                    track_turns     = home_turns if caller_task == TASK_ENTIRE_HOME else bath_turns
                    track_week_base = home_base  if caller_task == TASK_ENTIRE_HOME else bath_base
                    if track_turns:
                        next_rid = track_turns[0]["roommate_id"]
                        if caller_rid == next_rid:
                            target_wk    = (datetime.now() + timedelta(weeks=track_week_base)).strftime("%Y-W%V")
                            caller_dates = get_weekend_dates_from_week(target_wk)
                            label        = "next weekend" if track_week_base == 1 else "this weekend"
                            msg += f"\n\n🔔 {caller_mention} — your turn: {caller_dates} ({label}!)"
                        else:
                            steps        = compute_turn_offset(
                                caller_rid, caller.get("skip_turn_count") or 0,
                                next_rid,   caller_task,
                            ) + track_week_base
                            target_week  = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
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


async def _handle_myturn(ctx: MessageContext) -> None:
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return

    myturn_args = ctx.raw_text.split()[1:]
    try:
        if myturn_args:
            target_username = myturn_args[0].lstrip("@")
            target_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).ilike("telegram_username", target_username).execute()
            if not target_res.data:
                await ctx.bot.send_message(
                    chat_id=ctx.chat_id,
                    text=(
                        f"🕒 <b>Turn Lookup</b>\n\n"
                        f"❌ No user found with username @{html.escape(target_username)}.\n"
                        f"Make sure they've sent /hi so their @username is registered."
                    ),
                    parse_mode="HTML"
                )
                return
            target = target_res.data[0]
        else:
            target_res = supabase.table("dim_roommates").select(
                "roommate_id, name, telegram_id, telegram_username, skip_turn_count"
            ).eq("telegram_id", ctx.user_id).execute()
            if not target_res.data:
                await ctx.bot.send_message(
                    chat_id=ctx.chat_id,
                    text="🕒 <b>Turn Lookup</b>\n\n❌ Not registered. Send /hi first.",
                    parse_mode="HTML"
                )
                return
            target = target_res.data[0]

        target_rid     = target["roommate_id"]
        target_mention = mention_user(target)

        slots = (
            supabase.table("rotation_config")
            .select("task_id, sequence_order, dim_tasks(task_description)")
            .eq("roommate_id", target_rid).execute()
        )
        if not slots.data:
            await ctx.bot.send_message(
                chat_id=ctx.chat_id,
                text=f"🕒 <b>Turn Lookup</b>\n\n{target_mention} has no tasks assigned.",
                parse_mode="HTML"
            )
            return

        lines = []
        for slot in slots.data:
            slot_task_id = slot["task_id"]
            slot_desc    = html.escape(slot["dim_tasks"]["task_description"])
            next_rid     = peek_next_person_id(slot_task_id)

            if next_rid is None:
                lines.append(f"• <b>{slot_desc}</b>: no one scheduled right now")
                continue

            done_chk  = (
                supabase.table("fct_cleaning_logs").select("log_id")
                .eq("week_number", ctx.week_str).eq("is_volunteer", False)
                .eq("task_id", slot_task_id).execute()
            )
            week_base = 1 if done_chk.data else 0

            if next_rid == target_rid:
                target_wk = (datetime.now() + timedelta(weeks=week_base)).strftime("%Y-W%V")
                weekend   = get_weekend_dates_from_week(target_wk)
                label     = "next weekend" if week_base == 1 else "this weekend"
                lines.append(f"• <b>{slot_desc}</b>: 🔔 {label}! ({weekend})")
                continue

            steps       = compute_turn_offset(
                target_rid, target.get("skip_turn_count") or 0, next_rid, slot_task_id,
            ) + week_base
            target_week = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
            dates       = get_weekend_dates_from_week(target_week)
            week_word   = "week" if steps == 1 else "weeks"
            lines.append(f"• <b>{slot_desc}</b>: in {steps} {week_word} ({dates})")

        await ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text=f"🕒 <b>Turn Lookup</b>\n\n{target_mention}\n\n" + "\n".join(lines),
            parse_mode="HTML"
        )
    except Exception:
        log_to_db("ERROR", "handle_myturn failed", error_details=traceback.format_exc())
        await ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text="⚠️ Something went wrong. Check CloudWatch logs.",
            parse_mode="HTML"
        )


async def _handle_vacation(ctx: MessageContext) -> None:
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
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
    if not ctx.is_private:
        await ctx.bot.send_message(chat_id=ctx.chat_id, text=GROUP_ONLY_WARN, parse_mode="HTML")
        return
    try:
        roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", ctx.user_id).execute()
        if not roomie_res.data:
            msg = "🌟 <b>Volunteer</b>\n\n❌ ID not found. Send /hi to register first."
        else:
            roomie = roomie_res.data[0]
            cfg_res = (
                supabase.table("rotation_config").select("task_id")
                .eq("roommate_id", roomie["roommate_id"]).limit(1).execute()
            )
            vol_task_id   = cfg_res.data[0]["task_id"] if cfg_res.data else TASK_ENTIRE_HOME
            replacing_rid = peek_next_person_id(vol_task_id)
            if replacing_rid == roomie["roommate_id"]:
                replacing_rid = None

            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie["roommate_id"], "task_id": vol_task_id,
                "week_number": ctx.week_str, "is_volunteer": True,
            }).execute()
            new_skip_count = (roomie.get("skip_turn_count") or 0) + 1
            supabase.table("dim_roommates").update({
                "skip_turn_count":        new_skip_count,
                "skip_next_turn_task_id": vol_task_id,
                "is_priority_next":       True,
                "volunteer_replacing_id": replacing_rid,
            }).eq("roommate_id", roomie["roommate_id"]).execute()
            log_to_db("INFO",
                      f"Volunteer: skip_turn_count → {new_skip_count} (task={vol_task_id}) "
                      f"for {roomie['name']}, replacing_rid={replacing_rid}")
            skip_word  = "skip pass" if new_skip_count == 1 else f"{new_skip_count} skip passes"
            skip_turns = "next turn" if new_skip_count == 1 else f"next {new_skip_count} turns"
            msg = (
                f"🌟 <b>Volunteer Clean Logged</b>\n\n"
                f"✅ Extra clean recorded!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Weekend: {get_weekend_dates_from_week(ctx.week_str)}\n\n"
                f"🎟️ {skip_word} banked — {skip_turns} waived."
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
        user_res = supabase.table("dim_roommates").select("*").eq("telegram_id", target_tg_id).execute()
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
                f"• Week: <code>{week}</code>\n"
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

        existing = (
            supabase.table("fct_cleaning_logs").select("log_id")
            .eq("roommate_id", roomie_id).eq("week_number", ctx.week_str)
            .eq("is_volunteer", False).execute()
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

        task      = config_res.data[0]
        task_id   = task["task_id"]
        task_desc = html.escape(task["dim_tasks"]["task_description"])

        expected_rid = peek_next_person_id(task_id)
        if expected_rid is not None and expected_rid != roomie_id:
            block_msg = "🧹 <b>Clean Log</b>\n\n⚠️ It's not your turn yet!\n"
            try:
                done_chk = (
                    supabase.table("fct_cleaning_logs").select("log_id")
                    .eq("week_number", ctx.week_str).eq("is_volunteer", False)
                    .eq("task_id", task_id).execute()
                )
                week_base   = 1 if done_chk.data else 0
                steps       = compute_turn_offset(
                    roomie_id, roomie.get("skip_turn_count") or 0,
                    expected_rid, task_id,
                ) + week_base
                target_week = (datetime.now() + timedelta(weeks=steps)).strftime("%Y-W%V")
                block_msg  += f"• Your next turn: {get_weekend_dates_from_week(target_week)}"
            except Exception:
                log_to_db("ERROR", "done turn-guard calc failed", error_details=traceback.format_exc())
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=block_msg, parse_mode="HTML")
            return

        is_volunteer_priority = bool(roomie.get("is_priority_next")) and bool(roomie.get("volunteer_replacing_id"))
        replacing_rid         = roomie.get("volunteer_replacing_id")

        supabase.table("fct_cleaning_logs").insert({
            "roommate_id": roomie_id, "task_id": task_id, "week_number": ctx.week_str,
        }).execute()
        log_to_db("INFO", f"Clean logged: {roomie['name']} / {task_desc} / {ctx.week_str}")

        supabase.table("dim_roommates").update({
            "is_priority_next":       False,
            "volunteer_replacing_id": None,
        }).eq("roommate_id", roomie_id).execute()

        if is_volunteer_priority and replacing_rid:
            supabase.table("dim_roommates").update({"is_priority_next": True}).eq(
                "roommate_id", replacing_rid
            ).execute()
            log_to_db("INFO",
                      f"Priority passed to roommate_id={replacing_rid} after volunteer done ({roomie['name']})")

        consume_skips_up_to(task_id, roomie_id)
        next_persons = peek_next_n_persons(task_id, 1)
        next_handle  = mention_user(next_persons[0]) if next_persons else "No one available"
        weekend      = get_weekend_dates_from_week(ctx.week_str)
        next_weekend = get_weekend_dates_from_week(
            (datetime.now() + timedelta(weeks=1)).strftime("%Y-W%V")
        )

        if ctx.is_private:
            vol_line    = "\n🎟️ Volunteer logged — your banked skip still applies." if is_volunteer_priority else ""
            private_msg = (
                f"🧹 <b>Clean Logged</b>\n\n"
                f"✅ Logged!\n"
                f"• Task: {task_desc}\n"
                f"• Weekend: {weekend}{vol_line}\n\n"
                f"🔔 Next ({task_desc}): {next_handle}"
            )
            broadcast_msg = (
                f"📢 <b>Cleaning Update</b>\n\n"
                f"{mention_user(roomie)} has completed <b>{task_desc}</b>!\n"
                f"🔔 Next up: {next_handle} — {next_weekend}"
            )
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=private_msg, parse_mode="HTML")
            await ctx.bot.send_message(chat_id=GROUP_ID, text=broadcast_msg, parse_mode="HTML")
        else:
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

    user       = update.message.from_user
    raw_text   = update.message.text.strip()
    is_private = update.message.chat.type == "private"

    ctx = MessageContext(
        bot        = bot,
        chat_id    = update.message.chat.id,
        user_id    = user.id,
        username   = user.username,
        first_name = user.first_name or user.username or "Unknown",
        is_private = is_private,
        week_str   = get_current_week(),
        raw_text   = raw_text,
        text       = raw_text.lower(),
    )

    if is_private:
        log_interaction(ctx.user_id, ctx.first_name, raw_text.split()[0] if raw_text else "(empty)")

    # /hi works everywhere
    if ctx.text == "/hi":
        await _handle_hi(ctx)
        return {"statusCode": 200}

    # Admin commands work everywhere
    if ctx.text.startswith("/activate"):
        await _handle_activate(ctx)
        return {"statusCode": 200}

    if ctx.text == "/deletelast":
        await _handle_deletelast(ctx)
        return {"statusCode": 200}

    # DM-only commands (group gets redirect warning inside each handler)
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
    for prefix, handler in dm_routes.items():
        if ctx.text == prefix:
            await handler(ctx)
            return {"statusCode": 200}

    # /myturn [optional @username]
    if ctx.text == "/myturn" or ctx.text.startswith("/myturn "):
        await _handle_myturn(ctx)
        return {"statusCode": 200}

    # "done" — accepted from DMs and the group
    if ctx.text == "done":
        await _handle_done(ctx)
        return {"statusCode": 200}

    return {"statusCode": 200}


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
        return {"statusCode": 200, "body": json.dumps("Error handled")}
