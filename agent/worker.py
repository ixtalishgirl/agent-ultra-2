"""One worker for Agent Ultra 2.

A worker runs one approach for one task and returns a report dict
with task_id, worker_id, approach, result and evidence. The browser
is duck-typed and optional: it is passed in by the caller and only
needs act(action, **kw) and screenshot(). This module never imports
the browser package; the real browser is injected later.

A worker with no browser still works for pure-computation
approaches and honestly records browser_available=False. A browser
approach with no browser reports the browser as unavailable and
returns no result, never a faked browser result.
"""

import json
from pathlib import Path


def _task_id_of(task):
    if isinstance(task, dict):
        return task.get("task_id") or task.get("id") or "task"
    if isinstance(task, str):
        return task
    return "task"


class Worker:
    def __init__(self, worker_id, reports_path=None):
        self.worker_id = worker_id
        self.reports_path = reports_path
        self.approach = None

    @property
    def copy_id(self):
        """Alias so arena-style callers can use copy_id."""
        return self.worker_id

    def to_record(self):
        return {"worker_id": self.worker_id, "copy_id": self.worker_id}

    def run(self, task, approach, browser=None):
        """Execute one approach and return the report dict.

        approach is an approach name (see copies.approaches) or a
        callable taking (task, browser=None) and returning
        (result, evidence). The report always carries the honest
        browser_available flag.
        """
        self.approach = approach
        task_id = _task_id_of(task)
        browser_available = browser is not None
        if callable(approach):
            try:
                result, evidence = approach(task, browser=browser)
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - reported
                result, evidence = None, [
                    f"approach callable raised: "
                    f"{type(exc).__name__}: {exc}"]
                status = "error"
        else:
            # Lazy import: avoids a top-level cycle with copies,
            # and keeps this module free of any browser import.
            try:
                from copies.approaches import run_approach
            except Exception as exc:  # noqa: BLE001 - reported
                result, evidence = None, [
                    f"approach registry unavailable: {exc}"]
                status = "error"
            else:
                outcome = run_approach(approach, task, browser=browser)
                status, result, evidence = (
                    outcome["status"], outcome["result"],
                    outcome["evidence"])
        report = {
            "task_id": task_id,
            "worker_id": self.worker_id,
            "copy_id": self.worker_id,
            "approach": approach if isinstance(approach, str) else getattr(
                approach, "__name__", "callable"),
            "result": result,
            "evidence": list(evidence),
            "status": status,
            "browser_available": browser_available,
        }
        if self.reports_path is not None:
            path = Path(self.reports_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(report, sort_keys=True) + "\n")
        return report
