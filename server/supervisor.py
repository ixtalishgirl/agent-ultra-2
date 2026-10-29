"""Supervisor: run a command as a subprocess, restart it on crash,
and checkpoint after every supervised step so work can resume.

  - A "crash" is a non-zero exit (or a failure to start, which is
    reported as exit code None and also counts toward restarts).
  - A zero exit means the step completed: no restart.
  - Restarts stop after max_restarts (default 5). Backoff between
    restarts grows geometrically: backoff_seconds * 2**attempt,
    capped at 60 seconds.
  - After every attempt a checkpoint JSON file is written:
      {"command": [...], "attempt": N, "exit_code": int|None,
       "status": "completed"|"crashed"|"failed"|"exhausted",
       "restarts": N, "updated_utc": "ISO-8601"}
    "failed" means the command could not start at all; "exhausted"
    means the last allowed restart also crashed.

status() returns a dict describing the last run (or the not-started
state). It never claims a state the supervisor did not observe.

Stdlib only. This module does not import browser/, agent/, or
copies/.
"""

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CHECKPOINT_PATH = Path(__file__).resolve().parent / "checkpoint.json"


class Supervisor:
    def __init__(self, command, checkpoint_path=None, max_restarts=5,
                 backoff_seconds=1.0):
        if not isinstance(command, (list, tuple)) or not command \
                or not all(isinstance(c, str) for c in command):
            raise TypeError(
                "command must be a non-empty list of strings")
        if max_restarts < 0:
            raise ValueError("max_restarts must be >= 0")
        self.command = list(command)
        self.checkpoint_path = Path(checkpoint_path) \
            if checkpoint_path is not None else DEFAULT_CHECKPOINT_PATH
        self.max_restarts = max_restarts
        self.backoff_seconds = float(backoff_seconds)
        self.attempts = 0
        self.restarts = 0
        self.last_exit_code = None
        self.last_status = "not_started"
        self.last_checkpoint = None

    def _write_checkpoint(self, status, exit_code):
        record = {
            "command": self.command,
            "attempt": self.attempts,
            "exit_code": exit_code,
            "status": status,
            "restarts": self.restarts,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(json.dumps(record, indent=2),
                                        encoding="utf-8")
        self.last_checkpoint = record
        return record

    def run(self):
        """Supervise the command to completion or restart exhaustion.

        Returns the final status string: "completed" or "exhausted"
        (or "failed" if it could never start and no restart helped —
        reported as "exhausted" once restarts run out; a single
        max_restarts=0 start failure reports "failed").
        """
        attempt = 0
        while True:
            self.attempts = attempt + 1
            try:
                proc = subprocess.run(self.command, capture_output=True,
                                      text=True, timeout=None)
                exit_code = proc.returncode
                start_failed = False
            except OSError as exc:
                exit_code = None
                start_failed = True
                self._start_error = f"{type(exc).__name__}: {exc}"
            if not start_failed and exit_code == 0:
                self.last_exit_code = 0
                self.last_status = "completed"
                self._write_checkpoint("completed", 0)
                return "completed"
            # Crash (or start failure).
            self.last_exit_code = exit_code
            if self.restarts >= self.max_restarts:
                status = "failed" if (start_failed
                                      and self.max_restarts == 0) \
                    else "exhausted"
                self.last_status = status
                self._write_checkpoint(status, exit_code)
                return status
            self.restarts += 1
            self.last_status = "crashed"
            self._write_checkpoint("crashed", exit_code)
            delay = min(self.backoff_seconds * (2 ** (self.restarts - 1)),
                        60.0)
            if delay > 0:
                time.sleep(delay)
            attempt += 1

    def status(self):
        """Current supervisor state as a plain dict."""
        return {
            "command": self.command,
            "running": False,  # run() is synchronous; nothing runs now
            "attempts": self.attempts,
            "restarts": self.restarts,
            "max_restarts": self.max_restarts,
            "last_exit_code": self.last_exit_code,
            "last_status": self.last_status,
            "checkpoint_path": str(self.checkpoint_path),
            "checkpoint_exists": self.checkpoint_path.exists(),
        }
