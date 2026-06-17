# Neatify Cleaning Bot

Telegram bot managing a weekly cleaning rotation for a shared house with:
- Local polling backend (`[dev/main.py](dev/main.py)`)
- Serverless AWS Lambda webhook (`[prod/function.py](prod/function.py)`)
- Shared rotation engine in `[src/utils.py](src/utils.py)`

## Local development (DEV)

### Prerequisites

- Python 3.13 installed.
- Active Supabase project.

### 1) Configure environment

Update `[.env](.env)` with valid values (copy from `[.env.example](.env.example)`):

- `TELEGRAM_TOKEN`
- `TELEGRAM_GROUP_ID`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `ADMIN_TELEGRAM_ID`

### 2) Initialize database

Execute `[database/schema.sql](database/schema.sql)` in your Supabase SQL Editor.
Bootstrap the core tasks:

```sql
INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');
```

### 3) Build and start services

Install dependencies and run the local polling instance:

```bash
pip install -r dev/requirements.txt
python dev/main.py
```

### 4) Useful operations

Stop local polling (clean exit):
```bash
Ctrl + C
```

## Production Deploy (AWS)

### 1) Configure secrets
Ensure GitHub Actions Secrets are populated:
- `TELEGRAM_TOKEN`, `TELEGRAM_GROUP_ID`, `TELEGRAM_SECRET_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `SUPABASE_URL`, `SUPABASE_KEY`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### 2) Push to deploy
Deployments are handled completely via CI/CD. Push to the target branch:

```bash
git push origin dev
```

This triggers `[.github/workflows/deploy.yml](.github/workflows/deploy.yml)` to:
- Build the deployment container via AWS SAM.
- Deploy the serverless stack (`prod/function.py`) to AWS Lambda.
- Automatically wire the new API Gateway URL to the Telegram webhook.

## Commands and Roles

- Commands are routed via Private DM to reduce group noise. The bot broadcasts to the group chat only upon task completion.
- On first interaction, users must self-register:
    - `POST /hi` (captures Telegram ID and @username automatically).

- **Roommate role:**
    - `done`: Log weekly task completion.
    - `/next`: View upcoming global schedule and personal countdown.
    - `/status`: View active/vacation states and last-cleaned dates.
    - `/last`: View 3 most recent log entries.
    - `/volunteer`: Log bonus clean and waive next scheduled turn.
    - `/vacation` (or `/skip`): Freeze rotation placement.
    - `/back`: Return to active rotation (grants priority pass for next turn).

- **Admin role:**
    - `/activate <tg_id> <order> <task_id>`: Activate user and assign rotation slot.
    - `/deletelast`: Remove the newest cleaning log entry.

## Project layout

- `[dev](dev)`: Local polling execution environment.
- `[prod](prod)`: AWS Lambda webhook execution environment.
- `[src](src)`: Core logic and shared helper modules.
- `[src/utils.py](src/utils.py)`: Shared Supabase client, rotation engine, and structured logging.
- `[src/reminder.py](src/reminder.py)`: Friday morning cron execution script.
- `[database/schema.sql](database/schema.sql)`: Supabase DDL definitions.
- `[aws-bot-iac](aws-bot-iac)`: AWS SAM infrastructure-as-code templates.
- `[.github/workflows](.github/workflows)`: CI/CD deployment and cron pipelines.

## Notes

- Keep secrets in `[.env](.env)` (do not commit).
- Dual-track rotation (Entire Home vs. Bathroom) runs as independent queues; updating one track does not mutate the pointer of the other.
- Data is stored in Supabase using ISO week formats (`YYYY-Www`); the presentation layer translates this to calendar weekend dates dynamically.
- `prod/function.py` explicitly requires `HTTPXRequest` for Lambda cold-start compatibility.
- Application failures are logged server-side directly to the `sys_logs` table in Supabase via `src/utils.py`.