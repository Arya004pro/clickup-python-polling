from app.supabase_db import upsert_employee
from app.clickup import fetch_team_members


def sync_employees_to_supabase() -> int:
    """
    Sync ALL ClickUp team members into employees table.
    Independent of tasks.
    """
    members = fetch_team_members()
    synced = 0

    for m in members:
        payload = {
            "clickup_user_id": m["clickup_user_id"],
            "name": m["name"],
            "email": m.get("email"),
            "role": m.get("role"),
        }

        if upsert_employee(payload):
            synced += 1

    return synced
