"""Health check: real checks, real values, no invented status.

health() returns:
  {"ok": bool,
   "checks": {"supervisor": bool, "memory": bool, "tools": bool,
              "disk": bool}}

Checks:
  - supervisor: if a Supervisor object is passed, its status()
    returns a dict; otherwise the supervisor.py file next to this
    module is readable.
  - memory: the memory JSONL file is absent (empty store is valid)
    or present with every non-blank line parsing as JSON.
  - tools: the registry JSON file is absent (empty registry is
    valid) or present and parses as a JSON object.
  - disk: shutil.disk_usage on the repo root reports free bytes > 0.

ok is True only if every check passes. Stdlib only.
"""

import json
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _jsonl_readable(path):
    path = Path(path)
    if not path.exists():
        return True
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    json.loads(line)
    except (OSError, ValueError):
        return False
    return True


def _json_object_readable(path):
    path = Path(path)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict)


def _supervisor_ok(supervisor):
    if supervisor is not None:
        try:
            return isinstance(supervisor.status(), dict)
        except Exception:
            return False
    return Path(__file__).resolve().parent.joinpath(
        "supervisor.py").is_file()


def _disk_ok(path=None):
    try:
        usage = shutil.disk_usage(str(path or _REPO_ROOT))
        return usage.free > 0
    except OSError:
        return False


def health(supervisor=None, registry_path=None, memory_path=None):
    """Run all checks and return {"ok": ..., "checks": {...}}."""
    if registry_path is None:
        registry_path = _REPO_ROOT / "tools" / "registry.json"
    if memory_path is None:
        memory_path = _REPO_ROOT / "memory" / "memory.jsonl"
    checks = {
        "supervisor": _supervisor_ok(supervisor),
        "memory": _jsonl_readable(memory_path),
        "tools": _json_object_readable(registry_path),
        "disk": _disk_ok(),
    }
    return {"ok": all(checks.values()), "checks": checks}
