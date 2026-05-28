# Cleaning Rotation Database Schema (3NF)

This relational schema is designed for implementation in Supabase. It prioritizes data integrity and utilizes a **3rd Normal Form (3NF)** structure to eliminate redundancy and ensure consistent data tracking.

## Table A: `dim_roommates` (The Dimension Table)
This table stores the "Who." It implements **SCD (Slowly Changing Dimension) Type 2** logic to track historical changes, such as a roommate moving out or changing their status.

| Column Name | Type | Description |
| :--- | :--- | :--- |
| `roommate_id` | PK (Int) | A unique identifier for each person. |
| `name` | String | The roommate's name. |
| `telegram_handle` | String | Unique ID or username used by the bot for tagging. |
| `is_active` | Boolean | Indicates if the roommate currently lives in the house. |
| `is_on_vacation` | Boolean | A toggle used for "skip" logic in the rotation. |
| `effective_start_date` | Date | The date this record version became valid. |
| `effective_end_date` | Date | The date this record version expired. |

## Table B: `dim_tasks` (The Task Definition)
This table stores the "What"—defining the specific cleaning duties available.

| Column Name | Type | Description |
| :--- | :--- | :--- |
| `task_id` | PK (Int) | Unique identifier for the task. |
| `task_description` | String | Description of the task (e.g., "Entire Home", "Bathroom Only"). |

## Table C: `rotation_config` (The Map)
This table links roommates to tasks and defines their specific position within the cleaning cycle.

| Column Name | Type | Description |
| :--- | :--- | :--- |
| `rotation_id` | PK (Int) | Unique identifier for the rotation mapping. |
| `roommate_id` | FK (Int) | Reference to `dim_roommates`. |
| `task_id` | FK (Int) | Reference to `dim_tasks`. |
| `sequence_order` | Int | Defines the strict rotation order (e.g., 1 to 5). |

## Table D: `fct_cleaning_logs` (The Fact Table)
The heart of the database. This table records every completion event triggered by a "Done" message.

| Column Name | Type | Description |
| :--- | :--- | :--- |
| `log_id` | PK (Int) | Unique identifier for the log entry. |
| `roommate_id` | FK (Int) | The person who performed the cleaning. |
| `task_id` | FK (Int) | The task that was completed. |
| `cleaned_at` | DateTime | Timestamp of when the completion message was sent. |
| `week_number` | String | Calculated field (e.g., `2026-W22`) used for reporting and analytics. |
