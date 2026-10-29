"""Arena of copies for Agent Ultra 2.

Flow, in order:

  1. ORDER BROADCAST -- the head writes one order record per copy
     to orders.jsonl, each carrying the task and that copy's
     assigned approach.
  2. DISTINCT PER-WAVE APPROACH -- every copy in one wave gets a
     DIFFERENT approach. Approaches may repeat across waves, never
     within one wave. If a wave would have to reuse an approach
     (max_concurrent larger than the distinct pool) the run is
     refused with ValueError, as is a custom approach list with
     duplicates.
  3. WAVES -- copies run in sequential waves of at most
     max_concurrent on this host. 100 simultaneous copies on one
     host are NOT claimed: waves are the mechanism, and true
     simultaneity would need a fleet.
  4. REPORTS -- each copy reports to reports.jsonl.
  5. HEAD COMPARISON + JUDGED WINNER -- only after all waves,
     compare() examines EVERY report and judge() yields exactly
     one winner (or None when nothing reported) plus the missing
     list. Copies that never reported are listed, never silently
     dropped.

One run_arena call owns its base dir's orders.jsonl and
reports.jsonl; both are reset at the start so stale reports from
an older run can never count as this run's answers. This module
never imports the browser package: a browser object (or a per-copy
browser factory) is passed in and handed to workers duck-typed.
"""

import json
import math
import time
from pathlib import Path

from agent.head import Head
from agent.worker import Worker
from copies.approaches import APPROACH_NAMES


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def _append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def compare(reports):
    """Head comparison over EVERY report (delegates to Head)."""
    return Head().compare(list(reports))


def judge(reports, expected_ids=None):
    """Head judgement: exactly one winner or None, plus missing."""
    return Head().judge(list(reports), expected_ids=expected_ids)


def _normalise_approach(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    if callable(entry):
        return getattr(entry, "__name__", "callable")
    raise ValueError(f"unsupported approach entry: {entry!r}")


def run_arena(task_id, base_dir, copies=100, max_concurrent=10,
              approaches=None, task=None, browser=None,
              browser_factory=None, work_fn=None, prefix="copy"):
    """Run one task across N copies under head management.

    approaches: optional custom list of approach names. Duplicates
    are refused. When omitted, the built-in pool is used and
    approaches repeat across waves only.
    work_fn: optional override work_fn(copy, task_id, log_path);
    when it returns a report dict that dict is recorded, when it
    returns None it is assumed to have written to log_path itself.
    """
    if not isinstance(copies, int) or copies < 1:
        raise ValueError("copies must be an integer >= 1")
    if not isinstance(max_concurrent, int) or max_concurrent < 1:
        raise ValueError("max_concurrent must be an integer >= 1")
    if approaches is not None:
        pool = [_normalise_approach(a) for a in approaches]
        if len(set(pool)) != len(pool):
            raise ValueError(
                "approaches must be distinct -- duplicates refused")
    else:
        pool = list(APPROACH_NAMES)
    if max_concurrent > len(pool):
        raise ValueError(
            f"a wave of {max_concurrent} would reuse an approach: "
            f"only {len(pool)} distinct approaches available; "
            f"lower max_concurrent or add approaches")
    if isinstance(task, dict):
        task_payload = dict(task)
        task_payload.setdefault("task_id", task_id)
    elif task is None:
        task_payload = {
            "task_id": task_id,
            "text": "The arena probe checks distinct paths. "
                    "Each copy walks alone. "
                    "The head judges on evidence.",
            "expression": "2+3*4",
            "numbers": [1, 2, 3, 4],
        }
    else:
        task_payload = {"task_id": task_id, "text": str(task)}

    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    orders_path = base / "orders.jsonl"
    reports_path = base / "reports.jsonl"
    _write_jsonl(orders_path, [])
    _write_jsonl(reports_path, [])

    # Per-copy approach: consecutive indices modulo the pool size
    # give distinct approaches within any wave of size <= pool
    # size, and reuse across waves only.
    assigned = [pool[i % len(pool)] for i in range(copies)]
    all_copies = []
    assignments = []
    for index, approach in enumerate(assigned):
        copy_id = f"{prefix}-{index + 1:04d}"
        worker = Worker(copy_id)
        worker.approach = approach
        all_copies.append(worker)
        order = {
            "task_id": task_id,
            "copy_id": copy_id,
            "approach": approach,
            "order": task_payload,
            "ts": time.time(),
        }
        _append_jsonl(orders_path, order)
        assignments.append({"copy_id": copy_id, "approach": approach})

    waves = math.ceil(copies / max_concurrent)
    for wave_start in range(0, copies, max_concurrent):
        for worker in all_copies[wave_start:wave_start + max_concurrent]:
            copy_browser = None
            if browser_factory is not None:
                copy_browser = browser_factory(worker.copy_id)
            elif browser is not None:
                copy_browser = browser
            if work_fn is not None:
                outcome = work_fn(worker, task_id, reports_path)
                if isinstance(outcome, dict):
                    record = dict(outcome)
                    record.setdefault("copy_id", worker.copy_id)
                    record.setdefault("task_id", task_id)
                    _append_jsonl(reports_path, record)
            else:
                report = worker.run(task_payload, worker.approach,
                                    browser=copy_browser)
                _append_jsonl(reports_path, report)

    reports = _read_jsonl(reports_path)
    reported = {r.get("copy_id") or r.get("worker_id") for r in reports}
    missing = [c.copy_id for c in all_copies if c.copy_id not in reported]
    head = Head(reports_path)
    head.expect([c.copy_id for c in all_copies])
    judgement = head.judge(reports, expected_ids=[c.copy_id for c in all_copies])
    return {
        "task_id": task_id,
        "copies": copies,
        "max_concurrent": max_concurrent,
        "waves": waves,
        "wave_mode": "sequential batches on this host; "
                     "100 simultaneous copies are NOT claimed",
        "assignments": assignments,
        "orders_file": str(orders_path),
        "reports_file": str(reports_path),
        "reports": len(reports),
        "missing": missing,
        "rounds": judgement["rounds"],
        "comparison": judgement["comparison"],
        "winner": judgement["winner"],
        "status": (
            f"{copies} copies ran in {waves} wave(s) of at most "
            f"{max_concurrent}, each wave on distinct approaches; "
            f"{len(reports)} reported, {len(missing)} missing; "
            f"100 simultaneous copies are NOT claimed -- waves "
            f"are the mechanism."
        ),
    }
