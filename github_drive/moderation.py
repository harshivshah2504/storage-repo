import json
import os
import threading
from pathlib import Path
from typing import Dict

from .auth_manager import APP_STATE_DIR
from .api import now_utc_iso


REPORTS_FILE = APP_STATE_DIR / "abuse_reports.json"
_LOCK = threading.Lock()


def save_abuse_report(report: Dict) -> None:
    from . import db

    if db.is_enabled():
        db.insert_abuse_report(
            report["id"],
            report["reporter"],
            report["subject"],
            report["details"],
        )
        return

    with _LOCK:
        reports = []
        if REPORTS_FILE.exists():
            try:
                reports = json.loads(REPORTS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reports = []
        report = dict(report)
        report.setdefault("created_at", now_utc_iso())
        reports.append(report)
        REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = REPORTS_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(reports[-1000:], indent=2), encoding="utf-8")
        os.replace(tmp_path, REPORTS_FILE)
        try:
            os.chmod(REPORTS_FILE, 0o600)
        except OSError:
            pass

