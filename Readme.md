# Neatify Cleaning Bot

Telegram bot managing a weekly cleaning rotation for a shared house.

- Local polling backend — [dev/main.py](dev/main.py)
- Serverless AWS Lambda webhook — [prod/function.py](prod/function.py)
- Shared rotation engine — [src/utils.py](src/utils.py)

---

## Local Development (DEV)

### Prerequisites

- Python 3.13+
- Active Supabase project

### 1) Configure environment

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Required values in `.env`:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_GROUP_ID` | Numeric ID of the house group chat |
| `ADMIN_TELEGRAM_ID` | Your personal Telegram numeric ID |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |

### 2) Initialize database

Run [database/schema.sql](database/schema.sql) once in the Supabase SQL Editor, then seed the two task types:

```sql
INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');
```

### 3) Start the bot

```bash
pip install -r dev/requirements.txt
python dev/main.py
```

Stop with `Ctrl + C`.

---

## Production Deploy (AWS)

### 1) Configure secrets

Populate all GitHub Actions Secrets before deploying:

| Secret | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token |
| `TELEGRAM_GROUP_ID` | House group chat ID |
| `TELEGRAM_SECRET_TOKEN` | Random string for webhook header verification |
| `ADMIN_TELEGRAM_ID` | Admin's Telegram ID |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `AWS_ACCESS_KEY_ID` | IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret |

### 2) Push to deploy

All deployments are handled by CI/CD — no manual steps needed:

```bash
git push origin dev
```

This triggers [.github/workflows/deploy.yml](.github/workflows/deploy.yml), which:
1. Copies `src/` into `prod/` so Lambda can import shared utilities
2. Builds the deployment package via AWS SAM (`prod/template.yml`)
3. Deploys to AWS Lambda and registers the new API Gateway URL as the Telegram webhook

---

## Commands and Roles

All commands are handled via **private DM** to keep the group chat clean. The bot broadcasts to the group only when a clean is logged.

Users must register on first use: send `/hi` (captures Telegram ID and @username automatically).

### Roommate commands

| Command | What it does |
|---|---|
| `done` | Log your weekly clean — broadcasts to the group |
| `/next` | View upcoming schedule for both tracks + your personal countdown |
| `/myturn` | Your exact turn date, accounting for banked skips (or `/myturn @name`) |
| `/status` | Everyone's active/vacation state and last cleaned date |
| `/last` | 3 most recent log entries |
| `/volunteer` | Log a bonus clean and bank a skip pass for your next scheduled turn |
| `/vacation` | Pause your rotation slot (both tracks skip you) |
| `/skip` | Alias for /vacation |
| `/back` | Return from vacation — grants a priority pass for your next turn |
| `/help` | Full command reference |
| `/hi` | Register or refresh your Telegram ID and @username |

### Admin commands

| Command | What it does |
|---|---|
| `/activate <tg_id> <order> <task_id>` | Activate a user and assign their rotation slot |
| `/deletelast` | Remove the most recent log entry (undo a mistaken `done`) |

---

## Project Layout

```
roommate-cleaning-bot/
├── dev/
│   ├── main.py             ← Local polling bot — all command handlers
│   └── requirements.txt
│
├── prod/
│   ├── function.py         ← AWS Lambda webhook handler
│   ├── template.yml        ← AWS SAM infrastructure definition
│   └── requirements.txt
│
├── src/
│   ├── utils.py            ← Supabase client, rotation engine, logging helpers
│   ├── reminder.py         ← Friday cron reminder (GitHub Actions)
│   └── requirements.txt    ← Used by the delivery.yml workflow
│
├── database/
│   └── schema.sql          ← Run once in Supabase SQL Editor
│
├── assets/
│   └── data-dict.md        ← Full column reference for all tables
│
├── .github/workflows/
│   ├── deploy.yml          ← Push to dev branch → deploy to Lambda
│   └── delivery.yml        ← Every Friday 07:00 UTC → send reminder
│
└── .env.example            ← Copy to .env and fill in secrets
```

---

## Notes

- **Never commit `.env`** — all secrets must stay local or in GitHub Secrets.
- **Dual-track rotation:** Entire Home (task 1) and Bathroom (task 2) run as fully independent queues. Logging a clean on one track does not move the cursor on the other.
- **Volunteer mechanic:** `/volunteer` logs a bonus clean, banks a skip pass, and gives the volunteer priority so they can immediately say `done`. After their done is logged, the originally scheduled person automatically receives the next priority pass.
- **Turn display:** `/next` and `/myturn` factor in banked skip passes. Each skip shifts the displayed turn date forward by one full cycle (5 weeks).
- **Week format:** Logs use ISO week strings (`YYYY-Www`). The bot converts these to human-readable weekend date ranges (`Sat DD Mon – Sun DD Mon`) in all messages.
- **Lambda cold starts:** `prod/function.py` uses `HTTPXRequest` — required because Lambda has no running event loop at import time.
- **Error logging:** All failures write to the `sys_logs` table in Supabase. In dev, errors are also printed to the terminal with a full traceback. In prod, check CloudWatch.
