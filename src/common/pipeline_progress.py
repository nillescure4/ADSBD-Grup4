"""Atomic, bounded progress snapshots shared by the orchestrator and its UI."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


STATUS_ENV = "ADSBD_PIPELINE_STATUS"
_LOCK = threading.RLock()


def step_zone(step):
    module = step.module
    if module.startswith("src.data_collection.") or module == "src.ingestion.text_to_temporal_landing":
        return "Sources"
    if module.startswith("src.ingestion."):
        return "Landing zone"
    if module.startswith("src.formatting."):
        return "Formatted zone"
    if module.startswith("src.trusted."):
        return "Trusted zone"
    if module.startswith("src.exploitation."):
        return "Exploitation zone"
    return "Verification"


def read_progress(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def update_progress(path, **changes):
    if not path:
        return
    path = Path(path)
    with _LOCK:
        state = read_progress(path)
        log = changes.pop("log", None)
        if log is not None:
            state["log"] = [*state.get("log", []), str(log)[-4000:]][-60:]
        state.update(changes)
        state["updated_at"] = time.time()
        descriptor, temporary = tempfile.mkstemp(prefix=".progress-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
            for attempt in range(5):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02)
        finally:
            Path(temporary).unlink(missing_ok=True)


def create_progress(report_dir, plan):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"live_{uuid.uuid4().hex}.json"
    state = {"version": 1, "status": "running", "phase": "Starting the application",
             "started_at": time.time(), "started_at_utc": datetime.now(timezone.utc).isoformat(),
             "updated_at": time.time(), "current_step": None, "log": [], "error": None,
             "steps": [{"name": step.name, "zone": step_zone(step), "status": "pending"} for step in plan]}
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def step_progress(path, index, status, **details):
    if not path:
        return
    with _LOCK:
        state = read_progress(path)
        state["steps"][index].update(status=status, **details)
        update_progress(path, steps=state["steps"], current_step=index,
                        phase=state["steps"][index]["name"])


class Heartbeat:
    """Distinguish long inference from a vanished orchestration process."""

    def __init__(self, path):
        self.path = path
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop_event.wait(2):
            update_progress(self.path)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
