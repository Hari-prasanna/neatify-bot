-- 1. Dimension: Tasks
CREATE TABLE dim_tasks (
    task_id SERIAL PRIMARY KEY, -- Surrogate Key
    task_description TEXT NOT NULL UNIQUE
);

-- 2. Dimension: Roommates (SCD Type 2 approach)
CREATE TABLE dim_roommates (
    roommate_id SERIAL PRIMARY KEY, -- Surrogate Key
    name TEXT NOT NULL,
    telegram_id BIGINT UNIQUE, -- The ID the bot uses to talk to them
    is_active BOOLEAN DEFAULT TRUE,
    is_on_vacation BOOLEAN DEFAULT FALSE,
    -- Rotation state-machine flags (see notes.md § State Machine Flags):
    skip_next_turn  BOOLEAN DEFAULT FALSE, -- Set by /volunteer; consumed by find_next_person
    is_priority_next BOOLEAN DEFAULT FALSE, -- Set by /back; consumed when that person says done
    effective_start_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    effective_end_date TIMESTAMP WITH TIME ZONE
);

-- Migration: run these two statements if the table already exists
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS skip_next_turn  BOOLEAN DEFAULT FALSE;
-- ALTER TABLE dim_roommates ADD COLUMN IF NOT EXISTS is_priority_next BOOLEAN DEFAULT FALSE;

-- 3. Configuration: Rotation Order
CREATE TABLE rotation_config (
    rotation_id SERIAL PRIMARY KEY,
    roommate_id INTEGER REFERENCES dim_roommates(roommate_id),
    task_id INTEGER REFERENCES dim_tasks(task_id),
    sequence_order INTEGER NOT NULL,
    UNIQUE(sequence_order) -- Ensures no two people have the same spot
);

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