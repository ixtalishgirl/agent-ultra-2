"""Deterministic shell tool for Agent Ultra 2. Stdlib only.

run_shell() runs one bash command in the repo's pinned work
directory and returns its real result. There is no shell
metacharacter filtering: this is the container's own shell, and
the result always reports what actually happened (exit code,
truncated output, or a real timeout kill) — never invented
output.

  - cwd is pinned to <repo-root>/work/shell (created on demand);
    the command cannot be steered elsewhere through this tool.
  - timeout_s is clamped to 1..300 seconds; on timeout the
    process is killed and the report says so.
  - stdout and stderr are each capped at 32 KiB; a "truncated"
    flag says whether anything was cut.

Timeouts and non-zero exits are real outcomes, reported
honestly, not retried.
"""

import subprocess
import time
from pathlib import Path

MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 300
DEFAULT_TIMEOUT_S = 30
CAP_BYTES = 32 * 1024


def work_dir():
    """The pinned working directory; created on first use."""
    path = Path(__file__).resolve().parent.parent / "work" / "shell"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clamp_timeout(timeout_s):
    if isinstance(timeout_s, bool):
        return DEFAULT_TIMEOUT_S
    try:
        value = int(timeout_s)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, value))


def _cap(raw):
    """Cap raw bytes at CAP_BYTES; return (text, truncated)."""
    truncated = len(raw) > CAP_BYTES
    return raw[:CAP_BYTES].decode("utf-8", "replace"), truncated


def run_shell(command, timeout_s=DEFAULT_TIMEOUT_S):
    """Run one bash command; return its real result as a dict::

        {"ok", "exit_code", "stdout", "stderr", "truncated",
         "timed_out", "ms"}  (+ "error" only on invalid input)

    ``ok`` is True only when the command ran to completion with
    exit code 0. A timeout kills the process and reports
    ``timed_out`` True with ``exit_code`` None.
    """
    start = time.monotonic()
    if not isinstance(command, str) or not command.strip():
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "truncated": False, "timed_out": False,
                "ms": 0.0, "error": "command must be a non-empty string"}
    timeout = _clamp_timeout(timeout_s)
    timed_out = False
    exit_code = None
    raw_out, raw_err = b"", b""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=str(work_dir()),
            capture_output=True,
            timeout=timeout,
        )
        exit_code = completed.returncode
        raw_out = completed.stdout or b""
        raw_err = completed.stderr or b""
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the process on timeout.
        timed_out = True
        raw_out = exc.stdout or b""
        raw_err = exc.stderr or b""
    except OSError as exc:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "truncated": False, "timed_out": False,
                "ms": round((time.monotonic() - start) * 1000, 1),
                "error": f"shell failed to start: {exc}"}
    stdout, out_cut = _cap(raw_out)
    stderr, err_cut = _cap(raw_err)
    return {
        "ok": (not timed_out) and exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_cut or err_cut,
        "timed_out": timed_out,
        "ms": round((time.monotonic() - start) * 1000, 1),
    }
