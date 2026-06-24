# Neatify Cleaning Bot

Telegram bot managing a weekly cleaning rotation for a shared house with:
- Local polling backend ([dev/main.py](dev/main.py))
- Serverless AWS Lambda webhook ([prod/function.py](prod/function.py))
- Shared rotation engine in [src/utils.py](src/utils.py)
- Shared constants in [src/constants.py](src/constants.py)

## Local development

### Prerequisites

- Python 3.13 installed
- Active Supabase project

### 1) Configure environment

Copy [.env.example](.env.example) and fill in credentials:

```bash
cp .env.example .env
```

Required variables:

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_GROUP_ID` | Target group chat ID |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `ADMIN_TELEGRAM_ID` | Your Telegram user ID |

### 2) Initialize database

Execute [database/schema.sql](database/schema.sql) in your Supabase SQL Editor, then bootstrap tasks:

```sql
INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');
```

### 3) Run the bot

```bash
pip install -r dev/requirements.txt
python dev/main.py
```

Stop with `Ctrl + C`.

---

## Production deploy (AWS Lambda)

### 1) Configure GitHub Actions Secrets

| Secret | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token |
| `TELEGRAM_GROUP_ID` | Target group chat ID |
| `TELEGRAM_SECRET_TOKEN` | Webhook signature verification |
| `ADMIN_TELEGRAM_ID` | Admin's Telegram user ID |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `AWS_ACCESS_KEY_ID` | AWS deploy credentials |
| `AWS_SECRET_ACCESS_KEY` | AWS deploy credentials |

### 2) Push to deploy

```bash
git push origin dev
```

This triggers [.github/workflows/deploy.yml](.github/workflows/deploy.yml) to build and deploy via AWS SAM, then wire the new API Gateway URL to the Telegram webhook automatically.

---

## Commands

Commands are sent via **private DM** to reduce group noise. The bot broadcasts to the group only on task completion.

Register first by sending `/hi` in the group chat (captures Telegram ID and @username automatically).

**Roommate commands:**

| Command | Action |
|---|---|
| `done` | Log your clean for this week |
| `/next` | View upcoming schedule and personal countdown |
| `/status` | Everyone's state and last-cleaned dates |
| `/last` | 3 most recent log entries |
| `/volunteer` | Log a bonus clean and earn a skip pass |
| `/vacation` | Freeze your rotation slot |
| `/skip` | Skip your next scheduled turn |
| `/back` | Return from vacation (grants priority pass) |

**Admin commands (anywhere):**

| Command | Action |
|---|---|
| `/activate <tg_id> <slot> <task_id>` | Activate user and assign rotation slot |
| `/deletelast` | Remove the newest cleaning log entry |

---

## Project layout

| Path | Purpose |
|---|---|
| [dev/main.py](dev/main.py) | Local polling bot |
| [prod/function.py](prod/function.py) | AWS Lambda webhook handler |
| [prod/template.yml](prod/template.yml) | AWS SAM infrastructure definition |
| [src/constants.py](src/constants.py) | Task IDs, rotation limits, shared message strings |
| [src/utils.py](src/utils.py) | Supabase client, rotation engine, shared helpers |
| [src/reminder.py](src/reminder.py) | Friday morning reminder cron |
| [database/schema.sql](database/schema.sql) | Supabase DDL |
| [.github/workflows/](.github/workflows/) | CI/CD: deploy on push, reminder every Friday |

---

## Notes

- Secrets live in `.env` locally — never commit this file.
- The two cleaning tracks (Entire Home, Bathroom) run as fully independent queues. Completing one task does not advance the other track's cursor.
- Weeks are stored as ISO format (`YYYY-Www`); the display layer converts to calendar weekend dates at read time.
- `prod/function.py` requires `HTTPXRequest` for Lambda cold-start compatibility.
- All application errors are logged to the `sys_logs` table in Supabase via `log_to_db()` in `src/utils.py`.
