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
    effective_start_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    effective_end_date TIMESTAMP WITH TIME ZONE
);

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
    week_number TEXT -- Format: '2026-W22'
);