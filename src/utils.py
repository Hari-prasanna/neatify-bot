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

logger = logging.getLogger(__name__)

BOT_ENV: str = os.environ.get("BOT_ENV", "dev")

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not _SUPABASE_URL or not _SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set.")

# Created once at import time — reused across Lambda warm starts.
supabase: Client = create_client(_SUPABASE_URL, _SUPABASE_KEY)


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


def log_interaction(telegram_id: int, name: str, command: str) -> None:
    """Record every private-chat message for engagement analytics."""
    try:
        supabase.table("interaction_logs").insert({
            "telegram_id": telegram_id,
            "name":        name,
            "command":     command,
            "environment": BOT_ENV,
        }).execute()
    except Exception as e:
        print(f"\n[log_interaction FAILED] {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def compute_turn_offset(caller_rid: int, caller_skip: int, next_rid: int, task_id: int) -> int:
    """
    Returns how many weeks from now until caller_rid's turn, given next_rid cleans this week.

    Walks the actual rotation order (not a fixed-5 ring) so the count is correct for any
    group size. Intermediate people with banked skips are NOT counted as a week (they'll be
    passed over). The caller's own banked skips each add (n-1) weeks, where n is the
    number of people in the rotation.
    Returns 1 on any error.
    """
    try:
        res = (
            supabase.table("rotation_config")
            .select("roommate_id, dim_roommates(skip_turn_count, is_on_vacation)")
            .eq("task_id", task_id).order("sequence_order")
            .execute()
        )
        slots = res.data
        n = len(slots)
        if n == 0:
            return 1

        next_pos   = next((i for i, s in enumerate(slots) if s["roommate_id"] == next_rid), -1)
        caller_pos = next((i for i, s in enumerate(slots) if s["roommate_id"] == caller_rid), -1)

        if next_pos < 0 or caller_pos < 0 or next_pos == caller_pos:
            return 1

        # Walk forward from next, counting only slots that will actually clean
        base = 0
        idx  = (next_pos + 1) % n
        for _ in range(n):
            p           = slots[idx]["dim_roommates"]
            on_vacation = p.get("is_on_vacation") or False
            slot_skip   = p.get("skip_turn_count") or 0
            is_caller   = (idx == caller_pos)

            if not on_vacation:
                # Count this slot if it's the caller OR it has no banked skip (it WILL clean)
                if is_caller or slot_skip == 0:
                    base += 1

            if is_caller:
                break
            idx = (idx + 1) % n

        # Each of the caller's own banked skips defers them one full cycle (n-1 other cleaners)
        return base + caller_skip * max(1, n - 1)
    except Exception:
        return 1


def peek_next_n_persons(task_id: int, n: int = 3) -> list[dict]:
    """
    Returns the next N upcoming cleaners for task_id in rotation order.
    Fully read-only — skip_turn_count is simulated locally, never written.
    List index = week offset from now (0 = this week, 1 = next, 2 = week after).
    """
    results: list[dict] = []
    try:
        slots_res = (
            supabase.table("rotation_config")
            .select("sequence_order, roommate_id, "
                    "dim_roommates(roommate_id, name, telegram_id, telegram_username, "
                    "is_on_vacation, skip_turn_count)")
            .eq("task_id", task_id).order("sequence_order")
            .execute()
        )
        all_slots = slots_res.data
        if not all_slots:
            return []

        n_slots    = len(all_slots)
        local_skip = {
            s["roommate_id"]: (s["dim_roommates"].get("skip_turn_count") or 0)
            for s in all_slots
        }

        # Skipped done_this_week only for the first slot (current week)
        done_this_week: set = set()
        try:
            done_res = (
                supabase.table("fct_cleaning_logs").select("roommate_id")
                .eq("week_number", get_current_week()).eq("is_volunteer", False)
                .eq("task_id", task_id).execute()
            )
            done_this_week = {r["roommate_id"] for r in done_res.data}
        except Exception:
            pass

        # Priority override — goes first regardless of cursor position
        priority_rid: int | None = None
        start_idx = 0
        try:
            pres = (
                supabase.table("dim_roommates").select("roommate_id")
                .eq("is_priority_next", True).eq("is_active", True).eq("is_on_vacation", False)
                .execute()
            )
            if pres.data:
                cfg = (
                    supabase.table("rotation_config").select("roommate_id")
                    .eq("roommate_id", pres.data[0]["roommate_id"]).eq("task_id", task_id)
                    .execute()
                )
                if cfg.data:
                    priority_rid = cfg.data[0]["roommate_id"]
        except Exception:
            pass

        if priority_rid:
            for i, s in enumerate(all_slots):
                if s["roommate_id"] == priority_rid:
                    results.append(s["dim_roommates"])
                    start_idx     = i
                    done_this_week = set()  # priority turn IS this week — clear filter
                    break

        if len(results) < n:
            # Cursor from last non-volunteer log (only when no priority override)
            if not priority_rid:
                try:
                    last = (
                        supabase.table("fct_cleaning_logs").select("roommate_id")
                        .eq("task_id", task_id).eq("is_volunteer", False)
                        .order("cleaned_at", desc=True).limit(1).execute()
                    )
                    if last.data:
                        oref = (
                            supabase.table("rotation_config").select("sequence_order")
                            .eq("roommate_id", last.data[0]["roommate_id"]).eq("task_id", task_id)
                            .execute()
                        )
                        if oref.data:
                            cursor_order = oref.data[0]["sequence_order"]
                            for i, s in enumerate(all_slots):
                                if s["sequence_order"] == cursor_order:
                                    start_idx = i
                                    break
                except Exception:
                    pass

            first_found = bool(results)
            cur_idx     = start_idx
            max_steps   = n_slots * (n + max(local_skip.values(), default=0) + 2)

            for _ in range(max_steps):
                cur_idx = (cur_idx + 1) % n_slots
                slot    = all_slots[cur_idx]
                rid     = slot["roommate_id"]
                person  = slot["dim_roommates"]

                if person.get("is_on_vacation"):
                    continue
                if not first_found and rid in done_this_week:
                    continue

                skip = local_skip.get(rid, 0)
                if skip > 0:
                    local_skip[rid] = skip - 1
                    continue
                if priority_rid and rid == priority_rid:
                    continue  # already in results[0]

                results.append(person)
                first_found    = True
                done_this_week = set()

                if len(results) >= n:
                    break

    except Exception as e:
        log_to_db("ERROR", f"peek_next_n_persons failed (task_id={task_id})",
                  error_details=str(e))

    return results


def consume_skips_up_to(task_id: int, cleaner_rid: int) -> None:
    """
    Call this immediately after writing a done log for cleaner_rid.
    Walks forward from the PREVIOUS cleaner's position to cleaner_rid and decrements
    skip_turn_count for anyone stepped over. This is the only place skips are consumed,
    ensuring each skip is spent exactly once — at the moment the rotation passes that slot.
    """
    try:
        slots_res = (
            supabase.table("rotation_config")
            .select("roommate_id, dim_roommates(name, is_on_vacation, skip_turn_count, skip_next_turn_task_id)")
            .eq("task_id", task_id).order("sequence_order").execute()
        )
        slots = slots_res.data
        if not slots:
            return
        n = len(slots)

        # The two most recent non-volunteer logs: [0]=just written, [1]=previous
        recent = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("task_id", task_id).eq("is_volunteer", False)
            .order("cleaned_at", desc=True).limit(2).execute()
        )
        if len(recent.data) < 2:
            return  # First ever clean — nothing to consume

        prev_rid  = recent.data[1]["roommate_id"]
        start_idx = next((i for i, s in enumerate(slots) if s["roommate_id"] == prev_rid), 0)

        for step in range(n):
            idx  = (start_idx + step + 1) % n
            slot = slots[idx]
            rid  = slot["roommate_id"]
            if rid == cleaner_rid:
                break
            person     = slot["dim_roommates"]
            # Never consume a vacation person's skip — they may have banked it before leaving
            if person.get("is_on_vacation"):
                continue
            skip_task  = person.get("skip_next_turn_task_id")
            skip_count = person.get("skip_turn_count") or 0
            if skip_count > 0 and (skip_task is None or skip_task == task_id):
                new_count = skip_count - 1
                supabase.table("dim_roommates").update(
                    {"skip_turn_count": new_count}
                ).eq("roommate_id", rid).execute()
                log_to_db("INFO",
                          f"skip consumed for {person.get('name', '?')}: "
                          f"{skip_count} → {new_count} (task_id={task_id})")
    except Exception as e:
        print(f"\n[consume_skips_up_to]: {type(e).__name__}: {e}", flush=True)
        log_to_db("WARNING", f"consume_skips_up_to failed (task_id={task_id}): {e}")


def peek_next_person_id(task_id: int) -> int | None:
    """
    Read-only turn check — returns roommate_id of who should clean next for task_id.
    Unlike find_next_person, this never decrements skip_turn_count, so it is
    safe to call as a guard before deciding whether to accept a 'done' log.
    """
    # Priority override
    try:
        res = (
            supabase.table("dim_roommates")
            .select("roommate_id")
            .eq("is_priority_next", True).eq("is_active", True).eq("is_on_vacation", False)
            .execute()
        )
        if res.data:
            cfg = (
                supabase.table("rotation_config")
                .select("roommate_id")
                .eq("roommate_id", res.data[0]["roommate_id"]).eq("task_id", task_id)
                .execute()
            )
            if cfg.data:
                return cfg.data[0]["roommate_id"]
    except Exception as e:
        print(f"\n[peek_next_person_id] priority check: {type(e).__name__}: {e}", flush=True)

    # Load all slots ordered — walk by index, not slot number, so N-person rotations work correctly.
    try:
        slots_res = (
            supabase.table("rotation_config")
            .select("sequence_order, roommate_id, "
                    "dim_roommates(is_on_vacation, skip_turn_count, skip_next_turn_task_id)")
            .eq("task_id", task_id).order("sequence_order").execute()
        )
        slots = slots_res.data
    except Exception as e:
        print(f"\n[peek_next_person_id] slots load: {type(e).__name__}: {e}", flush=True)
        return None

    if not slots:
        return None

    n = len(slots)

    # Cursor: find the index of the last person who cleaned
    start_idx = 0
    try:
        last = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("task_id", task_id).eq("is_volunteer", False)
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if last.data:
            last_rid = last.data[0]["roommate_id"]
            for i, s in enumerate(slots):
                if s["roommate_id"] == last_rid:
                    start_idx = i
                    break
    except Exception as e:
        print(f"\n[peek_next_person_id] cursor: {type(e).__name__}: {e}", flush=True)

    # Pre-fetch who already logged a scheduled clean this week for this task.
    done_this_week: set = set()
    try:
        done_res = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("week_number", get_current_week()).eq("is_volunteer", False).eq("task_id", task_id)
            .execute()
        )
        done_this_week = {r["roommate_id"] for r in done_res.data}
    except Exception:
        pass

    # Circular walk — read-only, skip_turn_count is inspected but NOT decremented
    for step in range(n * 2):
        idx       = (start_idx + step + 1) % n
        slot      = slots[idx]
        rid       = slot["roommate_id"]
        candidate = slot["dim_roommates"]
        skip_task   = candidate.get("skip_next_turn_task_id")
        should_skip = (
            (candidate.get("skip_turn_count") or 0) > 0
            and (skip_task is None or skip_task == task_id)
        )
        if (candidate.get("is_on_vacation") or should_skip or rid in done_this_week):
            continue
        return rid

    return None


def find_next_person(task_id: int) -> dict | None:
    """
    Returns the next eligible rotation_config row (with dim_roommates + dim_tasks joined).

    Layer 1 — priority override: anyone with is_priority_next=True jumps the queue.
    Layer 2 — cursor: last non-volunteer log for this task_id gives the starting position.
    Layer 3 — circular walk: steps forward through all slots in sequence_order, skips
               vacationers, done_this_week, and skip_turn_count holders (count decremented).
    """
    # Layer 1: priority override
    try:
        res = (
            supabase.table("dim_roommates")
            .select("roommate_id, name, telegram_id, telegram_username")
            .eq("is_priority_next", True).eq("is_active", True).eq("is_on_vacation", False)
            .execute()
        )
        if res.data:
            p_id   = res.data[0]["roommate_id"]
            p_name = res.data[0].get("name", "?")
            cfg = (
                supabase.table("rotation_config")
                .select("*, dim_roommates(*), dim_tasks(*)")
                .eq("roommate_id", p_id).eq("task_id", task_id)
                .execute()
            )
            if cfg.data:
                return cfg.data[0]
            log_to_db("WARNING",
                      f"Priority person {p_name} (id={p_id}) has no slot in task_id={task_id}")
    except Exception as e:
        print(f"\n[find_next_person] priority check: {type(e).__name__}: {e}", flush=True)
        log_to_db("ERROR", f"priority check failed (task_id={task_id})",
                  error_details=traceback.format_exc())

    # Load all slots ordered — walk by index so N-person rotations work correctly.
    try:
        slots_res = (
            supabase.table("rotation_config")
            .select("sequence_order, roommate_id, dim_roommates(*), dim_tasks(*)")
            .eq("task_id", task_id).order("sequence_order").execute()
        )
        slots = slots_res.data
    except Exception as e:
        print(f"\n[find_next_person] slots load: {type(e).__name__}: {e}", flush=True)
        log_to_db("ERROR", f"slots load failed (task_id={task_id})",
                  error_details=traceback.format_exc())
        return None

    if not slots:
        return None

    n = len(slots)

    # Layer 2: cursor — find the index of whoever cleaned last
    start_idx = 0
    try:
        last = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("task_id", task_id).eq("is_volunteer", False)
            .order("cleaned_at", desc=True).limit(1).execute()
        )
        if last.data:
            last_rid = last.data[0]["roommate_id"]
            for i, s in enumerate(slots):
                if s["roommate_id"] == last_rid:
                    start_idx = i
                    break
    except Exception as e:
        print(f"\n[find_next_person] cursor lookup: {type(e).__name__}: {e}", flush=True)
        log_to_db("ERROR", f"cursor lookup failed (task_id={task_id})",
                  error_details=traceback.format_exc())

    # Pre-fetch who already logged a scheduled clean this week for this task.
    done_this_week: set = set()
    try:
        done_res = (
            supabase.table("fct_cleaning_logs").select("roommate_id")
            .eq("week_number", get_current_week()).eq("is_volunteer", False).eq("task_id", task_id)
            .execute()
        )
        done_this_week = {r["roommate_id"] for r in done_res.data}
    except Exception:
        pass

    # Layer 3: circular walk
    for step in range(n * 2):
        idx       = (start_idx + step + 1) % n
        slot      = slots[idx]
        rid       = slot["roommate_id"]
        candidate = slot["dim_roommates"]

        if candidate["is_on_vacation"] or rid in done_this_week:
            continue

        skip_task  = candidate.get("skip_next_turn_task_id")
        skip_count = candidate.get("skip_turn_count") or 0
        if skip_count > 0 and (skip_task is None or skip_task == task_id):
            new_count = skip_count - 1
            try:
                supabase.table("dim_roommates").update({
                    "skip_turn_count": new_count,
                }).eq("roommate_id", rid).execute()
                log_to_db("INFO",
                          f"skip_turn_count decremented for {candidate['name']}: "
                          f"{skip_count} → {new_count} remaining (task_id={task_id})")
            except Exception as e:
                print(f"\n[find_next_person] skip_turn_count update: {type(e).__name__}: {e}", flush=True)
                log_to_db("ERROR", f"skip_turn_count update failed for {candidate['name']}",
                          error_details=traceback.format_exc())
            continue

        return slot

    return None
