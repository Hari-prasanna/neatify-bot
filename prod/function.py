# prod/function.py — AWS Lambda webhook handler.
# deploy.yml copies src/ here before sam build so `from src.utils import ...` works on Lambda.

import os
import sys
import json
import html
import logging
import asyncio
import traceback
from datetime import datetime

import telegram
from telegram.request import HTTPXRequest

try:
    from src.utils import supabase, get_current_week, log_to_db, find_next_person, mention_user
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.utils import supabase, get_current_week, log_to_db, find_next_person, mention_user

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ADMIN_ID         = os.environ.get("ADMIN_TELEGRAM_ID", "")
TASK_ENTIRE_HOME = 1
TASK_BATHROOM    = 2


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def get_last_cleaned_date(roommate_id: int) -> str:
    try:
        res = (
            supabase.table("fct_cleaning_logs").select("cleaned_at")
            .eq("roommate_id", roommate_id).order("cleaned_at", desc=True).limit(1).execute()
        )
        if res.data:
            raw = res.data[0]["cleaned_at"].split("T")[0]
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b")
    except Exception:
        pass
    return "Never"


async def process_update(event: dict, bot: telegram.Bot) -> dict:
    # Verify the secret header set during webhook registration
    headers = event.get("headers", {})
    expected = os.environ.get("TELEGRAM_SECRET_TOKEN")
    if expected and headers.get("x-telegram-bot-api-secret-token") != expected:
        logger.warning("Unauthorized — secret token mismatch.")
        return {"statusCode": 403, "body": "Forbidden"}

    body   = json.loads(event.get("body", "{}"))
    update = telegram.Update.de_json(body, bot)

    if not update.message or not update.message.text:
        return {"statusCode": 200}

    user_id   = update.message.from_user.id
    username  = update.message.from_user.username
    user_name = update.message.from_user.first_name or username or "Unknown"
    text      = update.message.text.strip().lower()
    raw_text  = update.message.text.strip()
    chat_id   = update.message.chat.id
    week_str  = get_current_week()

    # /hi
    if text == "/hi":
        name = html.escape(user_name)
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
        except Exception:
            log_to_db("ERROR", f"handle_hi failed for user_id={user_id}",
                      error_details=traceback.format_exc())
            msg = (
                f"👤 <b>Registration</b>\n\n"
                f"⚠️ Auto-save failed — share this ID with the admin.\n"
                f"• Tg ID: <code>{user_id}</code>"
            )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /help
    if text == "/help":
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
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /status
    if text == "/status":
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
                f"Week: <code>{week_str}</code>\n\n"
                + ("\n".join(lines) if lines else "No roommates found.")
            )
        except Exception:
            log_to_db("ERROR", "get_status failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /next
    if text == "/next":
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
            except Exception:
                pass

            msg = (
                f"📅 <b>Upcoming Schedule</b>\n\n"
                f"• Week: <code>{week_str}</code>\n"
                f"• 🏠 Home: {home_handle}\n"
                f"• 🚿 Bathroom: {bath_handle}"
                f"{countdown_line}"
            )
        except Exception:
            log_to_db("ERROR", "handle_next failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /last
    if text == "/last":
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
                    date_str = datetime.strptime(
                        entry["cleaned_at"].split("T")[0], "%Y-%m-%d"
                    ).strftime("%d %b")
                    name = mention_user(entry["dim_roommates"]) if entry.get("dim_roommates") else "Unknown"
                    task = html.escape(entry["dim_tasks"]["task_description"]) if entry.get("dim_tasks") else "Task"
                    lines.append(f"• {date_str}  {name}  ({task})")
                msg = "🕒 <b>Recent Activity</b>\n\n" + "\n".join(lines)
        except Exception:
            log_to_db("ERROR", "handle_last failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /vacation
    if text == "/vacation":
        try:
            res = supabase.table("dim_roommates").update({"is_on_vacation": True}).eq(
                "telegram_id", user_id
            ).execute()
            if res.data:
                msg = (
                    f"🌴 <b>Vacation Mode</b>\n\n"
                    f"✅ Marked as away.\n"
                    f"• User: {mention_user(res.data[0])}\n\n"
                    f"Both rotation tracks will skip you."
                )
            else:
                msg = "🌴 <b>Vacation Mode</b>\n\n❌ ID not found. Send /hi to register first."
        except Exception:
            log_to_db("ERROR", "set_vacation failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /back
    if text == "/back":
        try:
            res = supabase.table("dim_roommates").update({
                "is_on_vacation": False, "is_priority_next": True,
            }).eq("telegram_id", user_id).execute()
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
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /volunteer
    if text == "/volunteer":
        try:
            roomie_res = supabase.table("dim_roommates").select("*").eq(
                "telegram_id", user_id
            ).execute()
            if not roomie_res.data:
                msg = "🌟 <b>Volunteer</b>\n\n❌ ID not found. Send /hi to register first."
            else:
                roomie = roomie_res.data[0]
                supabase.table("fct_cleaning_logs").insert({
                    "roommate_id": roomie["roommate_id"], "task_id": TASK_ENTIRE_HOME,
                    "week_number": week_str, "is_volunteer": True,
                }).execute()
                supabase.table("dim_roommates").update({"skip_next_turn": True}).eq(
                    "roommate_id", roomie["roommate_id"]
                ).execute()
                log_to_db("INFO", f"Volunteer + skip_next_turn set for {roomie['name']}")
                msg = (
                    f"🌟 <b>Volunteer Clean Logged</b>\n\n"
                    f"✅ Extra clean recorded!\n"
                    f"• By: {mention_user(roomie)}\n"
                    f"• Week: <code>{week_str}</code>\n\n"
                    f"🎟️ Skip pass granted — next scheduled turn is waived."
                )
        except Exception:
            log_to_db("ERROR", "handle_volunteer failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /activate
    if text.startswith("/activate"):
        if not is_admin(user_id):
            await bot.send_message(chat_id=chat_id,
                                   text="🔧 <b>Activation</b>\n\n🚫 Admin only.", parse_mode="HTML")
            return {"statusCode": 200}

        args = raw_text.split()[1:]
        if len(args) != 3:
            await bot.send_message(
                chat_id=chat_id,
                text="🔧 <b>Activation</b>\n\nUsage: /activate [tg_id] [order] [task_id]\nExample: /activate 123456789 3 1",
                parse_mode="HTML"
            )
            return {"statusCode": 200}

        try:
            target_tg_id = int(args[0])
            seq_order    = int(args[1])
            task_id      = int(args[2])
        except ValueError:
            await bot.send_message(chat_id=chat_id,
                                   text="🔧 <b>Activation</b>\n\n❌ All three args must be integers.",
                                   parse_mode="HTML")
            return {"statusCode": 200}

        try:
            user_res = supabase.table("dim_roommates").select("*").eq(
                "telegram_id", target_tg_id
            ).execute()
            if not user_res.data:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔧 <b>Activation</b>\n\n❌ No user with Tg ID {target_tg_id}. Have them send /hi first.",
                    parse_mode="HTML"
                )
                return {"statusCode": 200}

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
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"🔧 <b>Activation</b>\n\n⚠️ Slot {seq_order} (task {task_id}) taken by {taken_by}.",
                        parse_mode="HTML"
                    )
                    return {"statusCode": 200}
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
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # /deletelast
    if text == "/deletelast":
        if not is_admin(user_id):
            await bot.send_message(chat_id=chat_id,
                                   text="🗑️ <b>Delete Log</b>\n\n🚫 Admin only.", parse_mode="HTML")
            return {"statusCode": 200}
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
                    f"• Date: {date_fmt}  ({week})"
                )
        except Exception:
            log_to_db("ERROR", "handle_deletelast failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        return {"statusCode": 200}

    # done
    if "done" in text:
        try:
            roomie_res = supabase.table("dim_roommates").select("*").eq("telegram_id", user_id).execute()
            if not roomie_res.data:
                await bot.send_message(
                    chat_id=chat_id,
                    text="🧹 <b>Clean Log</b>\n\n❌ Unrecognized user. Send /hi to register first.",
                    parse_mode="HTML"
                )
                return {"statusCode": 200}

            roomie    = roomie_res.data[0]
            roomie_id = roomie["roommate_id"]

            existing = (
                supabase.table("fct_cleaning_logs").select("log_id")
                .eq("roommate_id", roomie_id).eq("week_number", week_str).execute()
            )
            if existing.data:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🧹 <b>Clean Log</b>\n\n"
                        f"✨ Already logged this week!\n"
                        f"• By: {mention_user(roomie)}\n"
                        f"• Week: <code>{week_str}</code>"
                    ),
                    parse_mode="HTML"
                )
                return {"statusCode": 200}

            config_res = (
                supabase.table("rotation_config").select("*, dim_tasks(task_description)")
                .eq("roommate_id", roomie_id).limit(1).execute()
            )
            if not config_res.data:
                await bot.send_message(
                    chat_id=chat_id,
                    text="🧹 <b>Clean Log</b>\n\n❌ No task assigned. Ask the admin to run /activate.",
                    parse_mode="HTML"
                )
                return {"statusCode": 200}

            task      = config_res.data[0]
            task_id   = task["task_id"]
            task_desc = html.escape(task["dim_tasks"]["task_description"])

            supabase.table("fct_cleaning_logs").insert({
                "roommate_id": roomie_id, "task_id": task_id, "week_number": week_str,
            }).execute()
            log_to_db("INFO", f"Clean logged: {roomie['name']} / {task_desc} / {week_str}")

            # Consume priority pass — one-time use
            supabase.table("dim_roommates").update({"is_priority_next": False}).eq(
                "roommate_id", roomie_id
            ).execute()

            next_config = find_next_person(task_id)
            next_handle = mention_user(next_config["dim_roommates"]) if next_config else "No one available"

            msg = (
                f"🧹 <b>Clean Logged</b>\n\n"
                f"✅ Logged!\n"
                f"• By: {mention_user(roomie)}\n"
                f"• Week: <code>{week_str}</code>\n"
                f"• Task: {task_desc}\n\n"
                f"🔔 Next ({task_desc}): {next_handle}"
            )
        except Exception:
            log_to_db("ERROR", "handle_done failed", error_details=traceback.format_exc())
            msg = "⚠️ Something went wrong. Check CloudWatch logs."
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
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
        # Always return 200 — non-200 makes Telegram retry the same update forever.
        return {"statusCode": 200, "body": json.dumps("Error handled")}
