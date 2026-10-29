"""Head for Agent Ultra 2.

The head hires workers, collects their reports and judges them.
Judging is mechanical only: it scores report evidence (status ok
plus evidence present). It never invents evidence and never drops
a worker or copy silently: judge() always returns a `missing` list
for expected workers/copies that never reported.

Exactly one winner is returned (the top-ranked report), or None
when no report was received.
"""

import json
from pathlib import Path

from .worker import Worker


def mechanical_score(report):
    score = 10 if report.get("status") == "ok" else 0
    score += min(len(report.get("evidence") or []), 5)
    return score


def _report_id(report):
    return report.get("worker_id") or report.get("copy_id")


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


class Head:
    def __init__(self, reports_path=None, log_path=None):
        self.reports_path = reports_path or log_path
        self.workers = []
        self._reports = []
        self._expected = []

    def hire_workers(self, n, prefix="worker"):
        """Create n worker records. Returns the Worker objects;
        their ids are also kept as the expected-report set."""
        if not isinstance(n, int) or n < 1:
            raise ValueError("n must be an integer >= 1")
        workers = []
        for i in range(1, n + 1):
            worker_id = f"{prefix}-{i:04d}"
            worker = Worker(worker_id, reports_path=self.reports_path)
            workers.append(worker)
        self.workers.extend(workers)
        self._expected.extend(w.worker_id for w in workers)
        return workers

    def expect(self, ids):
        """Set the expected worker/copy ids for missing tracking."""
        self._expected = list(ids)
        return self._expected

    def receive_report(self, report):
        """Record a report handed to the head in memory."""
        self._reports.append(report)
        return report

    def collect_reports(self, path=None, reports=None):
        """Read reports: an explicit list wins, else the given or
        configured JSONL file, merged with in-memory reports.
        Merging de-duplicates on (id, task_id, approach) so a
        report delivered both ways is counted once."""
        if reports is not None:
            return list(reports)
        merged = []
        seen = set()
        source = path or self.reports_path
        if source is not None:
            for report in _read_jsonl(source):
                key = (_report_id(report), report.get("task_id"),
                       report.get("approach"))
                if key not in seen:
                    seen.add(key)
                    merged.append(report)
        for report in self._reports:
            key = (_report_id(report), report.get("task_id"),
                   report.get("approach"))
            if key not in seen:
                seen.add(key)
                merged.append(report)
        return merged

    def compare(self, reports):
        """Examine EVERY report: one comparison row per report,
        ranked by mechanical evidence score, ties broken by id so
        the order is deterministic. No report is skipped."""
        ranked = sorted(
            reports,
            key=lambda r: (-mechanical_score(r), str(_report_id(r))))
        return [{
            "worker_id": _report_id(r),
            "copy_id": _report_id(r),
            "status": r.get("status"),
            "approach": r.get("approach"),
            "score": mechanical_score(r),
            "evidence": len(r.get("evidence") or []),
        } for r in ranked]

    def judge(self, reports=None, expected_ids=None):
        """Judge collected reports. Returns exactly one winner
        (or None) plus the missing list and the full comparison.
        Judging uses report evidence only."""
        if reports is None:
            reports = self.collect_reports()
        else:
            reports = list(reports)
        comparison = self.compare(reports)
        winner = None
        if comparison:
            top_id = comparison[0]["worker_id"]
            for report in reports:
                if _report_id(report) == top_id:
                    winner = report
                    break
        expected = list(expected_ids) if expected_ids is not None \
            else list(self._expected)
        reported = {_report_id(r) for r in reports}
        missing = [eid for eid in expected if eid not in reported]
        # Elimination rounds (top half each round, min 1), kept for
        # compatibility with the arena bracket shape. The winner
        # above is the top-ranked report, which is also the bracket
        # survivor, since the ranked list is kept in order.
        rounds = []
        current = [c["worker_id"] for c in comparison]
        while len(current) > 1:
            keep = max(1, len(current) // 2)
            current = current[:keep]
            rounds.append(list(current))
        return {"winner": winner, "missing": missing,
                "comparison": comparison, "rounds": rounds}
