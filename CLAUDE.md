# CLAUDE.md — Roommate Cleaning Bot

Quick reference for debugging, extending, or resuming work on this project.

---

## Project Layout

```
dev/main.py           local polling bot (python dev/main.py)
prod/function.py      AWS Lambda webhook handler
src/utils.py          shared code — supabase client, rotation engine, mention_user, log_to_db
src/reminder.py       Friday cron (GitHub Actions delivery.yml)
database/schema.sql   run once in Supabase SQL editor to create tables
.github/workflows/
  deploy.yml          push to dev branch → auto-deploy to AWS Lambda
  delivery.yml        every Friday 07:00 UTC → python src/reminder.py
```

All three entry points import from `src/utils.py`. Change something there and it applies everywhere.

---

## Environment Variables

Set in `.env` locally and in GitHub Actions Secrets for prod.

| Variable | Used by |
|---|---|
| `TELEGRAM_TOKEN` | all |
| `TELEGRAM_GROUP_ID` | dev/main.py, src/reminder.py |
| `ADMIN_TELEGRAM_ID` | dev/main.py, prod/function.py |
| `SUPABASE_URL` | all |
| `SUPABASE_KEY` | all |
| `BOT_ENV` | all (dev=local, prod=Lambda via template.yml) |
| `TELEGRAM_SECRET_TOKEN` | prod only — webhook header verification |

---

## Database Tables

| Table | Purpose |
|---|---|
| `dim_tasks` | task catalog — task_id=1 Entire Home, task_id=2 Bathroom |
| `dim_roommates` | people + state flags |
| `rotation_config` | who is in which slot (sequence_order 1–N) for which task_id |
| `fct_cleaning_logs` | every completed clean |
| `sys_logs` | written by log_to_db() — errors and key events |
| `interaction_logs` | every private DM received — usage analytics |

Key columns in `dim_roommates`:

| Column | What it does |
|---|---|
| `telegram_id` | captured by /hi — used to look up sender |
| `telegram_username` | captured by /hi — used for @mention |
| `is_on_vacation` | set by /vacation or /skip; both tracks skip this person |
| `skip_turn_count` | integer banked skips earned by /volunteer; decremented by the rotation walk when stepping over this person |
| `skip_next_turn_task_id` | which task track the skip applies to (NULL = both); set alongside skip_turn_count |
| `is_priority_next` | set by /back or /volunteer; person jumps queue; cleared when they say done |
| `volunteer_replacing_id` | FK to dim_roommates; set by /volunteer to remember who was displaced; after volunteer says done, that person gets is_priority_next=TRUE; then cleared to NULL |

Note: `skip_next_turn` (old boolean) is deprecated — ignore it. The live field is `skip_turn_count`.

---

## Rotation Logic

### `find_next_person(task_id)` — stateful (call only when about to log a clean)

Three layers, evaluated in order:

1. **Priority override** — anyone with `is_priority_next=True` is returned immediately.
2. **Cursor** — read last non-volunteer log for this `task_id` → get their `sequence_order`.
3. **Circular walk** — step forward through `rotation_config` (ordered by `sequence_order`):
   - Skip if `is_on_vacation`
   - Skip if `roommate_id` is in `done_this_week` (non-vol log already exists this week)
   - Skip if `skip_turn_count > 0` — decrement the count and move on (stateful)
   - Return the first eligible person

Task 1 and Task 2 are **fully independent** — each has its own logs, its own cursor.

### `peek_next_person_id(task_id)` — read-only, same walk without decrementing

Used as the **turn guard** before accepting "done". Same three-layer logic but never writes to DB.

### `peek_next_n_persons(task_id, n=3)` — read-only, returns list of N upcoming cleaners

Used by `/next` to show the next 3 upcoming turns. Simulates skip_turn_count locally without writing. Returns a list of `dim_roommates` dicts; list index = week offset from now.

### `compute_turn_offset(caller_rid, caller_skip, next_rid, task_id)` — turn countdown

Walks the actual rotation list (not a fixed-5 ring) to count how many weeks until the caller's turn. Accounts for intermediate skips (they don't consume a week), vacation skips, and the caller's own banked skips. Called by `/next` and `/myturn`.

---

## Key Behavioural Rules

- **`/next` is fully read-only** — calls `peek_next_n_persons`, never `find_next_person`. Checking the schedule cannot consume skip counts.
- **`/volunteer`** logs an `is_volunteer=TRUE` entry, increments `skip_turn_count`, sets `is_priority_next=TRUE` on the volunteer, and records `volunteer_replacing_id` (who they displaced).
- **After volunteer says done** — volunteer's flags are cleared, then `is_priority_next=TRUE` is set on the person stored in `volunteer_replacing_id`. This ensures the original scheduled person is never permanently skipped.
- **`done_this_week` guard** — both rotation functions pre-fetch non-volunteer log roommate_ids for current week + task before the walk. Prevents a cursor regression after `/deletelast` from re-selecting someone who already cleaned.
- **Friday reminder** sends two messages: (1) group broadcast (fatal if fails), (2) private DM to the scheduled person's `telegram_id` (non-fatal if fails or NULL). Skips entirely if the person already has a done log this week.

---

## Message Format

All responses are plain HTML strings with emoji bullet points. No box-drawing characters.
Always send with `parse_mode="HTML"`.

`mention_user(roommate_data)` picks the best mention:
1. `@username` if `telegram_username` is set
2. `<a href="tg://user?id=...">Name</a>` if `telegram_id` is set
3. Plain name as fallback

---

## Error Handling

**dev/main.py** — `_err(label, exc)` prints the raw exception type, message, and full traceback to stdout before calling `log_to_db`. Terminal shows the exact DB error.

**prod/function.py** — no stdout prints (Lambda stdout goes to CloudWatch). Uses `log_to_db` only.

**src/utils.py** — inline `print()` before each `log_to_db("ERROR", ...)` in rotation functions. Visible in dev terminal and CloudWatch in prod.

---

## Lambda Packaging

`prod/function.py` imports `from src.utils import ...`. This works because `deploy.yml` runs `cp -r src prod/` before `sam build`, so the Lambda zip contains `src/` as a subfolder of `prod/`.

The import in `prod/function.py` uses try/except to handle both Lambda runtime and local testing:
```python
try:
    from src.utils import ...
except ImportError:
    sys.path.insert(0, repo_root)
    from src.utils import ...
```

---

## Adding a New Roommate

```
1. They send /hi in the group → telegram_id and telegram_username auto-captured
2. Admin runs (in the group):
   /activate <tg_id> <slot> 1    ← Entire Home track
   /activate <tg_id> <slot> 2    ← Bathroom track
```

Slots are independent per `task_id`. Same slot number can appear in both tracks.

---

## DB Migrations (run in Supabase SQL editor if schema already exists)

```sql
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS telegram_username       TEXT;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn          BOOLEAN DEFAULT FALSE;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS is_priority_next        BOOLEAN DEFAULT FALSE;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_turn_count         INTEGER DEFAULT 0;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn_task_id  INTEGER REFERENCES dim_tasks(task_id);
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS volunteer_replacing_id  INTEGER REFERENCES dim_roommates(roommate_id);

ALTER TABLE rotation_config DROP CONSTRAINT IF EXISTS rotation_config_sequence_order_key;
ALTER TABLE rotation_config ADD CONSTRAINT rotation_config_sequence_task_unique
    UNIQUE (sequence_order, task_id);

CREATE TABLE IF NOT EXISTS interaction_logs (
    interaction_id SERIAL PRIMARY KEY,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    telegram_id    BIGINT NOT NULL,
    name           TEXT   NOT NULL,
    command        TEXT   NOT NULL,
    environment    TEXT   NOT NULL
);
```

---

## Common Errors and Fixes

| Symptom | Cause | Fix |
|---|---|---|
| `/hi` says "auto-save failed" | `telegram_username` column missing | Run migration above |
| `/status` crashes | Any DB error — check terminal `_err` output | Read the printed traceback |
| `/next` shows "No one scheduled" | `rotation_config` empty for that task_id, or all on vacation | Check `rotation_config` rows; check vacation flags |
| Lambda returns 403 | `TELEGRAM_SECRET_TOKEN` mismatch | Re-deploy to re-register webhook |
| "No task assigned" on done | Person has no `rotation_config` row | Run `/activate` for them |
| Priority person stuck in queue | Someone ran `/back` or `/volunteer` but never said done | `UPDATE dim_roommates SET is_priority_next=FALSE, volunteer_replacing_id=NULL WHERE name='X'` |
| Reminder sends wrong person | Bug in rotation walk | Check `sys_logs` for errors; `find_next_person` filters by `task_id` |
| Volunteer's skip not clearing | `skip_turn_count` not decremented | Check walk logic in `find_next_person`; confirm `rotation_config` row exists |
| Turn count shows wrong weeks | Old `% 5` formula used instead of `compute_turn_offset` | Ensure both `/next` and `/myturn` use `compute_turn_offset()` |

---

## Local Dev Quick Start

```bash
cp .env.example .env          # fill in credentials
pip install -r dev/requirements.txt
python dev/main.py            # start the bot
```

First time only — run `database/schema.sql` in Supabase SQL editor, then:
```sql
INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');
```
