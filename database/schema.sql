-- 1. Dimension: Tasks
CREATE TABLE dim_tasks (
    task_id SERIAL PRIMARY KEY, -- Surrogate Key
    task_description TEXT NOT NULL UNIQUE
    -- Seed data (run separately):
    -- INSERT INTO dim_tasks (task_description) VALUES ('Entire Home'), ('Bathroom');
    -- Task ID 1 = Entire Home (weekend)
    -- Task ID 2 = Bathroom    (mid-week)
);

-- 2. Dimension: Roommates (SCD Type 2 approach)
CREATE TABLE dim_roommates (
    roommate_id SERIAL PRIMARY KEY, -- Surrogate Key
    name TEXT NOT NULL,
    telegram_id BIGINT UNIQUE,       -- Numeric ID Telegram assigns every user
    telegram_username TEXT,          -- Optional @handle; used for @mention formatting
    is_active BOOLEAN DEFAULT TRUE,
    is_on_vacation BOOLEAN DEFAULT FALSE,
    -- Rotation state-machine flags (see notes.md § State Machine Flags):
    skip_next_turn        BOOLEAN DEFAULT FALSE, -- deprecated; use skip_turn_count
    is_priority_next      BOOLEAN DEFAULT FALSE, -- Set by /back or /volunteer; consumed when that person says done
    volunteer_replacing_id INTEGER REFERENCES dim_roommates(roommate_id), -- Set by /volunteer; cleared on done
    effective_start_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    effective_end_date TIMESTAMP WITH TIME ZONE
);

-- Migration: run these if the table already exists
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn          BOOLEAN DEFAULT FALSE;
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS is_priority_next        BOOLEAN DEFAULT FALSE;
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS telegram_username       TEXT;
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_turn_count         INTEGER DEFAULT 0;
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn_task_id  INTEGER REFERENCES dim_tasks(task_id);
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS volunteer_replacing_id  INTEGER REFERENCES dim_roommates(roommate_id);

-- 3. Configuration: Rotation Order
-- UNIQUE(sequence_order, task_id) allows each task track to have its own
-- independent sequence 1-5. e.g. slot-1 for Task 1 can be Ravi, slot-1 for
-- Task 2 can be Priya — the two tracks never interfere with each other.
CREATE TABLE rotation_config (
    rotation_id   SERIAL PRIMARY KEY,
    roommate_id   INTEGER REFERENCES dim_roommates(roommate_id),
    task_id       INTEGER REFERENCES dim_tasks(task_id),
    sequence_order INTEGER NOT NULL,
    UNIQUE(sequence_order, task_id) -- one slot per position per task track
);

-- Migration: if the table already exists with the old single-column constraint:
-- ALTER TABLE rotation_config DROP CONSTRAINT IF EXISTS rotation_config_sequence_order_key;
-- ALTER TABLE rotation_config ADD CONSTRAINT rotation_config_sequence_task_unique
--     UNIQUE (sequence_order, task_id);

-- 4. Fact: Cleaning Logs
CREATE TABLE fct_cleaning_logs (
    log_id SERIAL PRIMARY KEY,
    roommate_id INTEGER REFERENCES dim_roommates(roommate_id),
    task_id INTEGER REFERENCES dim_tasks(task_id),
    cleaned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    week_number TEXT, -- Format: '2026-W22'
    is_volunteer BOOLEAN DEFAULT FALSE -- TRUE = extra clean outside the normal rotation
);

-- 5. Operations: System Logs
-- Captures INFO / WARNING / ERROR events from both dev and prod bot instances.
-- The environment column tells you which instance wrote the row.
-- Note: Admin access is controlled via the ADMIN_TELEGRAM_ID env var, not a DB column,
-- so no is_admin column is needed in dim_roommates.
CREATE TABLE sys_logs (
    log_id   SERIAL PRIMARY KEY,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    log_level    TEXT NOT NULL CHECK (log_level IN ('INFO', 'WARNING', 'ERROR')),
    message      TEXT NOT NULL,
    error_details TEXT,          -- Full Python traceback on ERROR rows, NULL otherwise
    environment  TEXT NOT NULL CHECK (environment IN ('dev', 'prod'))
);

-- 6. Analytics: Private Chat Interaction Logs
-- Every private DM the bot receives is recorded here — who sent what command and when.
-- Kept separate from sys_logs so operational errors and usage analytics don't mix.
-- Useful queries:
--   Most active users:   SELECT telegram_id, name, COUNT(*) FROM interaction_logs GROUP BY 1,2 ORDER BY 3 DESC;
--   Command popularity:  SELECT command, COUNT(*) FROM interaction_logs GROUP BY 1 ORDER BY 2 DESC;
--   Daily activity:      SELECT DATE(created_at), COUNT(*) FROM interaction_logs GROUP BY 1 ORDER BY 1;
CREATE TABLE IF NOT EXISTS interaction_logs (
    interaction_id SERIAL PRIMARY KEY,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    telegram_id    BIGINT      NOT NULL,
    name           TEXT        NOT NULL,
    command        TEXT        NOT NULL,
    environment    TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_logs_telegram_id ON interaction_logs(telegram_id);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_command     ON interaction_logs(command);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_created_at  ON interaction_logs(created_at);