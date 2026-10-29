"""Convergence loop for Agent Ultra 2.

Repeats rounds of worker/copy work until either:

  * consensus: every participant in a round reported status ok
    with the same non-None result, or
  * full update: a participant explicitly reports status
    "full_update" (or a result dict carrying full_update=True).

A checkpoint JSON is written after EVERY round, so a restart can
resume instead of starting over. The checkpoint format is:

  {"task_id", "status": "running" | terminal status,
   "rounds_done", "history": [per-round report lists],
   "last_results", "last_reports"}

If a checkpoint with status "running" and the same task_id exists
at the start, the loop resumes after its rounds_done and keeps its
history. If the checkpoint is already terminal, it is returned
unchanged. If max_rounds is reached without consensus or a full
update, the loop stops honestly with status "not_converged" and
the real partial evidence (last round reports and distinct
results) -- never a faked consensus.
"""

import json
from pathlib import Path


def _task_id_of(task):
    if isinstance(task, dict):
        return task.get("task_id") or task.get("id") or "task"
    if isinstance(task, str):
        return task
    return "task"


def _results_equal(results):
    if not results:
        return False
    first = results[0]
    if first is None:
        return False
    return all(r == first for r in results)


def _run_participant(participant, task, approach, browser, round_no):
    """Run one participant and normalise to a report dict.

    participant may be: a Worker-like object with run(task,
    approach, browser=...); a callable returning a report dict or
    a plain result; or a dict/tuple pairing a worker with an
    approach. Unknown shapes raise, and the caller records the
    failure as error evidence for that participant.
    """
    if isinstance(participant, dict) and "worker" in participant:
        worker = participant["worker"]
        approach = participant.get("approach", approach)
        return worker.run(task, approach, browser=browser)
    if isinstance(participant, (list, tuple)) and len(participant) == 2:
        worker, approach = participant
        if hasattr(worker, "run"):
            return worker.run(task, approach, browser=browser)
    if hasattr(participant, "run"):
        use_approach = approach
        if use_approach is None:
            use_approach = getattr(participant, "approach", None)
        if use_approach is None:
            raise ValueError(
                "participant needs an approach: pass approaches= "
                "or set worker.approach")
        return participant.run(task, use_approach, browser=browser)
    if callable(participant):
        try:
            outcome = participant(task, round_no)
        except TypeError:
            outcome = participant(task)
        if isinstance(outcome, dict) and "result" in outcome:
            report = dict(outcome)
            report.setdefault("worker_id",
                              getattr(participant, "__name__", "callable"))
            report.setdefault("evidence", [str(outcome.get("result"))])
            report.setdefault("status", "ok")
            return report
        return {"worker_id": getattr(participant, "__name__", "callable"),
                "task_id": _task_id_of(task), "result": outcome,
                "evidence": [str(outcome)], "status": "ok"}
    raise ValueError(f"unsupported participant type: {type(participant)!r}")


class ConvergenceLoop:
    def __init__(self, checkpoint_path=None):
        self.checkpoint_path = checkpoint_path

    def _write_checkpoint(self, path, data):
        if path is None:
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, default=str),
                       encoding="utf-8")
        tmp.replace(path)
        return str(path)

    def _read_checkpoint(self, path):
        if path is None:
            return None
        path = Path(path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def run(self, task, copies_or_workers, max_rounds=10,
            approaches=None, checkpoint_path=None, browser=None):
        if not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds must be an integer >= 1")
        participants = list(copies_or_workers) if copies_or_workers else []
        if not participants:
            raise ValueError("loop needs at least one participant")
        task_id = _task_id_of(task)
        path = checkpoint_path or self.checkpoint_path

        history = []
        start_round = 0
        saved = self._read_checkpoint(path)
        if saved is not None and saved.get("task_id") == task_id:
            if saved.get("status") in ("converged", "full_update",
                                       "not_converged"):
                return {
                    "task_id": task_id,
                    "status": saved["status"],
                    "rounds": saved.get("rounds_done", 0),
                    "rounds_done": saved.get("rounds_done", 0),
                    "final_results": saved.get("last_results", []),
                    "results": saved.get("last_results", []),
                    "reports": saved.get("last_reports", []),
                    "evidence": saved.get("last_reports", []),
                    "history": saved.get("history", []),
                    "checkpoint": str(path) if path else None,
                    "resumed": True,
                }
            if saved.get("status") == "running":
                history = list(saved.get("history", []))
                start_round = int(saved.get("rounds_done", 0))

        status = "not_converged"
        last_reports = []
        last_results = []
        rounds_done = start_round
        for round_no in range(start_round, max_rounds):
            round_reports = []
            for index, participant in enumerate(participants):
                approach = None
                if approaches is not None:
                    approach = approaches[index]
                try:
                    report = _run_participant(
                        participant, task, approach, browser, round_no)
                except Exception as exc:  # noqa: BLE001 - reported
                    report = {
                        "worker_id": f"participant-{index:04d}",
                        "task_id": task_id,
                        "result": None,
                        "evidence": [f"participant raised: "
                                     f"{type(exc).__name__}: {exc}"],
                        "status": "error",
                    }
                round_reports.append(report)
            history.append(round_reports)
            last_reports = round_reports
            last_results = [r.get("result") for r in round_reports]
            rounds_done = round_no + 1
            full_update = any(
                r.get("status") == "full_update"
                or (isinstance(r.get("result"), dict)
                    and r["result"].get("full_update") is True)
                for r in round_reports)
            consensus = (
                all(r.get("status") == "ok" for r in round_reports)
                and _results_equal(last_results))
            if full_update:
                status = "full_update"
            elif consensus:
                status = "converged"
            else:
                status = "running" if rounds_done < max_rounds \
                    else "not_converged"
            self._write_checkpoint(path, {
                "task_id": task_id, "status": status,
                "rounds_done": rounds_done, "history": history,
                "last_results": last_results,
                "last_reports": last_reports,
            })
            if status in ("converged", "full_update"):
                break
        if status == "running":
            status = "not_converged"
            self._write_checkpoint(path, {
                "task_id": task_id, "status": status,
                "rounds_done": rounds_done, "history": history,
                "last_results": last_results,
                "last_reports": last_reports,
            })
        return {
            "task_id": task_id,
            "status": status,
            "rounds": rounds_done,
            "rounds_done": rounds_done,
            "final_results": last_results,
            "results": last_results,
            "distinct_results": sorted(
                {json.dumps(r, sort_keys=True, default=str)
                 for r in last_results}),
            "reports": last_reports,
            "evidence": last_reports,
            "history": history,
            "checkpoint": str(path) if path else None,
            "resumed": start_round > 0,
        }
