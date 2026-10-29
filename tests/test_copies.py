"""Tests for copies/arena.py and copies/approaches.py.

Runnable with plain: python3 tests/test_copies.py
Stdlib only. Exit 0 on pass, non-zero on any failure.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copies.approaches import APPROACH_NAMES, APPROACHES, run_approach  # noqa: E402
from copies.arena import run_arena  # noqa: E402


class MockBrowser:
    def act(self, action, **kw):
        return {"ok": True, "action": action, "kw": kw}

    def screenshot(self):
        return "mock-screenshot"


TASK = {"task_id": "arena-task",
        "text": "The arena probe checks distinct paths. "
                "Each copy walks alone. "
                "The head judges on evidence.",
        "expression": "2+3*4", "numbers": [1, 2, 3, 4]}


def _read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _wave_slices(assignments, max_concurrent):
    return [assignments[i:i + max_concurrent]
            for i in range(0, len(assignments), max_concurrent)]


def test_approach_pool_real_and_distinct():
    assert len(APPROACH_NAMES) >= 12, APPROACH_NAMES
    assert len(set(APPROACH_NAMES)) == len(APPROACH_NAMES)
    assert set(APPROACH_NAMES) == set(APPROACHES.keys())
    for name in APPROACH_NAMES:
        outcome = run_approach(name, TASK, browser=MockBrowser())
        assert outcome["status"] == "ok", (name, outcome)
        assert outcome["evidence"], (name, outcome)
    # Without a browser, browser approaches honestly fail, while
    # pure-computation approaches still succeed.
    assert run_approach("browser_open", TASK)["status"] == "error"
    assert run_approach("calculator", TASK)["result"] == 14


def test_arena_12_copies_waves_of_3():
    with tempfile.TemporaryDirectory() as tmp:
        result = run_arena("arena-task", tmp, copies=12,
                           max_concurrent=3, task=TASK,
                           browser=MockBrowser())
        assert result["copies"] == 12, result
        assert result["waves"] == 4, result
        assert result["reports"] == 12, result
        assert result["missing"] == [], result
        assert result["winner"] is not None, result
        assert len(result["comparison"]) == 12, result
        for wave in _wave_slices(result["assignments"], 3):
            approaches = [a["approach"] for a in wave]
            assert len(set(approaches)) == len(approaches), wave
        orders = _read_jsonl(result["orders_file"])
        assert len(orders) == 12, orders
        assert all(o["task_id"] == "arena-task" for o in orders)
        assert all(o["approach"] for o in orders)
        reports = _read_jsonl(result["reports_file"])
        assert len(reports) == 12, reports
        assert {r["copy_id"] for r in reports} == \
            {a["copy_id"] for a in result["assignments"]}


def test_arena_duplicate_custom_approaches_refused():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_arena("t", tmp, copies=2, max_concurrent=2,
                      approaches=["calculator", "calculator"])
        except ValueError:
            return
        raise AssertionError("duplicate approaches were not refused")


def test_arena_wave_reuse_refused():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_arena("t", tmp, copies=3, max_concurrent=5,
                      approaches=["calculator", "text_count"])
        except ValueError:
            return
        raise AssertionError("wave reuse was not refused")


def test_arena_reuse_across_waves_only():
    with tempfile.TemporaryDirectory() as tmp:
        result = run_arena("arena-task", tmp, copies=20,
                           max_concurrent=3, task=TASK,
                           browser=MockBrowser())
        assert result["waves"] == 7, result
        assert result["reports"] == 20, result
        assert result["missing"] == [], result
        for wave in _wave_slices(result["assignments"], 3):
            approaches = [a["approach"] for a in wave]
            assert len(set(approaches)) == len(approaches), wave
        all_approaches = [a["approach"] for a in result["assignments"]]
        # 20 copies over a 16-approach pool in waves of 3: some
        # approach must repeat across waves (pool is finite), but
        # never inside one wave (checked above).
        assert len(set(all_approaches)) < len(all_approaches)


def test_arena_missing_copies_listed_not_dropped():
    def only_first_reports(copy, task_id, log_path):
        if copy.copy_id.endswith("0001"):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "copy_id": copy.copy_id, "task_id": task_id,
                    "status": "ok", "approach": copy.approach,
                    "result": 14, "evidence": ["only reporter"],
                }, sort_keys=True) + "\n")
        return None

    with tempfile.TemporaryDirectory() as tmp:
        result = run_arena("arena-task", tmp, copies=4,
                           max_concurrent=2, task=TASK,
                           work_fn=only_first_reports)
        assert result["reports"] == 1, result
        assert len(result["missing"]) == 3, result
        assert result["winner"] is not None, result
        assert result["winner"]["copy_id"].endswith("0001"), result


def test_arena_default_100_copies_in_waves():
    # Default shape only: 100 copies, waves of 10, no claim of
    # simultaneity. Pure-computation approaches keep it fast;
    # browser approaches report honestly without a browser.
    with tempfile.TemporaryDirectory() as tmp:
        result = run_arena("arena-task", tmp, copies=100,
                           max_concurrent=10, task=TASK)
        assert result["waves"] == 10, result
        assert result["reports"] == 100, result
        assert result["missing"] == [], result
        assert result["winner"] is not None, result
        for wave in _wave_slices(result["assignments"], 10):
            approaches = [a["approach"] for a in wave]
            assert len(set(approaches)) == len(approaches), wave


TESTS = [
    test_approach_pool_real_and_distinct,
    test_arena_12_copies_waves_of_3,
    test_arena_duplicate_custom_approaches_refused,
    test_arena_wave_reuse_refused,
    test_arena_reuse_across_waves_only,
    test_arena_missing_copies_listed_not_dropped,
    test_arena_default_100_copies_in_waves,
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
