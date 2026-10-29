"""Tests for agent/head.py, agent/worker.py, agent/loop.py.

Runnable with plain: python3 tests/test_agent.py
Stdlib only. Exit 0 on pass, non-zero on any failure.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.head import Head  # noqa: E402
from agent.loop import ConvergenceLoop  # noqa: E402
from agent.worker import Worker  # noqa: E402


class MockBrowser:
    def act(self, action, **kw):
        return {"ok": True, "action": action, "kw": kw}

    def screenshot(self):
        return "mock-screenshot"


TASK = {"task_id": "t1",
        "text": "The arena probe checks distinct paths. "
                "Each copy walks alone. "
                "The head judges on evidence.",
        "expression": "2+3*4", "numbers": [1, 2, 3, 4]}


def test_worker_pure_computation_without_browser():
    worker = Worker("worker-0001")
    report = worker.run(TASK, "calculator")
    assert report["task_id"] == "t1", report
    assert report["worker_id"] == "worker-0001", report
    assert report["approach"] == "calculator", report
    assert report["result"] == 14, report
    assert report["status"] == "ok", report
    assert report["evidence"], report
    assert report["browser_available"] is False, report


def test_worker_with_mock_browser():
    worker = Worker("worker-0002")
    report = worker.run(TASK, "browser_open", browser=MockBrowser())
    assert report["status"] == "ok", report
    assert report["browser_available"] is True, report
    assert report["result"] is not None, report
    report2 = worker.run(TASK, "browser_screenshot", browser=MockBrowser())
    assert report2["status"] == "ok", report2
    assert report2["result"] == "mock-screenshot", report2


def test_worker_browser_unavailable_is_honest():
    worker = Worker("worker-0003")
    report = worker.run(TASK, "browser_open", browser=None)
    assert report["status"] == "error", report
    assert report["result"] is None, report
    assert any("browser unavailable" in e for e in report["evidence"]), report


def test_worker_unknown_approach_is_error_not_fake():
    worker = Worker("worker-0004")
    report = worker.run(TASK, "not-a-real-approach")
    assert report["status"] == "error", report
    assert report["result"] is None, report


def test_workers_report_to_head_and_judge_one_winner():
    with tempfile.TemporaryDirectory() as tmp:
        reports_path = Path(tmp) / "reports.jsonl"
        head = Head(reports_path)
        workers = head.hire_workers(4)
        assert len(workers) == 4
        assert len({w.worker_id for w in workers}) == 4
        # Only 3 of 4 report; the 4th must appear in missing.
        for worker in workers[:3]:
            worker.run(TASK, "calculator")
        collected = head.collect_reports()
        assert len(collected) == 3, collected
        judgement = head.judge(collected)
        assert judgement["winner"] is not None, judgement
        assert len(judgement["comparison"]) == 3, judgement
        assert judgement["missing"] == [workers[3].worker_id], judgement


def test_head_judge_no_reports_is_none_with_all_missing():
    head = Head()
    head.hire_workers(2)
    judgement = head.judge([])
    assert judgement["winner"] is None, judgement
    assert len(judgement["missing"]) == 2, judgement


def test_loop_converges_on_agreeing_task():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "checkpoint.json"
        workers = [Worker(f"worker-{i:04d}") for i in range(1, 4)]
        loop = ConvergenceLoop(checkpoint)
        result = loop.run(TASK, workers, max_rounds=10,
                          approaches=["calculator"] * 3)
        assert result["status"] == "converged", result
        assert result["rounds"] == 1, result
        assert all(r == 14 for r in result["final_results"]), result
        assert checkpoint.exists(), "checkpoint missing after round"
        saved = json.loads(checkpoint.read_text())
        assert saved["rounds_done"] == 1, saved


def test_loop_not_converged_on_disagreeing_task():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "checkpoint.json"
        workers = [Worker(f"worker-{i:04d}") for i in range(1, 4)]
        loop = ConvergenceLoop(checkpoint)
        result = loop.run(
            TASK, workers, max_rounds=3,
            approaches=["calculator", "text_count", "shell_echo"])
        assert result["status"] == "not_converged", result
        assert result["rounds"] == 3, result
        # Real partial evidence, not a faked consensus.
        assert len(result["reports"]) == 3, result
        assert len(set(str(r) for r in result["final_results"])) > 1, result
        saved = json.loads(checkpoint.read_text())
        assert saved["status"] == "not_converged", saved
        assert saved["rounds_done"] == 3, saved


def test_loop_checkpoint_resume_from_running():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "checkpoint.json"
        # Simulate a crash mid-run: a running checkpoint with 1
        # round already done, in the loop's own format.
        checkpoint.write_text(json.dumps({
            "task_id": "t1", "status": "running", "rounds_done": 1,
            "history": [[{"worker_id": "worker-0001", "result": 14,
                          "status": "ok", "evidence": ["x"]}]],
            "last_results": [14], "last_reports": [],
        }))
        workers = [Worker(f"worker-{i:04d}") for i in range(1, 4)]
        loop = ConvergenceLoop(checkpoint)
        result = loop.run(
            TASK, workers, max_rounds=3,
            approaches=["calculator", "text_count", "shell_echo"])
        assert result["rounds"] == 3, result
        assert result["resumed"] is True, result
        assert len(result["history"]) == 3, result
        assert result["history"][0][0]["worker_id"] == "worker-0001", result


def test_loop_full_update_signal():
    def full_update_worker(task, round_no):
        return {"worker_id": "worker-0001", "task_id": "t1",
                "result": {"full_update": True},
                "evidence": ["full update signalled"],
                "status": "full_update"}

    with tempfile.TemporaryDirectory() as tmp:
        loop = ConvergenceLoop(Path(tmp) / "checkpoint.json")
        result = loop.run(TASK, [full_update_worker], max_rounds=10)
        assert result["status"] == "full_update", result
        assert result["rounds"] == 1, result


TESTS = [
    test_worker_pure_computation_without_browser,
    test_worker_with_mock_browser,
    test_worker_browser_unavailable_is_honest,
    test_worker_unknown_approach_is_error_not_fake,
    test_workers_report_to_head_and_judge_one_winner,
    test_head_judge_no_reports_is_none_with_all_missing,
    test_loop_converges_on_agreeing_task,
    test_loop_not_converged_on_disagreeing_task,
    test_loop_checkpoint_resume_from_running,
    test_loop_full_update_signal,
]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
