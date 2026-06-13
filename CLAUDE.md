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
| `rotation_config` | who is in which slot (sequence_order 1–5) for which task_id |
| `fct_cleaning_logs` | every completed clean |
| `sys_logs` | written by log_to_db() — errors and key events |

Key columns in `dim_roommates`:

| Column | What it does |
|---|---|
| `telegram_id` | captured by /hi — used to look up sender |
| `telegram_username` | captured by /hi — used for @mention |
| `is_on_vacation` | both tracks skip this person |
| `skip_next_turn` | set by /volunteer — consumed by find_next_person(), one-time |
| `is_priority_next` | set by /back — person jumps queue, cleared when they say done |

---

## Rotation Logic (`find_next_person(task_id)` in src/utils.py)

Three layers, evaluated in order:

1. **Priority override** — anyone with `is_priority_next=True` gets returned immediately.
2. **Cursor** — read last non-volunteer log for this `task_id` → get their `sequence_order`.
3. **Circular walk** — step forward (`order % 5 + 1`, max 10 steps):
   - Skip if `is_on_vacation`
   - Skip if `skip_next_turn` (clear the flag immediately on skip)
   - Return the first eligible person

Task 1 and Task 2 are **fully independent** — each has its own logs, its own cursor, same slot numbers.

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

**src/utils.py** — inline `print()` before each `log_to_db("ERROR", ...)` in `find_next_person`. Visible in dev terminal and CloudWatch in prod.

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

Slots 1–5 are independent per `task_id`. Same slot number can appear in both tracks.

---

## DB Migrations (run in Supabase SQL editor if schema already exists)

```sql
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS telegram_username TEXT;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn    BOOLEAN DEFAULT FALSE;
ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS is_priority_next  BOOLEAN DEFAULT FALSE;

ALTER TABLE rotation_config DROP CONSTRAINT IF EXISTS rotation_config_sequence_order_key;
ALTER TABLE rotation_config ADD CONSTRAINT rotation_config_sequence_task_unique
    UNIQUE (sequence_order, task_id);
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
| Priority person stuck in queue | Someone ran `/back` but never said done | `UPDATE dim_roommates SET is_priority_next=FALSE WHERE name='X'` |
| Reminder sends wrong person | Bathroom log moved the Entire Home cursor | Shouldn't happen — `find_next_person` filters by `task_id`. Check logs |

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
