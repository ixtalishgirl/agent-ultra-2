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
    # First call carried exactly one tool, name + schema only.
    first_messages, first_tools = calls[0]
    assert first_messages == incoming, first_messages
    assert len(first_tools) == 1, first_tools
    assert first_tools[0] == BROWSER_OPEN_TOOL, first_tools
    fn = first_tools[0]["function"]
    assert set(fn.keys()) == {"name", "parameters"}, fn
    assert fn["name"] == "browser_open", fn
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
