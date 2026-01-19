from app.supabase_db import upsert_employee
from app.clickup import fetch_team_members


def sync_employees_to_supabase() -> int:
    """
    Sync ALL ClickUp team members into employees table.
    Independent of tasks.
    """
    import time

<<<<<<< HEAD
    max_retries = 3
    retry_delay = 2  # seconds
=======
    for m in members:
        name = m["name"] if m["name"] else "Unknown"
        payload = {
            "clickup_user_id": m["clickup_user_id"],
            "name": name,
            "email": m.get("email"),
            "role": m.get("role"),
        }
>>>>>>> 994eaba6753de9c5b9aa7c7c06ad18dcd3cf0319

    for attempt in range(max_retries):
        try:
            members = fetch_team_members()
            synced = 0

            for m in members:
                # Skip employees without names (they can't be meaningfully tracked)
                if not m.get("name"):
                    print(
                        f"[employee_sync] Skipping employee with id={m.get('clickup_user_id')} and email={m.get('email')} due to missing name."
                    )
                    continue

                payload = {
                    "clickup_user_id": m["clickup_user_id"],
                    "name": m["name"],
                    "email": m.get("email"),
                    "role": m.get("role"),
                }

                if upsert_employee(payload):
                    synced += 1

            return synced

        except Exception as e:
            if attempt < max_retries - 1:  # Not the last attempt
                print(
                    f"⚠️ Employee sync failed (attempt {attempt + 1}/{max_retries}): {e}"
                )
                time.sleep(retry_delay * (2**attempt))  # Exponential backoff
                continue
            else:
                print(f"❌ Employee sync failed after {max_retries} attempts: {e}")
                raise
