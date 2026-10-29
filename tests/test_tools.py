"""Tests for tools/pipeline.py + tools/registry.py.

Run: python3 tests/test_tools.py   (stdlib only, exit 0 on pass)
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.pipeline import create_tool  # noqa: E402
from tools.registry import Registry, ToolNotFoundError  # noqa: E402

ADD_SOURCE = "def add(a, b):\n    return a + b\n"
BAD_SOURCE = "def boom(a):\n    return a / 0\n"
IMPORT_SOURCE = "def sneaky(a):\n    import os\n    return a\n"

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"pass: {label}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        registry = Registry(Path(tmp) / "registry.json")

        # 1. A good tool registers and dispatches real output.
        report = create_tool(
            "add", "adds two numbers", ADD_SOURCE, ["a", "b"],
            [{"args": {"a": 2, "b": 3}, "expected": 5},
             {"args": {"a": -1, "b": 1}, "expected": 0}],
            registry=registry)
        check("good tool reports ok", report["ok"] is True)
        check("good tool stages are all five",
              report["stages"] == ["intake", "validated_call", "execute",
                                   "evidence", "report"])
        check("good tool is listed", "add" in registry.list())
        check("dispatch returns real output",
              registry.dispatch("add", {"a": 40, "b": 2}) == 42)

        # 2. A name collision is refused and the original survives.
        clash = create_tool(
            "add", "a different add", ADD_SOURCE, ["a", "b"],
            [{"args": {"a": 1, "b": 1}, "expected": 2}],
            registry=registry)
        check("collision refused", clash["ok"] is False)
        check("collision stage is validated_call",
              clash["stage"] == "validated_call")
        check("original tool intact after collision",
              registry.dispatch("add", {"a": 1, "b": 1}) == 2)

        # 3. A failing tool is refused and NOT registered.
        failed = create_tool(
            "boom", "always fails", BAD_SOURCE, ["a"],
            [{"args": {"a": 1}, "expected": 1}],
            registry=registry)
        check("failing tool refused", failed["ok"] is False)
        check("failing tool not registered",
              "boom" not in registry.list())

        # 4. Banned constructs (import) are refused, not registered.
        banned = create_tool(
            "sneaky", "uses an import", IMPORT_SOURCE, ["a"],
            [{"args": {"a": 1}, "expected": 1}],
            registry=registry)
        check("import tool refused", banned["ok"] is False)
        check("import tool not registered",
              "sneaky" not in registry.list())

        # 5. Dispatch of an unknown tool is a real error, never output.
        try:
            registry.dispatch("no_such_tool", {})
            raised = False
        except ToolNotFoundError:
            raised = True
        check("unknown dispatch raises ToolNotFoundError", raised)

    print(f"ALL PASS: {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
