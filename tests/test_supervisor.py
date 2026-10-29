"""Tests for server/supervisor.py + server/health.py.

Run: python3 tests/test_supervisor.py   (stdlib only, exit 0 on pass)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from server.supervisor import Supervisor  # noqa: E402
from server.health import health  # noqa: E402

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"pass: {label}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. A succeeding command runs once and checkpoints "completed".
        ok_path = tmp / "ok-checkpoint.json"
        sup = Supervisor([sys.executable, "-c", "raise SystemExit(0)"],
                         checkpoint_path=ok_path, max_restarts=5,
                         backoff_seconds=0)
        result = sup.run()
        check("succeeding command completes", result == "completed")
        check("succeeding command runs once", sup.attempts == 1)
        check("succeeding command restarts zero times",
              sup.restarts == 0)
        record = json.loads(ok_path.read_text())
        check("success checkpoint says completed",
              record["status"] == "completed"
              and record["exit_code"] == 0)

        # 2. A crashing command restarts up to max_restarts, then stops.
        crash_path = tmp / "crash-checkpoint.json"
        sup2 = Supervisor([sys.executable, "-c", "raise SystemExit(3)"],
                          checkpoint_path=crash_path, max_restarts=2,
                          backoff_seconds=0)
        result2 = sup2.run()
        check("crashing command exhausts restarts",
              result2 == "exhausted")
        check("crashing command attempted 1 + max_restarts times",
              sup2.attempts == 3)
        check("crashing command restarted max_restarts times",
              sup2.restarts == 2)
        record2 = json.loads(crash_path.read_text())
        check("crash checkpoint says exhausted with exit code 3",
              record2["status"] == "exhausted"
              and record2["exit_code"] == 3
              and record2["attempt"] == 3)
        st = sup2.status()
        check("status() reports real observed state",
              st["attempts"] == 3 and st["restarts"] == 2
              and st["last_exit_code"] == 3
              and st["checkpoint_exists"] is True)

        # 3. health() returns real values for all four checks.
        h = health(supervisor=sup2,
                   registry_path=tmp / "no-registry.json",
                   memory_path=tmp / "no-memory.jsonl")
        check("health has ok + four checks",
              set(h.keys()) == {"ok", "checks"}
              and set(h["checks"].keys()) == {"supervisor", "memory",
                                              "tools", "disk"})
        check("health is ok on a clean temp dir", h["ok"] is True)
        check("health disk check is really true",
              h["checks"]["disk"] is True)

        # 4. health() goes not-ok on a corrupt memory file (real check,
        # not a canned True).
        bad_memory = tmp / "bad-memory.jsonl"
        bad_memory.write_text("{not json\n")
        h_bad = health(supervisor=sup2,
                       registry_path=tmp / "no-registry.json",
                       memory_path=bad_memory)
        check("health detects corrupt memory file",
              h_bad["ok"] is False
              and h_bad["checks"]["memory"] is False)

    print(f"ALL PASS: {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
