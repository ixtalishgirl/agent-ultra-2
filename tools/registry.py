"""JSON file-backed tool registry.

A registered tool is a pure-computation function stored as source
text plus metadata in one JSON file. Nothing here imports, executes,
or serves anything else.

Rules (enforced, not documented wishes):
  - register() refuses a name that is already registered.
  - register() refuses source that fails validation (see
    tools/pipeline.py for the full 5-stage validation; this module
    re-checks the structural minimum so a direct register() call
    cannot smuggle in banned constructs).
  - dispatch() of an unknown tool raises ToolNotFoundError. It never
    returns invented output.

Storage format (one JSON object, tool name -> record):
  {"name": {"source": "...", "description": "...",
            "arg_names": [...], "sha256": "...", "bytes": N}}

Stdlib only.
"""

import ast
import hashlib
import json
import keyword
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "registry.json"

_BANNED_CALLS = {"eval", "exec", "compile", "open", "__import__", "input"}


class ToolNotFoundError(KeyError):
    """Raised by dispatch() for a tool that is not registered."""


class ToolRegistrationError(ValueError):
    """Raised by register() for a collision or invalid tool."""


def _validate_source(name, source, arg_names):
    """Structural minimum. Returns an error string, or None if valid."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"source does not parse: {exc}"
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1 or funcs[0].name != name:
        return (f"source must define exactly one module-level function "
                f"named {name!r}")
    fn = funcs[0]
    declared = [a.arg for a in fn.args.args]
    if declared != list(arg_names):
        return (f"function arguments {declared} do not match declared "
                f"args {list(arg_names)}")
    if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        return "variadic / keyword-only arguments are not allowed"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "imports are not allowed in a tool"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BANNED_CALLS:
                return f"banned call: {node.func.id}()"
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"dunder attribute access is not allowed: {node.attr}"
        if isinstance(node, ast.Global):
            return "global statements are not allowed"
    return None


class Registry:
    """File-backed registry. All writes are whole-file rewrites."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolRegistrationError(
                f"registry file is unreadable: {self.path}: {exc}")
        if not isinstance(data, dict):
            raise ToolRegistrationError(
                f"registry file is not a JSON object: {self.path}")
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True),
                             encoding="utf-8")

    def register(self, name, description, source, arg_names):
        """Register one tool. Raises ToolRegistrationError on collision
        or invalid source; on failure nothing is written."""
        if not isinstance(name, str) or not name.isidentifier() \
                or keyword.iskeyword(name):
            raise ToolRegistrationError(f"invalid tool name: {name!r}")
        if not isinstance(description, str) or not description.strip():
            raise ToolRegistrationError(
                "description must be a non-empty string")
        data = self._load()
        if name in data:
            raise ToolRegistrationError(
                f"tool {name!r} is already registered (collision refused)")
        problem = _validate_source(name, source, arg_names)
        if problem:
            raise ToolRegistrationError(problem)
        encoded = source.encode("utf-8")
        data[name] = {
            "source": source,
            "description": description,
            "arg_names": list(arg_names),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }
        self._save(data)
        return {"ok": True, "tool": name, "sha256": data[name]["sha256"],
                "bytes": data[name]["bytes"]}

    def list(self):
        """name -> {description, arg_names, sha256, bytes} for every
        registered tool. Never includes source text in listings."""
        data = self._load()
        return {name: {"description": rec.get("description", ""),
                       "arg_names": rec.get("arg_names", []),
                       "sha256": rec.get("sha256", ""),
                       "bytes": rec.get("bytes", 0)}
                for name, rec in sorted(data.items())}

    def dispatch(self, name, args=None):
        """Call a registered tool with keyword arguments.

        Raises ToolNotFoundError for an unknown tool, TypeError for
        bad arguments, and propagates any exception the tool itself
        raises. It never returns invented output.
        """
        data = self._load()
        if name not in data:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        record = data[name]
        namespace = {}
        exec(compile(record["source"], f"<tool:{name}>", "exec"), namespace)
        fn = namespace[name]
        args = args or {}
        unknown = set(args) - set(record["arg_names"])
        if unknown:
            raise TypeError(
                f"unknown arguments for {name}: {sorted(unknown)}")
        return fn(**args)
