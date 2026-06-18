# Data Dictionary — Roommate Cleaning Bot

All tables live in Supabase (PostgreSQL). See `database/schema.sql` to create them.

---

## `dim_tasks`

Stores the two cleaning task types. Seeded once — never changes at runtime.

| Column | Type | Description |
|---|---|---|
| `task_id` | PK serial | Auto-assigned. 1 = Entire Home, 2 = Bathroom. |
| `task_description` | text | Human-readable label (e.g. "Entire Home"). |

---

## `dim_roommates`

One row per person. State flags are updated by bot commands.

| Column | Type | Description |
|---|---|---|
| `roommate_id` | PK serial | Auto-assigned. |
| `name` | text | Display name, captured from Telegram on /hi. |
| `telegram_id` | bigint | Numeric Telegram user ID. Used to identify message senders. |
| `telegram_username` | text | Optional @handle. Refreshed on every /hi. Used for @mentions. |
| `is_active` | boolean | False until admin runs /activate. Inactive users are ignored. |
| `is_on_vacation` | boolean | Set by /vacation or /skip, cleared by /back. Both rotation tracks skip this person. |
| `skip_turn_count` | integer | Banked skips earned by /volunteer. Decremented by `find_next_person()` when the walk steps over this person. Zero = no skip. |
| `skip_next_turn_task_id` | FK → dim_tasks | Which task track the skip applies to. NULL = both tracks. |
| `is_priority_next` | boolean | Set by /back (vacation return) or /volunteer (so the volunteer can say "done"). This person is returned first by `find_next_person()`, bypassing rotation order. Cleared after they say done. |
| `volunteer_replacing_id` | FK → dim_roommates | Set by /volunteer to record who the volunteer displaced. After the volunteer says done, that person gets `is_priority_next=TRUE`. Cleared to NULL after done. NULL if the volunteer was the scheduled person. |
| `effective_start_date` | timestamptz | When this row was created. |
| `effective_end_date` | timestamptz | Null while active. Set if the person moves out. |

---

## `rotation_config`

Maps each person to a slot (1–5) in a specific task track. Each track has its own independent slots.

| Column | Type | Description |
|---|---|---|
| `rotation_id` | PK serial | Auto-assigned. |
| `roommate_id` | FK → dim_roommates | Who. |
| `task_id` | FK → dim_tasks | Which track (1 = Entire Home, 2 = Bathroom). |
| `sequence_order` | int | Position in the rotation ring (1–5). UNIQUE per (sequence_order, task_id). |

A person needs one row per task track. `/activate 123456789 3 1` creates a row for task 1 at slot 3.

---

## `fct_cleaning_logs`

One row per cleaning event. This is the source of truth for the rotation cursor.

| Column | Type | Description |
|---|---|---|
| `log_id` | PK serial | Auto-assigned. |
| `roommate_id` | FK → dim_roommates | Who cleaned. |
| `task_id` | FK → dim_tasks | Which task was done. |
| `cleaned_at` | timestamptz | When the log was created (auto, server time). |
| `week_number` | text | ISO week string (e.g. `2026-W24`). Prevents duplicate logs per person per week. |
| `is_volunteer` | boolean | TRUE if logged via /volunteer. Volunteer cleans do NOT advance the rotation cursor — `find_next_person()` ignores them when computing the current position. |

---

## `sys_logs`

Audit trail written by `log_to_db()` in src/utils.py. Check here when something goes wrong.

| Column | Type | Description |
|---|---|---|
| `log_id` | PK serial | Auto-assigned. |
| `log_level` | text | INFO, WARNING, or ERROR. Enforced by CHECK constraint. |
| `message` | text | Short description of what happened. |
| `error_details` | text | Full Python traceback (populated on errors only). |
| `environment` | text | "dev" or "prod" — which instance wrote this row. |
| `created_at` | timestamptz | Auto-set by Supabase. |

---

## `interaction_logs`

Every private DM the bot receives is recorded here for engagement analytics.
Kept separate from `sys_logs` so operational errors and usage data don't mix.

| Column | Type | Description |
|---|---|---|
| `interaction_id` | PK serial | Auto-assigned. |
| `telegram_id` | bigint | Who sent the message. |
| `name` | text | First name at the time of the message. |
| `command` | text | First word of the message (e.g. `/next`, `done`). |
| `environment` | text | "dev" or "prod". |
| `created_at` | timestamptz | Auto-set by Supabase. |

Useful queries:

```sql
-- Most active users
SELECT telegram_id, name, COUNT(*) AS messages
FROM interaction_logs GROUP BY 1, 2 ORDER BY 3 DESC;

-- Command popularity
SELECT command, COUNT(*) AS uses
FROM interaction_logs GROUP BY 1 ORDER BY 2 DESC;

-- Daily activity
SELECT DATE(created_at), COUNT(*) AS messages
FROM interaction_logs GROUP BY 1 ORDER BY 1;
```
