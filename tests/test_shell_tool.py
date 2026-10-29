"""Tests for tools/shell_tool.py.

Run: python3 tests/test_shell_tool.py   (stdlib only, exit 0 on pass)

Every check runs a real bash command in the pinned work directory;
no result here is invented.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import shell_tool  # noqa: E402
from tools.shell_tool import run_shell, work_dir  # noqa: E402

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"pass: {label}")


def main():
    # 1. A basic echo runs in the pinned work directory.
    r = run_shell("echo hello")
    check("echo ok", r["ok"] is True)
    check("echo exit 0", r["exit_code"] == 0)
    check("echo stdout", r["stdout"] == "hello\n")
    check("echo stderr empty", r["stderr"] == "")
    check("echo not truncated", r["truncated"] is False)
    check("echo not timed out", r["timed_out"] is False)
    check("echo ms measured", isinstance(r["ms"], float) and r["ms"] >= 0)
    check("echo keys",
          set(r) >= {"ok", "exit_code", "stdout", "stderr", "truncated",
                     "timed_out", "ms"})

    # 2. python3 is reachable from the shell.
    r = run_shell("python3 --version", timeout_s=10)
    check("python3 exit 0", r["ok"] is True and r["exit_code"] == 0)
    check("python3 prints a version",
          "Python" in r["stdout"] or "Python" in r["stderr"])

    # 3. A hanging command is killed on timeout and reported.
    r = run_shell("sleep 5", timeout_s=1)
    check("sleep timed out", r["timed_out"] is True)
    check("sleep not ok", r["ok"] is False)
    check("sleep exit_code none", r["exit_code"] is None)

    # 4. Commands run in the pinned work directory, not the repo root.
    wd = work_dir()
    check("work dir exists", wd.is_dir())
    r = run_shell("pwd")
    check("pwd is the work dir",
          Path(r["stdout"].strip()).resolve() == wd.resolve())
    check("work dir is repo-root/work/shell",
          wd.resolve() == (REPO_ROOT / "work" / "shell").resolve())

    # 5. Large output is capped and flagged.
    r = run_shell("yes | head -c 200000", timeout_s=10)
    check("big output truncated", r["truncated"] is True)
    check("big output capped at 32KB",
          len(r["stdout"].encode("utf-8")) <= 32 * 1024)
    check("big output still ran", r["exit_code"] == 0)

    # 6. Non-zero exits are real outcomes, not fabrications.
    r = run_shell("exit 7")
    check("exit 7 not ok", r["ok"] is False)
    check("exit 7 code reported", r["exit_code"] == 7)
    check("exit 7 not timed out", r["timed_out"] is False)

    # 7. Timeout clamps to 1..300.
    check("timeout clamped low", shell_tool._clamp_timeout(0) == 1)
    check("timeout clamped high", shell_tool._clamp_timeout(9999) == 300)
    check("timeout normal kept", shell_tool._clamp_timeout(45) == 45)

    # 8. Invalid input is an honest error, not a crash.
    r = run_shell("")
    check("empty command refused", r["ok"] is False)
    check("empty command has error", "error" in r)
    r = run_shell(None)
    check("none command refused", r["ok"] is False)

    # 9. Stderr is captured separately from stdout.
    r = run_shell("echo out; echo err >&2")
    check("stdout separate", r["stdout"] == "out\n")
    check("stderr separate", r["stderr"] == "err\n")

    print(f"ALL PASS: {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
