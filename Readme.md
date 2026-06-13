# Roommate Cleaning Bot

A Telegram bot that manages a weekly cleaning rotation for a shared house.
Roommates type "done" in a group chat when they finish their task; the bot
logs it, confirms the completed duty, and announces who is up next.
A GitHub Actions cron job sends a Friday morning reminder automatically.

---

## What It Does

- **Tracks turns** — maintains a circular rotation so every roommate cleans in order
- **Logs completions** — one entry per person per week, stored in Supabase
- **Handles vacations** — `/vacation` and `/back` skip you from the rotation while you're away
- **Auto-registers users** — `/hi` captures your Telegram ID and adds you to the database automatically
- **Sends Friday reminders** — GitHub Actions triggers a weekly Telegram message naming who's on deck
- **Supports volunteers** — `/volunteer` lets you log a bonus clean without affecting the rotation order

---

## Commands

| Command | What it does |
|---|---|
| `/hi` | Register yourself — the bot captures your Telegram ID automatically |
| `done` | Log that you completed your cleaning task this week |
| `/status` | Show everyone's active/vacation status and last cleaned date |
| `/next` | Show who is scheduled to clean next |
| `/last` | Show the 3 most recent cleaning log entries |
| `/vacation` | Mark yourself as away (skipped from rotation) |
| `/back` | Return from vacation (re-enter rotation) |
| `/volunteer` | Log a bonus clean outside your normal turn |

---

## Architecture

**DEV** — run `bot/main.py` locally. It polls Telegram for messages.
No webhook setup needed — just a `.env` file with your credentials.

**PROD** — `lambda/src/function.py` deployed to AWS Lambda via GitHub Actions.
Telegram pushes messages to an API Gateway webhook URL.
The deploy workflow runs automatically on every push to the `dev` branch.

```
bot/            ← DEV: local polling bot + Friday reminder script
lambda/         ← PROD: AWS Lambda webhook handler + SAM template
database/       ← schema.sql: run once in Supabase to create tables
.env.example    ← copy to .env and fill in your credentials
notes.md        ← full technical guide with troubleshooting steps
```

---

## Quick Start (DEV)

```bash
# 1. Copy the env template
cp .env.example .env
# Edit .env with your Telegram token, group ID, and Supabase credentials

# 2. Create tables in Supabase
# Paste database/schema.sql into the Supabase SQL editor and run it

# 3. Install dependencies
pip install -r bot/requirements.txt

# 4. Start the bot
python bot/main.py

# 5. In your Telegram group, send /hi to register yourself
#    Then ask the admin to activate your account in Supabase
```

---

## Production Deploy

Push to the `dev` branch. GitHub Actions will:

1. Build and deploy the Lambda function via AWS SAM
2. Register the new webhook URL with Telegram automatically

Required GitHub Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_GROUP_ID`,
`TELEGRAM_SECRET_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.

See [notes.md](notes.md) for the full setup guide and troubleshooting steps.

---

## Adding a New Roommate

1. They send `/hi` in the Telegram group → their ID is auto-captured
2. Admin goes to Supabase → `dim_roommates` → sets `is_active=TRUE`
3. Admin adds a row to `rotation_config` with their `roommate_id`, `task_id`, and `sequence_order`

---

## Tech Stack

- **Python 3.13** with `python-telegram-bot`
- **Supabase** (Postgres) for all data storage
- **AWS Lambda + API Gateway** for production hosting
- **AWS SAM** for infrastructure-as-code deployment
- **GitHub Actions** for CI/CD and the Friday reminder cron job
