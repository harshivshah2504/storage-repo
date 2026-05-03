import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .auth_manager import APP_STATE_DIR


TASKS_FILE = APP_STATE_DIR / "tasks.json"
_LOCK = threading.Lock()


def is_enabled() -> bool:
    from . import db

    return db.is_enabled()


def init_store() -> None:
    if is_enabled():
        from . import db

        db.ensure_schema()
        db.fail_stale_running_tasks()


def create_task(task: Dict[str, Any]) -> None:
    if is_enabled():
        from . import db

        db.insert_task_record(task)
        return
    with _LOCK:
        tasks = _load_all()
        tasks[task["id"]] = task
        _save_all(tasks)


def update_task(task_id: str, updates: Dict[str, Any]) -> None:
    if is_enabled():
        from . import db

        db.update_task_record(task_id, updates)
        return
    with _LOCK:
        tasks = _load_all()
        task = tasks.get(task_id)
        if not task:
            return
        task.update(updates)
        _save_all(tasks)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    if is_enabled():
        from . import db

        return db.get_task_record(task_id)
    with _LOCK:
        return _load_all().get(task_id)


def list_tasks(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    if is_enabled():
        from . import db

        return db.list_task_records(user_id, limit=limit)
    with _LOCK:
        rows = [t for t in _load_all().values() if t.get("user_id") == user_id]
    rows.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return rows[:limit]


def count_active_tasks(user_id: str, task_type: Optional[str] = None) -> int:
    rows = list_tasks(user_id, limit=500)
    return sum(
        1 for task in rows
        if task.get("status") in {"queued", "running"}
        and (task_type is None or task.get("type") == task_type)
    )


def _load_all() -> Dict[str, Dict[str, Any]]:
    if not TASKS_FILE.exists():
        return {}
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all(tasks: Dict[str, Dict[str, Any]]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TASKS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(tasks, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, TASKS_FILE)
    try:
        os.chmod(TASKS_FILE, 0o600)
    except OSError:
        pass

