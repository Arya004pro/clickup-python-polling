"""
Sync & Mapping Module for ClickUp MCP Server
Responsible for managing the "Project Mapping" layer between ClickUp entities
and the MCP server, as well as handling data caching.

Acts as the Source of Truth for tracked projects.
"""

import json
import os
import time
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
import requests
from fastmcp import FastMCP
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from clickup_mcp.config import CLICKUP_API_TOKEN, BASE_URL
from clickup_mcp.api_client import client as _client

# --- Constants & Configuration ---
DATA_FILE = "project_map.json"
CACHE_TTL_SECONDS = 3600  # 1 hour
HEADERS = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
MONITORING_CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "monitoring_config.json"
)
REPORT_SPACES_CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "report_spaces_config.json"
)
DEFAULT_MAINTENANCE_TIMEZONE = "Asia/Kolkata"
DEFAULT_MAINTENANCE_RUN_TIMES = [(h, 0) for h in range(9, 22, 2)]  # 09:00..21:00

# --- Persistence Layer ---


class PersistenceManager:
    """Handles loading and saving mappings to a local JSON file."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._data = {"mapped_projects": {}, "discovery_cache": {}}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self._data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error decoding {self.filepath}, starting with empty data.")

    def save(self):
        with open(self.filepath, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def projects(self) -> Dict[str, Any]:
        return self._data.get("mapped_projects", {})

    @property
    def cache(self) -> Dict[str, Any]:
        return self._data.get("discovery_cache", {})

    def add_project(self, alias: str, data: dict):
        self._data["mapped_projects"][alias] = data
        self.save()

    def remove_project(self, alias: str):
        if alias in self._data["mapped_projects"]:
            del self._data["mapped_projects"][alias]
            self.save()

    def set_cache(self, key: str, data: any):
        self._data["discovery_cache"][key] = {"timestamp": time.time(), "data": data}
        self.save()

    def get_cache(self, key: str) -> Optional[Any]:
        entry = self._data["discovery_cache"].get(key)
        if not entry:
            return None
        if time.time() - entry["timestamp"] > CACHE_TTL_SECONDS:
            del self._data["discovery_cache"][key]
            self.save()
            return None
        return entry["data"]

    def clear_all(self):
        self._data = {"mapped_projects": {}, "discovery_cache": {}}
        self.save()

    def prune_expired_cache(self) -> int:
        keys_to_remove = []
        now = time.time()
        for key, entry in self._data["discovery_cache"].items():
            if now - entry.get("timestamp", 0) > CACHE_TTL_SECONDS:
                keys_to_remove.append(key)

        for k in keys_to_remove:
            del self._data["discovery_cache"][k]

        if keys_to_remove:
            self.save()
        return len(keys_to_remove)


db = PersistenceManager(DATA_FILE)

_maintenance_scheduler: Optional[BackgroundScheduler] = None
_maintenance_lock = threading.Lock()
_maintenance_running = False

# --- Helpers ---


def _slugify(text: str) -> str:
    """Converts a string to a slug-like alias."""
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _normalize_entity_name(name: str) -> str:
    """
    Normalize names for robust matching across user input and cached API values.

    Handles:
        - leading/trailing whitespace
        - repeated internal whitespace
        - hyphen/en dash/em dash variance
    """
    value = (name or "").strip().lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"\s*-\s*", " - ", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _semantic_entity_key(name: str) -> str:
    """
    Build a tolerant matching key for entity names.

    Treats "&" and "and" as equivalent and ignores non-alphanumeric
    punctuation differences like dots or slashes.
    """
    value = _normalize_entity_name(name)
    value = re.sub(r"\s*&\s*", " and ", value)
    value = re.sub(r"\b&\b", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _entity_names_match(left: str, right: str) -> bool:
    """Return True when two names refer to the same entity string-wise."""
    return _normalize_entity_name(left) == _normalize_entity_name(right) or (
        _semantic_entity_key(left) != ""
        and _semantic_entity_key(left) == _semantic_entity_key(right)
    )


def _api_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """Generic API GET wrapper — delegates to shared client for connection pooling."""
    try:
        data, err = _client.get(endpoint, params=params)
        if err:
            verbose = os.getenv("SYNC_MAPPING_VERBOSE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if verbose:
                print(
                    f"[sync_mapping] API GET failed: endpoint={endpoint}, error={err}",
                    flush=True,
                )
            return None
        return data
    except Exception as e:
        print(f"API Error: {e}")
        return None


def _fetch_full_structure(entity_id: str, entity_type: str) -> dict:
    """
    Recursively fetches the internal structure of a ClickUp entity.
    Used for mapping projects.
    """
    structure = {"id": entity_id, "type": entity_type, "children": []}

    if entity_type == "space":
        # Get space details
        space_data = _api_get(f"/space/{entity_id}")
        if not space_data:
            return structure
        structure["name"] = space_data.get("name", "Unknown Space")

        # Get Folders
        folders = _api_get(f"/space/{entity_id}/folder")
        if folders:
            for f in folders.get("folders", []):
                structure["children"].append(_fetch_full_structure(f["id"], "folder"))

        # Get Folderless Lists
        lists = _api_get(f"/space/{entity_id}/list")
        if lists:
            for lst in lists.get("lists", []):
                structure["children"].append(
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                )

    elif entity_type == "folder":
        folder_data = _api_get(f"/folder/{entity_id}")
        if not folder_data:
            return structure
        structure["name"] = folder_data.get("name", "Unknown Folder")

        # Get Lists
        lists = _api_get(f"/folder/{entity_id}/list")
        if lists:
            for lst in lists.get("lists", []):
                structure["children"].append(
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                )

    elif entity_type == "list":
        list_data = _api_get(f"/list/{entity_id}")
        if list_data:
            structure["name"] = list_data.get("name", "Unknown List")
            # Lists are leaf nodes (ignoring tasks for structure mapping)

    return structure


def _search_entity_in_structure(structure: dict, search_name: str) -> Optional[dict]:
    """Recursively search for an entity by name in a structure."""
    if not structure or not search_name:
        return None
    if _entity_names_match(structure.get("name", ""), search_name):
        return {
            "id": structure["id"],
            "name": structure["name"],
            "type": structure["type"],
            "structure": structure,
            "found_at": "root",
        }
    for child in structure.get("children", []):
        if _entity_names_match(child.get("name", ""), search_name):
            return {
                "id": child["id"],
                "name": child["name"],
                "type": child["type"],
                "structure": child,
                "parent_name": structure.get("name"),
                "parent_type": structure.get("type"),
                "parent_id": structure.get("id"),
                "found_at": "direct_child",
            }
        if child.get("children"):
            nested = _search_entity_in_structure(child, search_name)
            if nested:
                nested["parent_name"] = structure.get("name")
                nested["parent_type"] = structure.get("type")
                nested["parent_id"] = structure.get("id")
                nested["found_at"] = "nested"
                return nested
    return None


def _refresh_project_mapping(alias: str, project: dict) -> dict:
    """Refresh one mapped project's structure in project_map.json."""
    cid = project.get("clickup_id")
    ctype = project.get("clickup_type")
    if not cid or ctype not in {"space", "folder", "list"}:
        return {
            "alias": alias,
            "success": False,
            "error": "Missing or invalid clickup_id/clickup_type",
        }

    attempts = 3 if ctype == "space" else 2
    new_structure = {}
    for attempt in range(1, attempts + 1):
        new_structure = _fetch_full_structure(cid, ctype)
        if "name" in new_structure:
            break
        if attempt < attempts:
            time.sleep(0.6 * attempt)

    if "name" not in new_structure:
        existing_structure = project.get("structure") or {}
        if isinstance(existing_structure, dict) and existing_structure.get("name"):
            return {
                "alias": alias,
                "success": True,
                "stale": True,
                "children": len(existing_structure.get("children", [])),
                "warning": (
                    f"Refresh failed after {attempts} attempts; kept previous structure "
                    f"(id={cid}, type={ctype})."
                ),
            }
        return {
            "alias": alias,
            "success": False,
            "error": (
                f"Failed to fetch structure from ClickUp API after {attempts} attempts "
                f"(id={cid}, type={ctype}). Check token/team access or whether this entity still exists."
            ),
        }

    updated = dict(project)
    updated["structure"] = new_structure
    updated["last_sync"] = datetime.now(
        tz=timezone(timedelta(hours=5, minutes=30))
    ).strftime("%Y-%m-%d %H:%M:%S IST")
    db.add_project(alias, updated)
    return {
        "alias": alias,
        "success": True,
        "children": len(new_structure.get("children", [])),
    }


def _sync_monitoring_config_list_ids() -> dict:
    """
    Keep monitoring_config.json list_ids fresh by pulling live folder lists.
    """
    if not os.path.exists(MONITORING_CONFIG_FILE):
        return {
            "updated_projects": 0,
            "status": "skipped",
            "reason": "config_not_found",
        }

    try:
        with open(MONITORING_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
    except Exception as e:
        return {"updated_projects": 0, "status": "error", "reason": f"read_failed: {e}"}

    projects = cfg.get("monitored_projects", [])
    changed = 0
    now_ist = datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )

    for p in projects:
        if p.get("type") != "folder" or not p.get("clickup_id"):
            continue

        resp = _api_get(f"/folder/{p['clickup_id']}/list")
        if not resp:
            continue

        live_ids = [lst.get("id") for lst in resp.get("lists", []) if lst.get("id")]
        live_ids = list(dict.fromkeys(live_ids))
        old_ids = [lid for lid in (p.get("list_ids") or []) if lid]

        if live_ids and live_ids != old_ids:
            p["list_ids"] = live_ids
            changed += 1

        # Always stamp when this project's lists were last checked
        p["last_synced"] = now_ist

    # Always write top-level maintenance timestamp + per-project stamps
    cfg["last_maintenance_run"] = now_ist
    try:
        with open(MONITORING_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        return {
            "updated_projects": 0,
            "status": "error",
            "reason": f"write_failed: {e}",
        }

    return {"updated_projects": changed, "status": "ok"}


def _prune_monitoring_config_removed_spaces(active_space_names_lc: set[str]) -> dict:
    """Remove monitored projects whose `space` is no longer present in live team spaces."""
    if not os.path.exists(MONITORING_CONFIG_FILE):
        return {"pruned_projects": 0, "status": "skipped", "reason": "config_not_found"}

    try:
        with open(MONITORING_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return {"pruned_projects": 0, "status": "error", "reason": f"read_failed: {e}"}

    projects = cfg.get("monitored_projects", [])
    if not isinstance(projects, list):
        projects = []

    keep = []
    pruned_aliases: List[str] = []
    for p in projects:
        space_name = str(p.get("space", "")).strip()
        if not space_name:
            keep.append(p)
            continue
        if active_space_names_lc and space_name.lower() not in active_space_names_lc:
            pruned_aliases.append(
                str(p.get("alias", "")).strip() or str(p.get("clickup_id", ""))
            )
            continue
        keep.append(p)

    if len(keep) != len(projects):
        now_ist = datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )
        cfg["monitored_projects"] = keep
        cfg["last_maintenance_run"] = now_ist
        try:
            with open(MONITORING_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            return {
                "pruned_projects": 0,
                "status": "error",
                "reason": f"write_failed: {e}",
            }

    return {
        "pruned_projects": len(pruned_aliases),
        "pruned_aliases": pruned_aliases,
        "status": "ok",
    }


def _prune_report_spaces_removed_spaces(active_space_names_lc: set[str]) -> dict:
    """Remove report spaces whose `name` no longer exists in live team spaces."""
    if not os.path.exists(REPORT_SPACES_CONFIG_FILE):
        return {"pruned_spaces": 0, "status": "skipped", "reason": "config_not_found"}

    try:
        with open(REPORT_SPACES_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return {"pruned_spaces": 0, "status": "error", "reason": f"read_failed: {e}"}

    report_spaces = cfg.get("report_spaces", [])
    if not isinstance(report_spaces, list):
        report_spaces = []

    keep = []
    pruned_names: List[str] = []
    for item in report_spaces:
        space_name = str(item.get("name", "")).strip()
        if not space_name:
            keep.append(item)
            continue
        if active_space_names_lc and space_name.lower() not in active_space_names_lc:
            pruned_names.append(space_name)
            continue
        keep.append(item)

    if len(keep) != len(report_spaces):
        cfg["report_spaces"] = keep
        cfg["last_updated"] = datetime.now(
            tz=timezone(timedelta(hours=5, minutes=30))
        ).strftime("%Y-%m-%d %H:%M:%S IST")
        try:
            with open(REPORT_SPACES_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            return {
                "pruned_spaces": 0,
                "status": "error",
                "reason": f"write_failed: {e}",
            }

    return {
        "pruned_spaces": len(pruned_names),
        "pruned_names": pruned_names,
        "status": "ok",
    }


def _load_monitoring_config() -> dict:
    """Load monitoring scope config from disk."""
    if not os.path.exists(MONITORING_CONFIG_FILE):
        return {"monitored_projects": []}
    try:
        with open(MONITORING_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (PermissionError, OSError, json.JSONDecodeError):
        return {"monitored_projects": []}
    if not isinstance(cfg, dict):
        return {"monitored_projects": []}
    projects = cfg.get("monitored_projects")
    if not isinstance(projects, list):
        cfg["monitored_projects"] = []
    return cfg


def _save_monitoring_config(cfg: dict) -> bool:
    """Persist monitoring scope config to disk."""
    try:
        with open(MONITORING_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except (PermissionError, OSError):
        return False


def _infer_space_name_from_entity(entity: dict, explicit_space_name: str = "") -> str:
    """
    Infer containing space name for a resolved folder/list entity.
    """
    if explicit_space_name and explicit_space_name.strip():
        return explicit_space_name.strip()

    if entity.get("type") == "space":
        return str(entity.get("name") or "").strip()

    if entity.get("parent_type") == "space":
        return str(entity.get("parent_name") or "").strip()

    if entity.get("grandparent_type") == "space":
        return str(entity.get("grandparent_name") or "").strip()

    root_alias = entity.get("root_alias")
    if root_alias and root_alias in db.projects:
        root = db.projects.get(root_alias, {})
        if root.get("clickup_type") == "space":
            return str(root.get("structure", {}).get("name") or "").strip()

    return ""


def _monitored_projects_view(cfg: dict, space_filter: str = "") -> List[Dict[str, Any]]:
    """Return normalized monitored project rows, optionally filtered by space."""
    rows: List[Dict[str, Any]] = []
    items = cfg.get("monitored_projects", [])
    target_space = (space_filter or "").strip().lower()
    for p in items if isinstance(items, list) else []:
        space = str(p.get("space", "")).strip()
        if target_space and space.lower() != target_space:
            continue
        list_ids = [str(x) for x in (p.get("list_ids") or []) if str(x).strip()]
        rows.append(
            {
                "alias": p.get("alias"),
                "clickup_id": p.get("clickup_id"),
                "type": p.get("type"),
                "space": space,
                "list_ids_count": len(list_ids),
                "list_ids": list_ids,
                "last_synced": p.get("last_synced"),
            }
        )
    return rows


def run_mapping_maintenance_once() -> dict:
    """
    Internal maintenance:
    1) Refresh every mapped project structure.
    2) Auto-discover and map any new ClickUp spaces not yet in project_map.json.
    3) Refresh monitored folder list_ids.
    4) Prune expired cache.
    """
    global _maintenance_running
    with _maintenance_lock:
        if _maintenance_running:
            print("[sync_mapping] Maintenance already running — skipped.", flush=True)
            return {"status": "skipped", "reason": "maintenance_already_running"}
        _maintenance_running = True

    _verbose = os.getenv("SYNC_MAPPING_VERBOSE", "").lower() in ("1", "true", "yes")
    try:
        refreshed = []
        failed = []
        stale_spaces_pruned = []

        teams_data = _api_get("/team")
        active_space_ids: set[str] = set()
        active_space_names_lc: set[str] = set()
        spaces_data = None
        if teams_data and teams_data.get("teams"):
            team_id = teams_data["teams"][0]["id"]
            spaces_data = _api_get(f"/team/{team_id}/space")
            if spaces_data:
                active_space_ids = {
                    str(space.get("id"))
                    for space in spaces_data.get("spaces", [])
                    if space.get("id")
                }
                active_space_names_lc = {
                    str(space.get("name", "")).strip().lower()
                    for space in spaces_data.get("spaces", [])
                    if str(space.get("name", "")).strip()
                }

        # --- Prune any non-space entries (folders/lists don't belong at top level) ---
        pruned_non_spaces = []
        for alias, project in list(db.projects.items()):
            if project.get("clickup_type") != "space":
                db.remove_project(alias)
                pruned_non_spaces.append(alias)
                print(
                    f"[sync_mapping]   ⚠ Removed non-space entry '{alias}' "
                    f"(type={project.get('clickup_type')}) — only spaces are top-level projects.",
                    flush=True,
                )

        for alias, project in list(db.projects.items()):
            project_id = str(project.get("clickup_id") or "").strip()
            project_type = str(project.get("clickup_type") or "").strip()

            # If we have a live spaces snapshot and this mapped space no longer exists,
            # prune immediately before attempting refresh.
            if (
                project_type == "space"
                and project_id
                and active_space_ids
                and project_id not in active_space_ids
            ):
                db.remove_project(alias)
                stale_spaces_pruned.append(alias)
                print(
                    f"[sync_mapping]   ⚠ Removed stale space mapping '{alias}' "
                    f"(id={project_id}) — not returned by current team spaces.",
                    flush=True,
                )
                continue

            result = _refresh_project_mapping(alias, project)
            if result.get("success"):
                refreshed.append(result["alias"])
                if result.get("stale") and _verbose:
                    print(
                        f"[sync_mapping]   ⚠ {alias}: {result.get('warning')}",
                        flush=True,
                    )
                if _verbose:
                    print(
                        f"[sync_mapping]   ✓ {alias} ({result.get('children', 0)} children)",
                        flush=True,
                    )
            else:
                failed.append(
                    {"alias": alias, "error": result.get("error", "unknown_error")}
                )
                print(f"[sync_mapping]   ✗ {alias}: {result.get('error')}", flush=True)

        # --- Auto-discover new spaces ---
        auto_mapped = []
        if spaces_data:
            existing_ids = {p["clickup_id"] for p in db.projects.values()}
            for space in spaces_data.get("spaces", []):
                if space["id"] in existing_ids:
                    continue
                # New space found — auto-map it
                new_alias = _slugify(space["name"])
                if new_alias in db.projects:
                    new_alias = f"{new_alias}-{space['id'][-4:]}"
                structure = _fetch_full_structure(space["id"], "space")
                if "name" not in structure:
                    print(
                        f"[sync_mapping]   ⚠ Skipping new space '{space['name']}' — could not fetch structure.",
                        flush=True,
                    )
                    continue
                db.add_project(
                    new_alias,
                    {
                        "alias": new_alias,
                        "clickup_id": space["id"],
                        "clickup_type": "space",
                        "last_sync": (
                            datetime.now(
                                tz=timezone(timedelta(hours=5, minutes=30))
                            ).strftime("%Y-%m-%d %H:%M:%S IST")
                        ),
                        "structure": structure,
                    },
                )
                auto_mapped.append(new_alias)
                print(
                    f"[sync_mapping]   + Auto-mapped new space '{space['name']}' as '{new_alias}'",
                    flush=True,
                )

        monitor_res = _sync_monitoring_config_list_ids()
        monitor_prune_res = _prune_monitoring_config_removed_spaces(
            active_space_names_lc
        )
        report_spaces_prune_res = _prune_report_spaces_removed_spaces(
            active_space_names_lc
        )
        pruned = db.prune_expired_cache()

        if _verbose or failed or auto_mapped or pruned_non_spaces:
            print(
                f"[sync_mapping] Done — {len(refreshed)} refreshed, {len(failed)} failed, "
                f"{len(auto_mapped)} new space(s) auto-mapped, "
                f"{len(stale_spaces_pruned)} stale space(s) pruned, "
                f"{len(pruned_non_spaces)} non-space entries pruned, "
                f"{monitor_res.get('updated_projects', 0)} monitoring list_ids updated, "
                f"{monitor_prune_res.get('pruned_projects', 0)} monitoring entries pruned, "
                f"{report_spaces_prune_res.get('pruned_spaces', 0)} report spaces pruned, "
                f"{pruned} cache entries pruned.",
                flush=True,
            )

        return {
            "status": "success",
            "mapped_projects_refreshed": len(refreshed),
            "mapped_projects_failed": failed,
            "auto_mapped_spaces": auto_mapped,
            "stale_space_mappings_pruned": stale_spaces_pruned,
            "non_space_entries_pruned": pruned_non_spaces,
            "monitoring_config": monitor_res,
            "monitoring_config_pruned": monitor_prune_res,
            "report_spaces_config_pruned": report_spaces_prune_res,
            "cache_entries_pruned": pruned,
            "ran_at": time.time(),
        }
    finally:
        _maintenance_running = False


def start_mapping_maintenance_scheduler(
    timezone: str = DEFAULT_MAINTENANCE_TIMEZONE,
    run_times: Optional[List[Tuple[int, int]]] = None,
    run_on_startup: bool = False,
) -> bool:
    """
    Start maintenance scheduler. Returns False if already running.
    """
    global _maintenance_scheduler
    if _maintenance_scheduler and _maintenance_scheduler.running:
        return False

    if run_times is None:
        run_times = DEFAULT_MAINTENANCE_RUN_TIMES

    tz = ZoneInfo(timezone)
    _maintenance_scheduler = BackgroundScheduler(timezone=tz)

    for idx, (hour, minute) in enumerate(run_times):
        _maintenance_scheduler.add_job(
            run_mapping_maintenance_once,
            trigger="cron",
            hour=hour,
            minute=minute,
            id=f"mapping_maintenance_{idx}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    _maintenance_scheduler.start()

    if run_on_startup:

        def _startup_sync():
            run_mapping_maintenance_once()

        threading.Thread(target=_startup_sync, daemon=True).start()

    return True


def find_entity_anywhere(entity_name: str) -> Optional[dict]:
    """
    Universal entity finder. Searches for a space, folder, or list by name.
    Search priority:
    1. Mapped projects in project_map.json
    2. Live API search across all spaces
    """
    if not entity_name:
        return None
    # 1. Search in mapped projects
    for alias, data in db.projects.items():
        structure = data.get("structure", {})
        if any(
            _entity_names_match(candidate, entity_name)
            for candidate in (alias, data.get("alias", ""), structure.get("name", ""))
        ):
            return {
                "id": data["clickup_id"],
                "name": structure.get("name", alias),
                "type": data["clickup_type"],
                "structure": structure,
                "source": "project_map",
                "alias": alias,
                "found_at": "root",
            }
        result = _search_entity_in_structure(structure, entity_name)
        if result:
            result["source"] = "project_map"
            result["root_alias"] = alias
            return result

    # 2. Live API search
    teams_data = _api_get("/team")
    if not teams_data:
        return None
    team_id = teams_data["teams"][0]["id"]
    spaces_data = _api_get(f"/team/{team_id}/space")
    if not spaces_data:
        return None

    for space in spaces_data.get("spaces", []):
        if _entity_names_match(space["name"], entity_name):
            return {
                "id": space["id"],
                "name": space["name"],
                "type": "space",
                "structure": _fetch_full_structure(space["id"], "space"),
                "source": "api",
                "found_at": "root",
            }
        folders_data = _api_get(f"/space/{space['id']}/folder")
        if folders_data:
            for folder in folders_data.get("folders", []):
                if _entity_names_match(folder["name"], entity_name):
                    return {
                        "id": folder["id"],
                        "name": folder["name"],
                        "type": "folder",
                        "structure": _fetch_full_structure(folder["id"], "folder"),
                        "parent_name": space["name"],
                        "parent_type": "space",
                        "parent_id": space["id"],
                        "source": "api",
                        "found_at": "folder",
                    }
                lists_data = _api_get(f"/folder/{folder['id']}/list")
                if lists_data:
                    for lst in lists_data.get("lists", []):
                        if _entity_names_match(lst["name"], entity_name):
                            return {
                                "id": lst["id"],
                                "name": lst["name"],
                                "type": "list",
                                "structure": {
                                    "id": lst["id"],
                                    "name": lst["name"],
                                    "type": "list",
                                },
                                "parent_name": folder["name"],
                                "parent_type": "folder",
                                "parent_id": folder["id"],
                                "grandparent_name": space["name"],
                                "grandparent_type": "space",
                                "grandparent_id": space["id"],
                                "source": "api",
                                "found_at": "list_in_folder",
                            }
        lists_data = _api_get(f"/space/{space['id']}/list")
        if lists_data:
            for lst in lists_data.get("lists", []):
                if _entity_names_match(lst["name"], entity_name):
                    return {
                        "id": lst["id"],
                        "name": lst["name"],
                        "type": "list",
                        "structure": {
                            "id": lst["id"],
                            "name": lst["name"],
                            "type": "list",
                        },
                        "parent_name": space["name"],
                        "parent_type": "space",
                        "parent_id": space["id"],
                        "source": "api",
                        "found_at": "folderless_list",
                    }
    return None


def _resolve_monitored_scope_entity(entity_name: str) -> Optional[dict]:
    """
    Resolve synthetic monitored scopes used by report tools.

    Supported expressions:
      - "monitored"            -> aggregate monitored project scope
      - "Monitored AIX"        -> monitored subset within space AIX
      - "monitored:AIX"        -> same as above
    """
    raw = (entity_name or "").strip()
    if not raw:
        return None

    lower = raw.lower()
    if lower == "monitored":
        try:
            with open(MONITORING_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            projects = cfg.get("monitored_projects", [])
            if not projects:
                return None
        except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
            return None

        return {
            "id": "monitoring_scope:monitored",
            "name": "monitored",
            "type": "project",
            "source": "monitoring_config",
            "found_at": "monitoring_scope",
            "monitoring_scope": True,
        }

    target = ""
    if lower.startswith("monitored "):
        target = raw[len("monitored ") :].strip()
    elif lower.startswith("monitored:"):
        target = raw.split(":", 1)[1].strip()
    else:
        return None

    if not target:
        return None

    target_lower = target.lower()
    try:
        with open(MONITORING_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        projects = cfg.get("monitored_projects", [])
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None

    matching = [
        p for p in projects if str(p.get("space", "")).strip().lower() == target_lower
    ]
    if not matching:
        return None

    resolved_space = str(matching[0].get("space", target)).strip() or target
    return {
        "id": f"monitoring_scope:space:{resolved_space}",
        "name": f"Monitored {resolved_space}",
        "type": "space",
        "source": "monitoring_config",
        "found_at": "monitoring_scope",
        "monitoring_scope": True,
    }


def _load_report_spaces_config() -> dict:
    """Load report-space selection config from disk."""
    if not os.path.exists(REPORT_SPACES_CONFIG_FILE):
        return {"report_spaces": []}
    try:
        with open(REPORT_SPACES_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (PermissionError, OSError, json.JSONDecodeError):
        return {"report_spaces": []}
    if not isinstance(cfg, dict):
        return {"report_spaces": []}
    report_spaces = cfg.get("report_spaces")
    if not isinstance(report_spaces, list):
        cfg["report_spaces"] = []
    return cfg


def _save_report_spaces_config(cfg: dict) -> bool:
    try:
        cfg["last_updated"] = datetime.now(
            tz=timezone(timedelta(hours=5, minutes=30))
        ).strftime("%Y-%m-%d %H:%M:%S IST")
        with open(REPORT_SPACES_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except (PermissionError, OSError):
        return False


# --- Tools Definition ---


def register_sync_mapping_tools(mcp: FastMCP):
    @mcp.tool()
    def discover_hierarchy(
        workspace_id: str = None, show_archived: bool = False
    ) -> dict:
        """
        List all spaces, folders, and lists in a workspace for project mapping.
        Fetches the full tree and caches the result.
        """
        # 1. Resolve Workspace
        target_ws_id = workspace_id
        if not target_ws_id:
            teams_data = _api_get("/team")
            if not teams_data or not teams_data.get("teams"):
                return {"error": "No teams/workspaces found."}
            target_ws_id = teams_data["teams"][0]["id"]

        cache_key = f"hierarchy_{target_ws_id}_{show_archived}"
        cached = db.get_cache(cache_key)
        if cached:
            return {"source": "cache", "data": cached}

        # 2. Fetch Spaces
        spaces_data = _api_get(
            f"/team/{target_ws_id}/space", {"archived": str(show_archived).lower()}
        )
        if not spaces_data:
            return {"error": "Failed to fetch spaces."}

        hierarchy = []
        for space in spaces_data.get("spaces", []):
            space_node = {
                "id": space["id"],
                "name": space["name"],
                "type": "space",
                "folders": [],
                "folderless_lists": [],
            }

            # 3. Fetch Folders
            folders_data = _api_get(
                f"/space/{space['id']}/folder", {"archived": str(show_archived).lower()}
            )
            if folders_data:
                for folder in folders_data.get("folders", []):
                    folder_node = {
                        "id": folder["id"],
                        "name": folder["name"],
                        "type": "folder",
                        "lists": [],
                    }
                    # 4. Fetch Lists inside Folders
                    lists_data = _api_get(
                        f"/folder/{folder['id']}/list",
                        {"archived": str(show_archived).lower()},
                    )
                    if lists_data:
                        folder_node["lists"] = [
                            {"id": lst["id"], "name": lst["name"], "type": "list"}
                            for lst in lists_data.get("lists", [])
                        ]
                    space_node["folders"].append(folder_node)

            # 5. Fetch Folderless Lists
            fl_lists_data = _api_get(
                f"/space/{space['id']}/list", {"archived": str(show_archived).lower()}
            )
            if fl_lists_data:
                space_node["folderless_lists"] = [
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                    for lst in fl_lists_data.get("lists", [])
                ]

            hierarchy.append(space_node)

        result = {"workspace_id": target_ws_id, "hierarchy": hierarchy}
        db.set_cache(cache_key, result)
        return {"source": "api", "data": result}

    @mcp.tool()
    def map_project(id: str, type: str, alias: str = None) -> dict:
        """
        Map a ClickUp Space as a top-level 'Project'.
        Verifies the space ID/Name, fetches its full internal structure, and persists the mapping.

        Only spaces can be mapped as top-level projects. Folders and lists are tracked
        as sub-entities within their parent space's structure.

        Args:
            id: Space ID or Space Name (names are auto-resolved to IDs).
            type: Must be 'space'.
            alias: Optional custom alias (auto-generated from space name if not provided).

        Returns:
            Mapping confirmation with project details.
        """
        if type != "space":
            return {
                "error": "Only 'space' can be mapped as a top-level project. "
                "Folders and lists are sub-entities within their parent space — "
                "map the parent space instead.",
                "hint": "Use discover_hierarchy() to find the space that contains this entity.",
            }

        # Step 1: Resolve name to ID if needed (for spaces)
        resolved_id = id
        if type == "space" and not id.isdigit():
            # Try to resolve space name to ID
            teams_data = _api_get("/team")
            if teams_data and teams_data.get("teams"):
                team_id = teams_data["teams"][0]["id"]
                spaces_data = _api_get(f"/team/{team_id}/space")

                if spaces_data:
                    all_spaces = spaces_data.get("spaces", [])
                    found = False

                    for s in all_spaces:
                        if _entity_names_match(s["name"], id):
                            resolved_id = s["id"]
                            found = True
                            break

                    if not found:
                        return {
                            "error": f"Space '{id}' not found",
                            "hint": f"Available spaces: {[s['name'] for s in all_spaces]}",
                            "available_spaces": [
                                {"id": s["id"], "name": s["name"]} for s in all_spaces
                            ],
                        }

        # Step 2: Verify ID and get initial structure
        structure = _fetch_full_structure(resolved_id, type)
        if "name" not in structure:
            return {
                "error": f"Could not verify {type} with ID/Name '{id}'. Check ID or permissions."
            }

        final_alias = alias or _slugify(structure["name"])

        # Step 3: Check if alias exists
        if final_alias in db.projects:
            return {
                "error": f"Alias '{final_alias}' already exists. Please choose another."
            }

        mapping_data = {
            "alias": final_alias,
            "clickup_id": resolved_id,
            "clickup_type": type,
            "last_sync": (
                datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).strftime(
                    "%Y-%m-%d %H:%M:%S IST"
                )
            ),
            "structure": structure,
        }

        db.add_project(final_alias, mapping_data)

        return {
            "success": True,
            "message": f"Mapped {type} '{structure['name']}' as '{final_alias}'",
            "project_details": mapping_data,
        }

    @mcp.tool()
    def list_mapped_projects() -> List[Dict[str, Any]]:
        """Show all currently mapped projects."""
        projects = db.projects
        output = []
        for alias, data in projects.items():
            output.append(
                {
                    "alias": alias,
                    "clickup_id": data.get("clickup_id"),
                    "type": data.get("clickup_type"),
                    "last_sync": data.get("last_sync"),
                }
            )
        return output

    @mcp.tool()
    def get_mapped_project(alias: str) -> dict:
        """Get detailed info about a specifically mapped project."""
        project = db.projects.get(alias)
        if not project:
            return {"error": f"Project alias '{alias}' not found."}
        return project["structure"]

    @mcp.tool()
    def refresh_project(alias: str) -> dict:
        """Force a refresh of a project's structure from ClickUp."""
        project = db.projects.get(alias)
        if not project:
            return {"error": f"Project alias '{alias}' not found."}
        result = _refresh_project_mapping(alias, project)
        if not result.get("success"):
            return {
                "error": result.get(
                    "error", "Failed to refresh structure from ClickUp API."
                )
            }

        return {
            "success": True,
            "message": f"Refreshed structure for '{alias}'",
            "structure_summary": f"Contains {result.get('children', 0)} top-level items.",
        }

    @mcp.tool()
    def unmap_project(alias: str) -> dict:
        """Remove a project mapping."""
        if alias not in db.projects:
            return {"error": f"Project alias '{alias}' not found."}

        db.remove_project(alias)
        return {"success": True, "message": f"Unmapped project '{alias}'"}

    @mcp.tool()
    def get_sync_status() -> dict:
        """Get the overall health of the sync and cache."""
        return {
            "mapped_projects_count": len(db.projects),
            "cached_items_count": len(db.cache),
            "storage_file": DATA_FILE,
            "status": "active",
        }

    @mcp.tool()
    def list_spaces(workspace_id: str = None) -> dict:
        """List all spaces and indicate which ones are already mapped."""
        # Get actual spaces
        teams_data = _api_get("/team")
        if not teams_data:
            return {"error": "API Error"}

        ws_id = workspace_id or teams_data["teams"][0]["id"]
        spaces_data = _api_get(f"/team/{ws_id}/space")

        if not spaces_data:
            return {"error": "Failed to fetch spaces."}

        # Get local map IDs for comparison
        mapped_ids = {
            p["clickup_id"]
            for p in db.projects.values()
            if p["clickup_type"] == "space"
        }

        output = []
        for s in spaces_data.get("spaces", []):
            output.append(
                {"id": s["id"], "name": s["name"], "is_mapped": s["id"] in mapped_ids}
            )

        return {"workspace_id": ws_id, "spaces": output}

    @mcp.tool()
    def clear_sync(confirm: bool) -> dict:
        """Clear all project mappings and discovery cache. Destructive."""
        if not confirm:
            return {"error": "You must set confirm=True to wipe the database."}

        db.clear_all()
        return {"success": True, "message": "All mappings and cache have been wiped."}

    @mcp.tool()
    def prune_cache() -> dict:
        """Remove expired cache entries."""
        count = db.prune_expired_cache()
        return {"success": True, "removed_entries": count, "message": "Cache pruned."}

    @mcp.tool()
    def find_project_anywhere(project_name: str) -> dict:
        """
        Universal project/entity finder. Search for any space, folder, or list by name.
        Works regardless of where the entity is located in the hierarchy.
        Use this BEFORE any report tool to resolve the entity type and ID.
        """
        # Handle monitored scopes early to avoid false "not found" on
        # synthetic names like "Monitored AIX".
        result = _resolve_monitored_scope_entity(project_name)
        if not result:
            result = find_entity_anywhere(project_name)
        if not result:
            return {
                "error": f"Project '{project_name}' not found",
                "suggestion": "Try discover_hierarchy() to see all available spaces, folders, and lists",
            }
        response = {
            "found": True,
            "name": result["name"],
            "id": result["id"],
            "type": result["type"],
            "source": result["source"],
            "location": result.get("found_at", "unknown"),
        }
        if result.get("parent_name"):
            response["parent"] = {
                "name": result["parent_name"],
                "type": result["parent_type"],
                "id": result["parent_id"],
            }
        if result.get("grandparent_name"):
            response["grandparent"] = {
                "name": result["grandparent_name"],
                "type": result["grandparent_type"],
                "id": result["grandparent_id"],
            }
        if result.get("structure"):
            children = result["structure"].get("children", [])
            if children:
                response["contains"] = {
                    "total_children": len(children),
                    "children": [
                        {"name": c.get("name"), "type": c.get("type")}
                        for c in children[:10]
                    ],
                }
                if len(children) > 10:
                    response["contains"]["note"] = (
                        f"Showing 10 of {len(children)} items"
                    )
        if result.get("monitoring_scope"):
            if result.get("type") == "space":
                response["usage_hint"] = (
                    f"Use get_space_task_report(space_name='{result['name']}', period_type='today')"
                )
            else:
                response["usage_hint"] = (
                    "Use monitored scope with report tools (e.g., project_name='monitored')."
                )
        else:
            response["usage_hint"] = (
                f"Use this {result['type']} ID ({result['id']}) with report generation tools"
            )
        return response

    @mcp.tool()
    def get_environment_context() -> dict:
        """
        Bootstrap tool: returns MCP environment state including
        mapped projects, cache status, and usage guidance.
        Call this at session start.
        """
        projects = [
            {
                "alias": alias,
                "clickup_id": data.get("clickup_id"),
                "type": data.get("clickup_type"),
                "last_sync": data.get("last_sync"),
            }
            for alias, data in db.projects.items()
        ]

        return {
            "mapped_projects_count": len(projects),
            "mapped_projects": projects,
            "cached_items": len(db.cache),
            "storage": DATA_FILE,
            "server_status": "ready",
            "usage_guidance": {
                "mapped_projects": "Use mapped project tools for analytics/reporting.",
                "raw_clickup": "Use raw fetch tools only for discovery or unmapped entities.",
            },
        }

    @mcp.tool()
    def trigger_mapping_maintenance() -> dict:
        """
        Manually trigger the mapping maintenance routine to update all mapped
        project structures and monitoring configs with the latest data from ClickUp.
        """
        from typing import Dict, Any

        result: Dict[str, Any] = run_mapping_maintenance_once()
        return result

    @mcp.tool()
    def list_report_spaces() -> dict:
        """
        List spaces selected for automated report generation.

        This is separate from project_map.json:
        - project_map.json: all mapped spaces for discovery/resolution
        - report_spaces_config.json: subset of spaces to generate reports for
        """
        cfg = _load_report_spaces_config()
        items = cfg.get("report_spaces", [])
        mapped_space_names = {
            (p.get("structure", {}).get("name") or "").strip().lower()
            for p in db.projects.values()
            if p.get("clickup_type") == "space"
        }

        normalized = []
        for s in items:
            name = str(s.get("name", "")).strip()
            if not name:
                continue
            normalized.append(
                {
                    "name": name,
                    "display": s.get("display") or name,
                    "query_label": s.get("query_label") or name,
                    "scope": s.get("scope") or "full",
                    "is_mapped": name.lower() in mapped_space_names,
                }
            )

        return {
            "config_file": REPORT_SPACES_CONFIG_FILE,
            "count": len(normalized),
            "spaces": normalized,
            "last_updated": cfg.get("last_updated"),
        }

    @mcp.tool()
    def add_report_space(
        space_name: str,
        scope: str = "full",
        display: str = "",
        query_label: str = "",
    ) -> dict:
        """
        Add/update one space in report_spaces_config.json.

        Args:
            space_name: ClickUp space name (resolved/validated against live API/project_map).
            scope: "full" or "monitored"
            display: Optional label used in UI/email
            query_label: Optional label sent to report prompt/tool call
        """
        desired_scope = (scope or "full").strip().lower()
        if desired_scope not in {"full", "monitored"}:
            return {"error": "Invalid scope. Use 'full' or 'monitored'."}

        found = find_entity_anywhere(space_name)
        if not found:
            return {"error": f"Space '{space_name}' not found."}
        if found.get("type") != "space":
            return {
                "error": f"'{space_name}' resolved to {found.get('type')}, not a space.",
                "hint": "Provide a valid space name.",
            }

        canonical_name = str(found.get("name") or space_name).strip()
        if not canonical_name:
            return {"error": "Resolved space name is empty."}

        cfg = _load_report_spaces_config()
        report_spaces = cfg.get("report_spaces", [])
        if not isinstance(report_spaces, list):
            report_spaces = []

        entry = {
            "name": canonical_name,
            "display": (display or canonical_name).strip(),
            "query_label": (
                (query_label or f"Monitored {canonical_name}").strip()
                if desired_scope == "monitored"
                else (query_label or canonical_name).strip()
            ),
            "scope": desired_scope,
        }

        replaced = False
        for i, existing in enumerate(report_spaces):
            if _entity_names_match(str(existing.get("name", "")), canonical_name):
                report_spaces[i] = entry
                replaced = True
                break
        if not replaced:
            report_spaces.append(entry)

        cfg["report_spaces"] = report_spaces
        if not _save_report_spaces_config(cfg):
            return {
                "error": f"Failed to write report spaces config at {REPORT_SPACES_CONFIG_FILE}"
            }

        return {
            "success": True,
            "message": (
                f"Updated report space '{canonical_name}'"
                if replaced
                else f"Added report space '{canonical_name}'"
            ),
            "entry": entry,
            "total_spaces": len(report_spaces),
        }

    @mcp.tool()
    def remove_report_space(space_name: str) -> dict:
        """
        Remove one space from report_spaces_config.json by name.
        """
        cfg = _load_report_spaces_config()
        report_spaces = cfg.get("report_spaces", [])
        if not isinstance(report_spaces, list):
            report_spaces = []

        keep = []
        removed = None
        target = (space_name or "").strip()
        for item in report_spaces:
            name = str(item.get("name", "")).strip()
            if _entity_names_match(name, target) and removed is None:
                removed = item
                continue
            keep.append(item)

        if removed is None:
            return {"error": f"Report space '{space_name}' not found."}

        cfg["report_spaces"] = keep
        if not _save_report_spaces_config(cfg):
            return {
                "error": f"Failed to write report spaces config at {REPORT_SPACES_CONFIG_FILE}"
            }

        return {
            "success": True,
            "message": f"Removed report space '{removed.get('name', space_name)}'",
            "total_spaces": len(keep),
        }

    @mcp.tool()
    def list_monitored_projects(space_name: str = "") -> dict:
        """
        List monitored projects configured in monitoring_config.json.
        Optional filter: only show monitored projects in one space.
        """
        cfg = _load_monitoring_config()
        filtered = _monitored_projects_view(cfg, space_name)

        return {
            "config_file": MONITORING_CONFIG_FILE,
            "count": len(filtered),
            "projects": filtered,
            "last_maintenance_run": cfg.get("last_maintenance_run"),
        }

    @mcp.tool()
    def add_monitored_project(
        project_name: str = "",
        alias: str = "",
        space_name: str = "",
        project_name_or_alias: str = "",
    ) -> dict:
        """
        Add/update one monitored project (folder/list) in monitoring_config.json.

        Intended use:
        - Keep only selected projects from a space in monitored scope
        - Example: add 6 chosen folders from AIX
        """
        effective_project_name = (project_name or project_name_or_alias).strip()
        if not effective_project_name:
            return {
                "error": "Provide project_name (or project_name_or_alias) to add a monitored project."
            }

        resolved = find_entity_anywhere(effective_project_name)
        if not resolved:
            return {"error": f"Project '{effective_project_name}' not found."}

        entity_type = str(resolved.get("type") or "").strip().lower()
        entity_id = str(resolved.get("id") or "").strip()
        entity_name = str(resolved.get("name") or effective_project_name).strip()
        if entity_type not in {"folder", "list"}:
            return {
                "error": (
                    f"'{effective_project_name}' resolved to type '{entity_type}'. "
                    "Only folder or list can be added as monitored project."
                ),
                "hint": "Use a folder/list name. For full-space selection use add_report_space().",
            }
        if not entity_id:
            return {"error": "Resolved project has no ID."}

        list_ids: List[str] = []
        if entity_type == "folder":
            resp = _api_get(f"/folder/{entity_id}/list")
            list_ids = [
                str(lst.get("id"))
                for lst in (resp or {}).get("lists", [])
                if lst.get("id")
            ]
            list_ids = list(dict.fromkeys(list_ids))
            if not list_ids:
                return {
                    "error": f"Folder '{entity_name}' has no lists to monitor.",
                    "hint": "Only folders with lists can be monitored.",
                }
        else:  # list
            list_ids = [entity_id]

        resolved_space_name = _infer_space_name_from_entity(resolved, space_name)
        if not resolved_space_name:
            return {
                "error": f"Could not infer space for '{entity_name}'.",
                "hint": "Pass space_name explicitly (e.g., space_name='AIX').",
            }

        final_alias = (alias or entity_name).strip()
        now_ist = datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )
        entry = {
            "alias": final_alias,
            "clickup_id": entity_id,
            "type": entity_type,
            "space": resolved_space_name,
            "list_ids": list_ids,
            "last_synced": now_ist,
        }

        cfg = _load_monitoring_config()
        projects = cfg.get("monitored_projects", [])
        if not isinstance(projects, list):
            projects = []

        replaced = False
        for i, p in enumerate(projects):
            same_alias = str(p.get("alias", "")).strip().lower() == final_alias.lower()
            same_id = str(p.get("clickup_id", "")).strip() == entity_id
            if same_alias or same_id:
                projects[i] = entry
                replaced = True
                break
        if not replaced:
            projects.append(entry)

        cfg["monitored_projects"] = projects
        cfg["last_maintenance_run"] = now_ist
        if not _save_monitoring_config(cfg):
            return {
                "error": f"Failed to write monitoring config at {MONITORING_CONFIG_FILE}"
            }

        updated_for_space = _monitored_projects_view(cfg, resolved_space_name)

        return {
            "success": True,
            "message": (
                f"Updated monitored project '{final_alias}'"
                if replaced
                else f"Added monitored project '{final_alias}'"
            ),
            "project": entry,
            "total_monitored_projects": len(projects),
            "updated_projects_for_space": updated_for_space,
        }

    @mcp.tool()
    def remove_monitored_project(
        project_name_or_alias: str, space_name: str = ""
    ) -> dict:
        """
        Remove one monitored project from monitoring_config.json by alias or clickup_id.

        Args:
            project_name_or_alias: Alias or clickup_id of monitored project.
            space_name: Optional space filter to disambiguate same alias across spaces.
        """
        target = (project_name_or_alias or "").strip().lower()
        if not target:
            return {"error": "project_name_or_alias is required."}
        target_space = (space_name or "").strip().lower()

        cfg = _load_monitoring_config()
        projects = cfg.get("monitored_projects", [])
        if not isinstance(projects, list):
            projects = []

        keep = []
        removed = None
        for p in projects:
            alias = str(p.get("alias", "")).strip().lower()
            clickup_id = str(p.get("clickup_id", "")).strip().lower()
            project_space = str(p.get("space", "")).strip().lower()
            matches_target = alias == target or clickup_id == target
            matches_space = not target_space or project_space == target_space
            if removed is None and matches_target and matches_space:
                removed = p
                continue
            keep.append(p)

        if removed is None:
            aliases = [
                str(p.get("alias", "")).strip() for p in projects if p.get("alias")
            ]
            return {
                "error": f"Monitored project '{project_name_or_alias}' not found.",
                "space_filter": space_name or None,
                "available_aliases": aliases[:25],
            }

        now_ist = datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )
        cfg["monitored_projects"] = keep
        cfg["last_maintenance_run"] = now_ist
        if not _save_monitoring_config(cfg):
            return {
                "error": f"Failed to write monitoring config at {MONITORING_CONFIG_FILE}"
            }

        result_space = (space_name or str(removed.get("space") or "")).strip()
        updated_for_space = _monitored_projects_view(cfg, result_space)

        return {
            "success": True,
            "message": f"Removed monitored project '{removed.get('alias', project_name_or_alias)}'",
            "space": removed.get("space"),
            "total_monitored_projects": len(keep),
            "updated_projects_for_space": updated_for_space,
        }
