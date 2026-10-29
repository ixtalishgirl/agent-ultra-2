#!/usr/bin/env python3
"""Tests for the agent-ultra-2 server wave.

Run:  python3 tests/test_server.py
Run from the repo root. Stdlib only. Exit 0 when every
non-skipped test passes.

The server is started on an ephemeral port in a thread. Real
browser parts are structural only here: when Playwright is absent
the browser action must fail honestly (ok False with a real
error), never with a fake success. The end-to-end live-browser
check lives in tests/test_integration.py and skips honestly
without Playwright.
"""

import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from browser import playwright_available  # noqa: E402
from server import model_client  # noqa: E402
import server.model_loop as model_loop_module  # noqa: E402
from server.chat_server import create_server  # noqa: E402
from server.model_loop import BROWSER_OPEN_TOOL, run as run_model_loop  # noqa: E402

PASSED = []
SKIPPED = []
FAILED = []


class _Skip(Exception):
    pass


def check(name, fn):
    try:
        fn()
    except _Skip as exc:
        SKIPPED.append(name)
        print(f"SKIP {name}: {exc}")
    except Exception:
        FAILED.append(name)
        print(f"FAIL {name}")
        traceback.print_exc()
    else:
        PASSED.append(name)
        print(f"PASS {name}")


BASE = None


def http_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type")


def http_post(path, obj):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _set_env(url, model):
    if url is None:
        os.environ.pop("MODEL_API_URL", None)
    else:
        os.environ["MODEL_API_URL"] = url
    if model is None:
        os.environ.pop("MODEL_ID", None)
    else:
        os.environ["MODEL_ID"] = model


# ---------------------------------------------------------------- tests
def t_health():
    code, body, _ = http_get("/health")
    assert code == 200, code
    data = json.loads(body)
    assert data["ok"] is True, data
    for key in ("supervisor", "memory", "tools", "disk"):
        assert key in data["checks"], data
        assert data["checks"][key] is True, data


def t_ui():
    code, body, ctype = http_get("/ui")
    assert code == 200, code
    assert "text/html" in (ctype or ""), ctype
    text = body.decode("utf-8")
    assert "Agent Ultra 2" in text
    assert "chat-area" in text and "browser-screen" in text
    code, body, _ = http_get("/ui/app.js")
    assert code == 200, code
    assert "2000" in body.decode("utf-8"), "screenshot poll interval missing"
    code, body, _ = http_get("/ui/style.css")
    assert code == 200, code


def t_screenshot_no_session():
    code, body, _ = http_get("/api/browser/screenshot")
    assert code == 404, code
    assert "no live browser session" in body.decode("utf-8"), body


def t_chat_not_live():
    _set_env(None, None)
    code, body = http_post("/api/chat", {
        "messages": [{"role": "user", "content": "hello"}]})
    assert code == 200, code
    data = json.loads(body)
    assert data["reply"] == "model not live yet", data
    assert data["live"] is False, data


def t_chat_bad_body():
    code, body = http_post("/api/chat", {})
    assert code == 400, code
    assert "messages" in body.decode("utf-8")


def t_tools_list():
    code, body, _ = http_get("/api/tools")
    assert code == 200, code
    data = json.loads(body)
    assert isinstance(data["tools"], list), data
    for tool in data["tools"]:
        assert "tool" in tool and "arg_names" in tool, tool


def t_browser_action_honest():
    code, body = http_post("/api/browser/action", {
        "action": "goto", "args": {"url": "https://example.com"},
        "copy_id": "test-copy"})
    assert code == 200, code
    data = json.loads(body)
    assert data["action"] == "goto" and "ms" in data and "ok" in data, data
    if not playwright_available():
        assert data["ok"] is False, data
        assert "error" in data["detail"], data


def t_arena_api_small():
    code, body = http_post("/api/arena/run", {
        "task_id": "server-test-task", "copies": 4, "max_concurrent": 4})
    assert code == 200, code
    data = json.loads(body)
    assert data["copies"] == 4 and data["reports"] == 4, data
    assert data["missing"] == [], data
    assert data["winner"] is not None, data
    winner_id = data["winner"].get("copy_id") or data["winner"].get("worker_id")
    assert winner_id is not None, data["winner"]
    assert data["comparison"][0]["worker_id"] == winner_id, data
    reports_path = Path(data["reports_file"])
    lines = [ln for ln in reports_path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 4, lines
    ids = {json.loads(ln).get("copy_id") for ln in lines}
    assert winner_id in ids, ids


# ------------------------------------------------- model-loop unit tests
class _FakeSession:
    def __init__(self):
        self.acts = []

    def act(self, action, **kw):
        self.acts.append((action, kw))
        return {"ok": True, "action": action, "ms": 12.5,
                "detail": {"url": kw.get("url"), "title": "Fake Title"}}

    def close(self):
        pass


def t_model_loop_tool_rules():
    _set_env("https://backend.invalid/chat", "test-backend")
    created = []
    calls = []

    def fake_chat(messages, tools=None):
        calls.append((list(messages), tools))
        if len(calls) == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "call-1", "name": "browser_open",
                                "arguments": {"url": "https://example.com"}}],
                "message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "browser_open",
                                                "arguments": json.dumps(
                                                    {"url": "https://example.com"})}}]},
            }
        return {"content": "opened it", "tool_calls": [],
                "message": {"role": "assistant", "content": "opened it"}}

    original = model_client.chat
    model_client.chat = fake_chat
    try:
        incoming = [{"role": "user", "content": "open the page"}]
        result = run_model_loop(
            incoming, session_factory=lambda: created.append(_FakeSession())
            or created[-1])
    finally:
        model_client.chat = original
    assert result["reply"] == "opened it", result
    assert result["tool_rounds"] == 1, result
    assert len(result["browser_results"]) == 1, result
    assert result["browser_results"][0]["ok"] is True, result
    assert result["browser_results"][0]["ms"] == 12.5, result
    # The caller's messages are unchanged.
    assert incoming == [{"role": "user", "content": "open the page"}], incoming
    # First call carried exactly the four model tools, each with
    # name + parameter schema only (no model-facing text).
    first_messages, first_tools = calls[0]
    assert first_messages == incoming, first_messages
    assert len(first_tools) == 4, first_tools
    names = [t["function"]["name"] for t in first_tools]
    assert names == ["browser_open", "run_shell", "create_tool",
                     "use_tool"], names
    for tool in first_tools:
        assert set(tool.keys()) == {"type", "function"}, tool
        assert tool["type"] == "function", tool
        fn = tool["function"]
        assert set(fn.keys()) == {"name", "parameters"}, fn
        assert isinstance(fn["parameters"], dict), fn
    # The fake session really ran goto.
    assert created and created[0].acts == [
        ("goto", {"url": "https://example.com"})], created[0].acts


def t_model_loop_unknown_tool_not_executed():
    _set_env("https://backend.invalid/chat", "test-backend")
    created = []

    def fake_chat(messages, tools=None):
        if not hasattr(fake_chat, "n"):
            fake_chat.n = 0
        fake_chat.n += 1
        if fake_chat.n == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "call-9", "name": "do_anything",
                                "arguments": {}}],
                "message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "call-9",
                                            "type": "function",
                                            "function": {
                                                "name": "do_anything",
                                                "arguments": "{}"}}]},
            }
        # Second call sees the tool message: it must report the
        # unknown tool as not executed.
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, messages
        assert "not executed" in tool_msgs[0]["content"], tool_msgs
        return {"content": "noted, unknown tool skipped", "tool_calls": [],
                "message": {"role": "assistant",
                            "content": "noted, unknown tool skipped"}}

    original = model_client.chat
    model_client.chat = fake_chat
    try:
        result = run_model_loop(
            [{"role": "user", "content": "hi"}],
            session_factory=lambda: created.append(_FakeSession())
            or created[-1])
    finally:
        model_client.chat = original
    assert result["reply"] == "noted, unknown tool skipped", result
    assert result["browser_results"] == [], result
    assert created == [], "unknown tool must not create a browser session"


def t_model_loop_shell_create_use_sequence():
    # Mocked backend calls run_shell, then create_tool, then
    # use_tool. Each must really execute; create_tool must go
    # through the 5-stage pipeline and register; use_tool must
    # dispatch the newly created tool. The registry is a temp
    # file so the repo registry is never touched.
    import tempfile
    from tools.registry import Registry
    _set_env("https://backend.invalid/chat", "test-backend")
    tmp = tempfile.mkdtemp(prefix="model-loop-test-")
    tmp_registry = Registry(Path(tmp) / "registry.json")
    original_registry = model_loop_module.Registry
    model_loop_module.Registry = lambda: tmp_registry
    GREET_SOURCE = "def greet(name):\n    return \"hi \" + name\n"

    def tool_call(call_id, name, arguments):
        return {
            "content": None,
            "tool_calls": [{"id": call_id, "name": name,
                            "arguments": arguments}],
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments)}}]},
        }

    steps = iter([
        tool_call("c-1", "run_shell",
                  {"command": "echo hello-tool", "timeout_s": 10}),
        tool_call("c-2", "create_tool",
                  {"name": "greet", "description": "greets by name",
                   "source": GREET_SOURCE, "arg_names": ["name"],
                   "tests": [{"args": ["world"],
                              "expected": "hi world"}]}),
        tool_call("c-3", "use_tool",
                  {"tool": "greet", "args": ["world"]}),
    ])

    def fake_chat(messages, tools=None):
        try:
            return next(steps)
        except StopIteration:
            return {"content": "done", "tool_calls": [],
                    "message": {"role": "assistant", "content": "done"}}

    original = model_client.chat
    model_client.chat = fake_chat
    try:
        result = run_model_loop(
            [{"role": "user", "content": "do the sequence"}],
            session_factory=lambda: _FakeSession())
    finally:
        model_client.chat = original
        model_loop_module.Registry = original_registry

    assert result["reply"] == "done", result
    assert result["tool_rounds"] == 3, result
    assert result["live"] is True, result

    # Each round really executed: the registry now holds the new
    # tool and it dispatches real output.
    assert "greet" in tmp_registry.list(), "create_tool must register"
    assert tmp_registry.dispatch("greet", {"name": "zed"}) == "hi zed"


def t_model_loop_shell_create_use_results():
    # Same sequence, but asserts the tool-message payloads the
    # loop fed back: real shell output, the full pipeline report,
    # and the real dispatch result.
    import tempfile
    from tools.registry import Registry
    _set_env("https://backend.invalid/chat", "test-backend")
    tmp = tempfile.mkdtemp(prefix="model-loop-test-")
    tmp_registry = Registry(Path(tmp) / "registry.json")
    original_registry = model_loop_module.Registry
    model_loop_module.Registry = lambda: tmp_registry
    GREET_SOURCE = "def greet(name):\n    return \"hi \" + name\n"
    seen = []

    def tool_call(call_id, name, arguments):
        return {
            "content": None,
            "tool_calls": [{"id": call_id, "name": name,
                            "arguments": arguments}],
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments)}}]},
        }

    plan = [
        tool_call("c-1", "run_shell",
                  {"command": "echo hello-tool", "timeout_s": 10}),
        tool_call("c-2", "create_tool",
                  {"name": "greet", "description": "greets by name",
                   "source": GREET_SOURCE, "arg_names": ["name"],
                   "tests": [{"args": ["world"],
                              "expected": "hi world"}]}),
        tool_call("c-3", "use_tool",
                  {"tool": "greet", "args": ["world"]}),
    ]
    plan_iter = iter(plan)

    def fake_chat(messages, tools=None):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        seen.append([json.loads(m["content"]) for m in tool_msgs])
        try:
            return next(plan_iter)
        except StopIteration:
            return {"content": "done", "tool_calls": [],
                    "message": {"role": "assistant", "content": "done"}}

    original = model_client.chat
    model_client.chat = fake_chat
    try:
        result = run_model_loop(
            [{"role": "user", "content": "do the sequence"}],
            session_factory=lambda: _FakeSession())
    finally:
        model_client.chat = original
        model_loop_module.Registry = original_registry

    assert result["reply"] == "done", result
    assert result["tool_rounds"] == 3, result
    # seen[0] is the empty tool list before any call; each later
    # entry accumulates the tool messages so far, so the newest
    # result of each round is the last element.
    assert len(seen) == 4, seen
    shell_result = seen[1][-1]
    assert shell_result["ok"] is True, shell_result
    assert "hello-tool" in shell_result["stdout"], shell_result
    report = seen[2][-1]
    assert report["ok"] is True, report
    assert report["stages"] == ["intake", "validated_call", "execute",
                                "evidence", "report"], report
    assert report["tests_passed"] == 1 and report["tests_total"] == 1
    use_result = seen[3][-1]
    assert use_result["ok"] is True, use_result
    assert use_result["tool"] == "greet", use_result
    assert use_result["result"] == "hi world", use_result


def t_shell_api():
    code, body = http_post("/api/shell",
                           {"command": "echo shell-proof", "timeout_s": 10})
    assert code == 200, code
    data = json.loads(body)
    assert data["ok"] is True, data
    assert data["exit_code"] == 0, data
    assert "shell-proof" in data["stdout"], data
    assert data["timed_out"] is False, data


def t_shell_api_bad_body():
    code, body = http_post("/api/shell", {})
    assert code == 400, code
    assert "command" in body.decode("utf-8")
    code, body = http_post("/api/shell", {"command": "echo x",
                                          "timeout_s": "fast"})
    assert code == 400, code
    assert "timeout_s" in body.decode("utf-8")


def main():
    global BASE
    saved_url = os.environ.get("MODEL_API_URL")
    saved_model = os.environ.get("MODEL_ID")
    server = create_server(0, host="127.0.0.1")
    port = server.server_address[1]
    BASE = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Screenshot honesty must be checked before any browser
        # action creates a session.
        tests = [
            ("health-200-real-checks", t_health),
            ("ui-200", t_ui),
            ("screenshot-404-no-session", t_screenshot_no_session),
            ("chat-not-live", t_chat_not_live),
            ("chat-bad-body-400", t_chat_bad_body),
            ("tools-list", t_tools_list),
            ("browser-action-honest", t_browser_action_honest),
            ("arena-api-small-winner", t_arena_api_small),
            ("model-loop-tool-rules", t_model_loop_tool_rules),
            ("model-loop-unknown-tool-not-executed",
             t_model_loop_unknown_tool_not_executed),
            ("model-loop-shell-create-use-sequence",
             t_model_loop_shell_create_use_sequence),
            ("model-loop-shell-create-use-results",
             t_model_loop_shell_create_use_results),
            ("shell-api", t_shell_api),
            ("shell-api-bad-body", t_shell_api_bad_body),
        ]
        for name, fn in tests:
            check(name, fn)
    finally:
        _set_env(saved_url, saved_model)
        server.shutdown()
        server.server_close()
    print(f"\n{len(PASSED)} passed, {len(SKIPPED)} skipped, {len(FAILED)} failed")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
