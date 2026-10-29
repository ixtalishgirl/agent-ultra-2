"""Model loop for Agent Ultra 2: messages plus four model tools.

Rules enforced here:

  - The caller's ``messages`` are passed to the backend UNCHANGED:
    nothing is inserted into them and the caller's list is never
    mutated (a copy is used for the working thread). Only the
    assistant and tool messages produced by the tool rounds are
    appended to the working copy.
  - Exactly four tools are offered: ``browser_open``,
    ``run_shell``, ``create_tool`` and ``use_tool``. Each tool
    definition carries name plus parameter schema ONLY. No other
    key is present on any tool definition, and no model-facing
    text is written anywhere in this module.
  - ``browser_open`` runs a real ``BrowserSession`` goto and feeds
    the real, measured result back as a tool message.
  - ``run_shell`` runs a real bash command in the pinned work
    directory (tools/shell_tool.py) and feeds the real result
    back as a tool message.
  - ``create_tool`` runs the 5-stage tool pipeline
    (tools/pipeline.py) and feeds the full pipeline report back
    as a tool message. A failed pipeline registers nothing.
  - ``use_tool`` dispatches a registered tool
    (tools/registry.py) and feeds its real output back as a tool
    message; an unknown tool is not executed.
  - At most 8 tool rounds run (multi-step tasks need it).
  - Any other tool name is NOT executed. The tool message for it
    says so honestly.
  - If the backend is not configured, the reply is exactly
    ``"model not live yet"`` -- never a fabricated reply.
"""

import json

from browser.profile import DEFAULT_BASE_DIR, profile_dir_for
from browser.session import BrowserSession
from browser.worker import get_worker
from server import model_client
from tools import shell_tool
from tools.pipeline import create_tool as pipeline_create_tool
from tools.registry import Registry, ToolNotFoundError

MAX_TOOL_ROUNDS = 8

BROWSER_OPEN_TOOL = {
    "type": "function",
    "function": {
        "name": "browser_open",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
}

RUN_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
            "required": ["command"],
        },
    },
}

CREATE_TOOL_TOOL = {
    "type": "function",
    "function": {
        "name": "create_tool",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "source": {"type": "string"},
                "arg_names": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "args": {"type": "array"},
                            "expected": {},
                        },
                        "required": ["args", "expected"],
                    },
                },
            },
            "required": ["name", "description", "source", "arg_names",
                         "tests"],
        },
    },
}

USE_TOOL_TOOL = {
    "type": "function",
    "function": {
        "name": "use_tool",
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "args": {"type": "array"},
            },
            "required": ["tool"],
        },
    },
}

MODEL_TOOLS = [BROWSER_OPEN_TOOL, RUN_SHELL_TOOL, CREATE_TOOL_TOOL,
               USE_TOOL_TOOL]

NOT_LIVE = "model not live yet"


def _default_session_factory():
    return BrowserSession(
        profile_dir_for(DEFAULT_BASE_DIR, "model-loop"),
        headless=True,
    )


def _tool_result_for_unknown(name):
    return {"ok": False, "error": f"unknown tool {name!r}: not executed"}


def _exec_run_shell(args):
    """Execute the run_shell tool call; real bash, real result."""
    if not isinstance(args, dict):
        return {"ok": False,
                "error": "run_shell needs an arguments object"}
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"ok": False,
                "error": "run_shell needs a command string"}
    timeout_s = args.get("timeout_s", 30)
    return shell_tool.run_shell(command, timeout_s=timeout_s)


def _exec_create_tool(args):
    """Execute the create_tool tool call; real 5-stage pipeline.

    The model sends each test's ``args`` as an array; it is
    converted to the pipeline's ``{"arg_name": value}`` shape by
    position (count mismatch is an honest error, not a guess).
    Returns the pipeline's full report dict unchanged.
    """
    if not isinstance(args, dict):
        return {"ok": False,
                "error": "create_tool needs an arguments object"}
    arg_names = args.get("arg_names")
    tests = args.get("tests")
    if not isinstance(arg_names, list):
        return {"ok": False,
                "error": "create_tool needs arg_names as an array "
                         "of strings"}
    if not isinstance(tests, list):
        return {"ok": False,
                "error": "create_tool needs tests as an array"}
    norm_tests = []
    for case in tests:
        if not isinstance(case, dict):
            return {"ok": False,
                    "error": "each test must be an object with args "
                             "and expected"}
        targs = case.get("args")
        if isinstance(targs, list):
            if len(targs) != len(arg_names):
                return {"ok": False,
                        "error": "test args count does not match "
                                 "arg_names count"}
            targs = dict(zip(arg_names, targs))
        if not isinstance(targs, dict):
            return {"ok": False,
                    "error": "test args must be an array matching "
                             "arg_names, or an object"}
        norm_tests.append({"args": targs, "expected": case.get("expected")})
    try:
        registry = Registry()
    except Exception as exc:
        return {"ok": False, "error": f"registry unavailable: {exc}"}
    return pipeline_create_tool(
        args.get("name"), args.get("description"), args.get("source"),
        arg_names, norm_tests, registry=registry)


def _exec_use_tool(args):
    """Execute the use_tool tool call; real registry dispatch.

    The model sends ``args`` as an array; it is converted to
    keyword arguments by position against the registered tool's
    own arg_names (count mismatch is an honest error). An
    unknown tool is caught and reported, never executed.
    """
    if not isinstance(args, dict):
        return {"ok": False,
                "error": "use_tool needs an arguments object"}
    name = args.get("tool")
    targs = args.get("args", [])
    if not isinstance(name, str) or not name:
        return {"ok": False,
                "error": "use_tool needs a tool name string"}
    try:
        registry = Registry()
    except Exception as exc:
        return {"ok": False, "error": f"registry unavailable: {exc}"}
    if isinstance(targs, list):
        listing = registry.list()
        if name not in listing:
            return {"ok": False,
                    "error": f"unknown tool {name!r}: not executed"}
        arg_names = listing[name].get("arg_names", [])
        if len(targs) != len(arg_names):
            return {"ok": False,
                    "error": "args count does not match the tool's "
                             "arg_names"}
        kwargs = dict(zip(arg_names, targs))
    elif isinstance(targs, dict):
        kwargs = targs
    else:
        return {"ok": False,
                "error": "use_tool args must be an array, or an object"}
    try:
        output = registry.dispatch(name, kwargs)
    except ToolNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "tool": name, "result": output}


def run(messages, session_factory=None):
    """Run one user thread through the backend with the model tools.

    ``messages`` is a list of ``{"role", "content"}`` dicts (tool
    messages are appended by the loop itself on later rounds).
    ``session_factory`` is an injection point for tests; when it is
    None a real ``BrowserSession`` is created lazily on the first
    ``browser_open`` call and closed when the run ends.

    Returns a dict::

        {"reply": str, "live": bool, "tool_rounds": int,
         "browser_results": [real BrowserSession.act results],
         "error": str | None}

    ``reply`` is the backend's final content, or an honest string
    when there is no backend, no content, or the tool rounds run
    out. ``browser_results`` holds only results from real goto
    calls (or real failures from them).
    """
    if not model_client.health()["live"]:
        return {
            "reply": NOT_LIVE,
            "live": False,
            "tool_rounds": 0,
            "browser_results": [],
            "error": None,
        }
    working = list(messages)
    browser_results = []
    tool_rounds = 0
    session = None
    session_owned = False
    error = None
    reply = None
    try:
        while True:
            try:
                response = model_client.chat(
                    working, tools=MODEL_TOOLS)
            except RuntimeError as exc:
                if str(exc) == NOT_LIVE:
                    return {
                        "reply": NOT_LIVE,
                        "live": False,
                        "tool_rounds": tool_rounds,
                        "browser_results": browser_results,
                        "error": None,
                    }
                error = str(exc)
                reply = f"model request failed: {exc}"
                break
            content = response.get("content")
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                if isinstance(content, str) and content:
                    reply = content
                else:
                    reply = "model returned no content"
                break
            if tool_rounds >= MAX_TOOL_ROUNDS:
                # Cap reached: the extra tool call is NOT executed.
                reply = "tool rounds exhausted without a final reply"
                break
            # One tool round: append the backend's own assistant
            # message verbatim, then one tool message per call.
            working.append(response.get("message") or {
                "role": "assistant", "content": content,
                "tool_calls": [],
            })
            tool_rounds += 1
            for call in tool_calls:
                name = call.get("name")
                args = call.get("arguments") or {}
                if name == "browser_open":
                    url = args.get("url") if isinstance(args, dict) else None
                    if not isinstance(url, str) or not url:
                        result = {"ok": False,
                                  "error": "browser_open needs a url string"}
                    else:
                        if session is None:
                            factory = session_factory or _default_session_factory
                            session = factory()
                            session_owned = session_factory is None
                        # The loop runs on an HTTP handler thread;
                        # Playwright itself only ever runs on the
                        # dedicated browser worker thread.
                        result = get_worker().submit(
                            lambda: session.act("goto", url=url))
                        browser_results.append(result)
                elif name == "run_shell":
                    result = _exec_run_shell(args)
                elif name == "create_tool":
                    result = _exec_create_tool(args)
                elif name == "use_tool":
                    result = _exec_use_tool(args)
                else:
                    result = _tool_result_for_unknown(name)
                working.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, default=str),
                })
    finally:
        if session is not None and session_owned:
            try:
                get_worker().submit(session.close, timeout=30)
            except Exception:
                pass
    return {
        "reply": reply,
        "live": True,
        "tool_rounds": tool_rounds,
        "browser_results": browser_results,
        "error": error,
    }
