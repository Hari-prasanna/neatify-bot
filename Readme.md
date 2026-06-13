# Roommate Cleaning Bot

A Telegram bot that manages a weekly cleaning rotation for a shared house.
Roommates type "done" in a group chat when they finish their task; the bot
logs it, confirms the clean, and announces who is up next.
A GitHub Actions cron job fires a Friday morning reminder automatically.

---

## What It Does

- **Dual-track rotation** — Entire Home (weekly) and Bathroom (mid-week) run as completely independent queues; a Bathroom log never shifts the Home pointer
- **@mention notifications** — replies use `@username` or inline Telegram links so the right person gets pinged
- **Data card UI** — every reply is a structured telemetry-style card (not plain text)
- **Tracks turns** — circular rotation (positions 1–5) so everyone cleans in order
- **Logs completions** — one entry per person per week, stored in Supabase
- **Handles vacations** — `/vacation` / `/back` skip you from both tracks; `/back` grants a priority pass so you don't miss two turns in a row
- **Auto-registers users** — `/hi` captures your Telegram ID and @username automatically
- **Volunteer overtime** — `/volunteer` logs a bonus clean and waives your next scheduled turn as a reward
- **Friday reminder** — GitHub Actions cron sends a weekly card to the group naming who's on deck for the Home clean

---

## Commands

| Command | What it does |
|---|---|
| `/hi` | Register — bot captures your Telegram ID and @username |
| `done` | Log your cleaning task for this week |
| `/next` | Show who is next for both tracks + your personal countdown |
| `/status` | Show everyone's active/vacation status and last cleaned date |
| `/last` | Show the 3 most recent log entries |
| `/vacation` | Mark yourself away (both tracks skip you) |
| `/back` | Return from vacation (priority pass: front of queue next turn) |
| `/volunteer` | Log a bonus clean + earn a skip for your next scheduled turn |

Admin only (set `ADMIN_TELEGRAM_ID` in your `.env`):

| Command | What it does |
|---|---|
| `/activate [tg_id] [order] [task_id]` | Activate a registered user and assign their rotation slot |
| `/deletelast` | Remove the newest cleaning log entry |

---

## Architecture

```
dev/main.py        ← DEV: local polling bot
prod/function.py   ← PROD: AWS Lambda webhook handler
src/utils.py       ← Shared logic (one source of truth for all three)
src/reminder.py    ← Friday cron script (GitHub Actions)
```

**DEV** — run `dev/main.py` locally. Polls Telegram every few seconds.
No webhook needed — just a `.env` file.

**PROD** — `prod/function.py` deployed to AWS Lambda via GitHub Actions.
Telegram pushes each message to an API Gateway URL. The deploy workflow runs
automatically on every push to the `dev` branch.

Both environments share `src/utils.py` — the Supabase client, rotation engine,
@mention helper, and card formatter all live there.

---

## Quick Start (DEV)

```bash
# 1. Copy env template and fill in your credentials
cp .env.example .env

# 2. Run the schema in Supabase SQL editor (first time only)
# → paste database/schema.sql and run it
# → then: INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');

# 3. Install dependencies
pip install -r dev/requirements.txt

# 4. Start the bot
python dev/main.py

# 5. In the Telegram group, send /hi to register yourself
#    Then run /activate in the group to assign your rotation slot
```

---

## Production Deploy

Push to the `dev` branch. GitHub Actions (`deploy.yml`) will:

1. Copy `src/` into `prod/` so the Lambda package includes shared utilities
2. Build and package via AWS SAM (`sam build --use-container`)
3. Deploy to AWS Lambda (`sam deploy`)
4. Register the new API Gateway URL as the Telegram webhook

**Required GitHub Secrets:**

`TELEGRAM_TOKEN`, `TELEGRAM_GROUP_ID`, `TELEGRAM_SECRET_TOKEN`,
`ADMIN_TELEGRAM_ID`, `SUPABASE_URL`, `SUPABASE_KEY`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

---

## Adding a New Roommate

```
# 1. They send /hi in the group → ID and @username auto-captured
# 2. Admin runs (in the group):
/activate 123456789 3 1    ← Entire Home track, slot 3
/activate 123456789 3 2    ← Bathroom track, slot 3
```

Each track needs its own `/activate` call. Slot numbers (1–5) are independent per track.

---

## Tech Stack

- **Python 3.13** with `python-telegram-bot`
- **Supabase** (Postgres) for all data storage
- **AWS Lambda + API Gateway** for production hosting
- **AWS SAM** for infrastructure-as-code deployment
- **GitHub Actions** for CI/CD and Friday reminder cron

See [notes.md](notes.md) for the complete technical guide and troubleshooting steps.
