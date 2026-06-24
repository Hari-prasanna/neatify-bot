# src/utils.py — Shared code for dev/main.py, prod/function.py, and src/reminder.py.

import os
import sys
import html
import logging
import traceback
from datetime import datetime, date, timedelta

from supabase import create_client, Client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.constants import ROTATION_SLOT_COUNT, MAX_WALK_ATTEMPTS

logger = logging.getLogger(__name__)

BOT_ENV: str = os.environ.get("BOT_ENV", "dev")

_SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY", "")
_ADMIN_ID: str = os.environ.get("ADMIN_TELEGRAM_ID", "")

if not _SUPABASE_URL or not _SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set.")

# Created once at import time — reused across Lambda warm starts.
supabase: Client = create_client(_SUPABASE_URL, _SUPABASE_KEY)


# ── Date helpers ─────────────────────────────────────────────────────────────


def get_current_week() -> str:
    return datetime.now().strftime("%Y-W%V")


def get_weekend_dates_from_week(week_str: str) -> str:
    """Convert '2026-W25' to 'Sat 20 Jun – Sun 21 Jun'."""
    try:
        year_s, week_s = week_str.split("-W")
        year = int(year_s)
        week = int(week_s)
        # Jan 4 is always in ISO week 1; walk back to that week's Monday
        jan4 = date(year, 1, 4)
        week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
        target_monday = week1_monday + timedelta(weeks=week - 1)
        saturday = target_monday + timedelta(days=5)
        sunday   = target_monday + timedelta(days=6)
        return (
            f"Sat {saturday.day} {saturday.strftime('%b')}"
            f" – Sun {sunday.day} {sunday.strftime('%b')}"
        )
    except Exception:
        return week_str


# ── Formatting helpers ────────────────────────────────────────────────────────


def mention_user(roommate_data: dict) -> str:
    # @username → inline tg:// link → plain name
    username = roommate_data.get("telegram_username")
    if username:
        return f"@{html.escape(username)}"
    tg_id = roommate_data.get("telegram_id")
    name  = html.escape(roommate_data.get("name", "Unknown"))
    if tg_id:
        return f'<a href="tg://user?id={tg_id}">{name}</a>'
    return name


# ── Persistence helpers ───────────────────────────────────────────────────────


def log_to_db(level: str, message: str, error_details: str = None) -> None:
    try:
        supabase.table("sys_logs").insert({
            "log_level":     level,
            "message":       message,
            "error_details": error_details,
            "environment":   BOT_ENV,
        }).execute()
    except Exception as e:
        print(f"\n[log_to_db FAILED] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        logger.error(f"log_to_db failed: {e}")


def is_admin(user_id: int) -> bool:
    return bool(_ADMIN_ID) and str(user_id) == _ADMIN_ID


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


# ── Rotation private helpers ──────────────────────────────────────────────────


def _find_priority_roommate_id(task_id: int) -> int | None:
    """Return roommate_id of anyone with is_priority_next=True who holds a slot in task_id."""
    try:
        priority_res = (
            supabase.table("dim_roommates")
            .select("roommate_id")
            .eq("is_priority_next", True).eq("is_active", True).eq("is_on_vacation", False)
            .execute()
        )
        if not priority_res.data:
            return None
        candidate_id = priority_res.data[0]["roommate_id"]
        slot_res = (
            supabase.table("rotation_config")
            .select("roommate_id")
            .eq("roommate_id", candidate_id).eq("task_id", task_id)
            .execute()
        )
        return slot_res.data[0]["roommate_id"] if slot_res.data else None
    except Exception as e:
        print(f"\n[_find_priority_roommate_id] task_id={task_id}: {type(e).__name__}: {e}", flush=True)
        return None


def _find_cursor_starting_order(task_id: int) -> int:
    """Return the sequence_order of the last person who completed task_id (non-volunteer)."""
    try:
        last_log = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("task_id", task_id).eq("is_volunteer", False)
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if not last_log.data:
            return 0
        order_res = (
            supabase.table("rotation_config").select("sequence_order")
            .eq("roommate_id", last_log.data[0]["roommate_id"]).eq("task_id", task_id)
            .execute()
        )
        return order_res.data[0]["sequence_order"] if order_res.data else 0
    except Exception as e:
        print(f"\n[_find_cursor_starting_order] task_id={task_id}: {type(e).__name__}: {e}", flush=True)
        log_to_db("ERROR", f"cursor lookup failed (task_id={task_id})", error_details=traceback.format_exc())
        return 0


def _is_candidate_skippable(candidate: dict, task_id: int) -> bool:
    """Return True if this rotation slot should be passed over for task_id."""
    if candidate.get("is_on_vacation"):
        return True
    skip_task = candidate.get("skip_next_turn_task_id")
    return candidate.get("skip_next_turn", False) and (skip_task is None or skip_task == task_id)


def _consume_skip_flag(roommate_id: int, roommate_name: str, task_id: int) -> None:
    """Clear skip_next_turn after the flag has been applied during a walk."""
    try:
        supabase.table("dim_roommates").update({
            "skip_next_turn": False, "skip_next_turn_task_id": None,
        }).eq("roommate_id", roommate_id).execute()
        log_to_db("INFO", f"skip_next_turn consumed for {roommate_name} (task_id={task_id})")
    except Exception as e:
        print(f"\n[_consume_skip_flag] {roommate_name}: {type(e).__name__}: {e}", flush=True)
        log_to_db("ERROR", f"skip_next_turn reset failed for {roommate_name}",
                  error_details=traceback.format_exc())


# ── Public rotation API ───────────────────────────────────────────────────────


def peek_next_person_id(task_id: int) -> int | None:
    """
    Read-only turn check — returns roommate_id of who should clean next for task_id.
    Never consumes skip_next_turn flags, safe to call as a guard before accepting 'done'.
    """
    priority_id = _find_priority_roommate_id(task_id)
    if priority_id is not None:
        return priority_id

    check_order = _find_cursor_starting_order(task_id)
    for _ in range(MAX_WALK_ATTEMPTS):
        check_order = (check_order % ROTATION_SLOT_COUNT) + 1
        try:
            res = (
                supabase.table("rotation_config")
                .select("roommate_id, dim_roommates(is_on_vacation, skip_next_turn, skip_next_turn_task_id)")
                .eq("sequence_order", check_order).eq("task_id", task_id)
                .execute()
            )
        except Exception:
            continue
        if not res.data:
            continue
        if _is_candidate_skippable(res.data[0]["dim_roommates"], task_id):
            continue
        return res.data[0]["roommate_id"]

    return None


def find_next_person(task_id: int) -> dict | None:
    """
    Returns the next eligible rotation_config row (with dim_roommates + dim_tasks joined).

    Layer 1 — priority override: anyone with is_priority_next=True jumps the queue.
    Layer 2 — cursor: last non-volunteer log for this task_id gives the starting position.
    Layer 3 — circular walk: steps forward (order % ROTATION_SLOT_COUNT + 1), skips
               vacationers and skip_next_turn holders (flag consumed immediately).
               Returns None after MAX_WALK_ATTEMPTS steps.
    """
    priority_id = _find_priority_roommate_id(task_id)
    if priority_id is not None:
        cfg = (
            supabase.table("rotation_config")
            .select("*, dim_roommates(*), dim_tasks(*)")
            .eq("roommate_id", priority_id).eq("task_id", task_id)
            .execute()
        )
        if cfg.data:
            return cfg.data[0]
        log_to_db("WARNING", f"Priority person (id={priority_id}) has no slot in task_id={task_id}")

    check_order = _find_cursor_starting_order(task_id)
    for _ in range(MAX_WALK_ATTEMPTS):
        check_order = (check_order % ROTATION_SLOT_COUNT) + 1
        try:
            res = (
                supabase.table("rotation_config")
                .select("*, dim_roommates(*), dim_tasks(*)")
                .eq("sequence_order", check_order).eq("task_id", task_id)
                .execute()
            )
        except Exception as e:
            print(f"\n[find_next_person] walk order={check_order}: {type(e).__name__}: {e}", flush=True)
            log_to_db("ERROR", f"walk failed at order={check_order}, task_id={task_id}",
                      error_details=traceback.format_exc())
            continue

        if not res.data:
            continue

        candidate = res.data[0]["dim_roommates"]
        if candidate.get("is_on_vacation"):
            continue
        skip_task = candidate.get("skip_next_turn_task_id")
        if candidate.get("skip_next_turn", False) and (skip_task is None or skip_task == task_id):
            _consume_skip_flag(candidate["roommate_id"], candidate["name"], task_id)
            continue

        return res.data[0]

    return None
