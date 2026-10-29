"""5-stage tool pipeline: intake -> validated_call -> execute ->
evidence -> report.

A tool is pure computation only. Its source must parse as Python,
define exactly one module-level function with the tool's name, and
must NOT contain imports, eval/exec/compile, open(), __import__(),
input(), global statements, or dunder attribute access.

A tool whose validation fails, or whose declared test cases fail, is
NOT registered: the registry is left untouched and the report says
so. A name collision with an already-registered tool is refused at
the validated_call stage.

HONEST LIMIT: this pipeline validates provenance and tests. It is
NOT a sandbox — a registered tool runs in-process with the caller's
own privileges, exactly like the caller's own code. That is
documented here so nobody mistakes the pipeline for isolation.

This module does not import browser/, agent/, or copies/. Stdlib only.
"""

import hashlib
from datetime import datetime, timezone

from .registry import Registry, ToolRegistrationError

STAGES = ("intake", "validated_call", "execute", "evidence", "report")


def create_tool(name, description, source, arg_names, tests,
                registry=None):
    """Run all five stages and, only on full success, register the tool.

    tests: list of {"args": {...}, "expected": ...} dicts.
    Returns a report dict. "ok" is True only if the tool was
    validated, passed every test case, and was registered.
    """
    registry = registry if registry is not None else Registry()

    # Stage 1 — intake.
    if not isinstance(name, str) or not name.isidentifier():
        return {"ok": False, "tool": name, "stage": "intake",
                "error": f"invalid tool name: {name!r}"}
    if not isinstance(description, str) or not description.strip():
        return {"ok": False, "tool": name, "stage": "intake",
                "error": "description must be a non-empty string"}
    if not isinstance(tests, list) or not tests:
        return {"ok": False, "tool": name, "stage": "intake",
                "error": "at least one test case is required"}
    stages = ["intake"]

    # Stage 2 — validated_call: collision first, then source checks.
    # The registry re-validates source on register(); here we surface
    # the collision and any structural problem before executing tests.
    if name in registry.list():
        return {"ok": False, "tool": name, "stage": "validated_call",
                "error": f"tool {name!r} is already registered "
                         f"(collision refused)"}
    from .registry import _validate_source
    problem = _validate_source(name, source, arg_names)
    if problem:
        return {"ok": False, "tool": name, "stage": "validated_call",
                "error": problem}
    stages.append("validated_call")

    # Stage 3 — execute: load the source in an isolated namespace and
    # run its declared test cases. Nothing is registered yet.
    namespace = {}
    try:
        exec(compile(source, f"<tool:{name}>", "exec"), namespace)
        fn = namespace[name]
    except Exception as exc:
        return {"ok": False, "tool": name, "stage": "execute",
                "error": f"tool failed to load: {type(exc).__name__}: {exc}"}
    results = []
    for case in tests:
        args = case.get("args", {})
        expected = case.get("expected")
        try:
            actual = fn(**args)
            passed = actual == expected
            detail = None
        except Exception as exc:
            actual = None
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
        results.append({"args": args, "expected": expected,
                        "actual": actual, "passed": passed,
                        "detail": detail})
    stages.append("execute")

    # Stage 4 — evidence (in the report only; no tool file is written
    # by this pipeline — the registry JSON file is the record).
    evidence = {
        "tool": name,
        "description": description,
        "arg_names": list(arg_names),
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "bytes": len(source.encode("utf-8")),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "tests_total": len(results),
        "tests_passed": sum(1 for r in results if r["passed"]),
        "tests": results,
    }
    stages.append("evidence")

    # Stage 5 — report. Register only if every test passed.
    stages.append("report")
    if evidence["tests_passed"] != evidence["tests_total"]:
        return {"ok": False, "tool": name, "stages": stages,
                "error": "tool tests failed — tool was NOT registered",
                "evidence": evidence}
    try:
        registry.register(name, description, source, arg_names)
    except ToolRegistrationError as exc:
        return {"ok": False, "tool": name, "stages": stages,
                "error": f"registration refused: {exc}",
                "evidence": evidence}
    return {"ok": True, "tool": name, "stages": stages,
            "tests_passed": evidence["tests_passed"],
            "tests_total": evidence["tests_total"],
            "sha256": evidence["sha256"],
            "evidence": evidence}
