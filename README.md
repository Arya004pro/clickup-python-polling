# ClickUp → PostgreSQL Sync

Real-time sync of ClickUp tasks, time tracking, and comments to PostgreSQL (Supabase).

## Features

- **Auto Sync** - Background scheduler syncs every 45 seconds
- **Full & Incremental** - Full sync every 12th run, incremental otherwise
- **Time Tracking** - Session-wise `start_times[]` and `end_times[]` arrays
- **Assigned Comments** - Tracks unresolved comments assigned to users
- **Employee Mapping** - Links ClickUp users to database employees
- **REST API** - FastAPI endpoints for querying synced data

## Tech Stack

- **Python 3.11+**
- **FastAPI** - REST API
- **psycopg2** - Direct PostgreSQL connection
- **APScheduler** - Background sync jobs
- **Supabase** - PostgreSQL database (Transaction Pooler)

## Quick Start

```bash
# Clone & setup
git clone https://github.com/Arya004pro/clickup-python-polling.git
cd clickup-python-polling
python -m venv myenv
myenv\Scripts\activate  # Windows
pip install -r requirements.txt

# Configure .env
CLICKUP_API_TOKEN=pk_xxx
CLICKUP_TEAM_ID=xxx
DATABASE_URL=postgresql://postgres.xxx:password@aws-0-region.pooler.supabase.com:6543/postgres

# Run
uvicorn app.main:app --reload
```

## API Endpoints

| Endpoint                                  | Description                  |
| ----------------------------------------- | ---------------------------- |
| `GET /employees`                          | List all employees           |
| `GET /tasks`                              | List all tasks (paginated)   |
| `GET /tasks/by-employee?employee_id=uuid` | Tasks for specific employee  |
| `GET /tasks/with-time`                    | Tasks with tracked time      |
| `GET /tasks/with-comments`                | Tasks with assigned comments |
| `GET /tasks/{task_id}`                    | Single task details          |
| `GET /sync/tasks`                         | Trigger manual full sync     |
| `GET /sync/employees`                     | Sync employees from ClickUp  |

## Database Schema

### `employees`

| Column          | Type      |
| --------------- | --------- |
| id              | UUID (PK) |
| clickup_user_id | TEXT      |
| name            | TEXT      |
| email           | TEXT      |
| role            | TEXT      |

### `tasks`

| Column           | Type          |
| ---------------- | ------------- |
| id               | UUID (PK)     |
| clickup_task_id  | TEXT (unique) |
| title            | TEXT          |
| status           | TEXT          |
| assignee_name    | TEXT          |
| employee_ids     | UUID[]        |
| start_times      | TEXT[]        |
| end_times        | TEXT[]        |
| tracked_minutes  | INTEGER       |
| assigned_comment | TEXT          |
| ...              | (30+ fields)  |

## Project Structure

```
app/
├── main.py          # FastAPI app & endpoints
├── clickup.py       # ClickUp API client
├── sync.py          # Task sync logic
├── supabase_db.py   # PostgreSQL queries
├── scheduler.py     # Background job scheduler
├── time_tracking.py # Time entry aggregation
├── employee_sync.py # Employee sync
├── config.py        # Environment config
└── logging_config.py
```

## How It Works

1. **Scheduler** runs every 45 seconds
2. **Employees** sync first (ClickUp → DB)
3. **Tasks** fetch from all spaces/lists
4. **Time entries** & **comments** batch-fetched concurrently
5. **Bulk upsert** to PostgreSQL with `ON CONFLICT`
6. **Incremental sync** only fetches recently updated tasks

## Time Tracking Format

```json
{
  "start_times": ["2026-01-13T09:30:00+05:30", "2026-01-12T12:00:00+05:30"],
  "end_times": ["2026-01-13T11:00:00+05:30", "2026-01-12T14:00:00+05:30"],
  "tracked_minutes": 210
}
```

- Arrays sorted by **latest first**
- Each index = one work session

## License

MIT
