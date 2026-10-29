#!/usr/bin/env python3
"""Tests for the training-format tool-invocation parsing in the model loop.

Run:  python3 tests/test_tool_text_format.py
Run from the repo root. Stdlib only. Exit 0 when every
non-skipped test passes.

Background (verified live 2026-10-29): the backend model never
emits OpenAI-style tool_calls. It writes tool invocations as
plain text in its training format, e.g.
{"tool": "shell_execute", "arguments": {"command": "python3 --version"},
 "reason": "...", "expected_result": "...", "risk_level": "read_only"}.
The loop must parse that text and really execute the calls;
a tool-call JSON preview that is never executed is a failure.
"""

import json
import os
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from server import model_client  # noqa: E402
import server.model_loop as model_loop_module  # noqa: E402
from server.chat_server import create_server  # noqa: E402
import server.chat_server as chat_server_module  # noqa: E402
from server.model_loop import (  # noqa: E402
    _extract_text_tool_calls,
    run as run_model_loop,
)

PASSED = []
SKIPPED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except Exception:
        FAILED.append(name)
        print(f"FAIL {name}")
        traceback.print_exc()
    else:
        PASSED.append(name)
        print(f"PASS {name}")


def _set_env(url, model):
    if url is None:
        os.environ.pop("MODEL_API_URL", None)
    else:
        os.environ["MODEL_API_URL"] = url
    if model is None:
        os.environ.pop("MODEL_ID", None)
    else:
        os.environ["MODEL_ID"] = model


class _FakeShell:
    """Records run_shell calls; returns a fixed honest result."""

    def __init__(self):
        self.calls = []

    def run_shell(self, command, timeout_s=30):
        self.calls.append((command, timeout_s))
        return {"ok": True, "exit_code": 0, "stdout": "fake-out\n",
                "stderr": "", "truncated": False, "timed_out": False,
                "ms": 1.0}


class _FakeSession:
    def __init__(self):
        self.acts = []

    def act(self, action, **kw):
        self.acts.append((action, kw))
        return {"ok": True, "action": action, "ms": 3.0,
                "detail": {"url": kw.get("url"), "title": "Fake Title"}}

    def close(self):
        pass


def _text_response(content):
    return {"content": content, "tool_calls": [],
            "message": {"role": "assistant", "content": content}}


def _with_mocks(fake_chat, fake_shell=None):
    """Install mocks; returns a restore function."""
    _set_env("https://backend.invalid/chat", "test-backend")
    original_chat = model_client.chat
    original_shell = model_loop_module.shell_tool
    model_client.chat = fake_chat
    if fake_shell is not None:
        model_loop_module.shell_tool = fake_shell

    def restore():
        model_client.chat = original_chat
        model_loop_module.shell_tool = original_shell

    return restore


def _tool_messages(fake_chat_log):
    return [m for m in fake_chat_log["messages"]
            if m.get("role") == "tool"]


def _one_round_then_final(call_content, final_content="done",
                          shell=None):
    """Fake backend: one text-format call, then a plain reply.

    Asserts the tool round really executed and fed a real result
    back before the final reply.
    """
    shell = shell or _FakeShell()
    log = {"messages": [], "n": 0}

    def fake_chat(messages, tools=None):
        log["messages"] = list(messages)
        log["n"] += 1
        if log["n"] == 1:
            return _text_response(call_content)
        tool_msgs = _tool_messages(log)
        assert len(tool_msgs) == 1, log["messages"]
        payload = json.loads(tool_msgs[0]["content"])
        assert payload["ok"] is True, payload
        return _text_response(final_content)

    restore = _with_mocks(fake_chat, shell)
    try:
        result = run_model_loop([{"role": "user", "content": "go"}])
    finally:
        restore()
    return result, shell, log


# ---------------------------------------------------------------- tests
def t_text_whole_content_shell_execute():
    """Whole-content JSON {"tool": "shell_execute", ...} really runs."""
    content = json.dumps({
        "tool": "shell_execute",
        "arguments": {"command": "python3 --version"},
        "reason": "verify the Python version the worker is running on",
        "expected_result": "Python version output",
        "risk_level": "read_only",
    })
    result, shell, _log = _one_round_then_final(content, "version ok")
    assert shell.calls == [("python3 --version", 30)], shell.calls
    assert result["tool_rounds"] == 1, result
    assert result["reply"] == "version ok", result
    assert result["live"] is True, result


def t_text_fenced_block():
    """A ```json fenced invocation is extracted and executed."""
    content = ("Here is the call:\n```json\n" +
               json.dumps({"tool": "shell_execute",
                           "arguments": {"command": "echo fenced"},
                           "reason": "r"}) +
               "\n```\nThat is all.")
    result, shell, _log = _one_round_then_final(content, "fenced ok")
    assert shell.calls == [("echo fenced", 30)], shell.calls
    assert result["tool_rounds"] == 1, result
    assert result["reply"] == "fenced ok", result


def t_text_multiple_in_order():
    """Two invocations in one reply run in order, same round."""
    content = (json.dumps({"tool": "shell_execute",
                           "arguments": {"command": "echo one"}}) +
               " then " +
               json.dumps({"tool": "shell_execute",
                           "arguments": {"command": "echo two"}}))
    shell = _FakeShell()
    log = {"messages": [], "n": 0}

    def fake_chat(messages, tools=None):
        log["messages"] = list(messages)
        log["n"] += 1
        if log["n"] == 1:
            return _text_response(content)
        tool_msgs = _tool_messages(log)
        assert len(tool_msgs) == 2, log["messages"]
        first = json.loads(tool_msgs[0]["content"])
        second = json.loads(tool_msgs[1]["content"])
        assert first["ok"] is True and second["ok"] is True
        return _text_response("both ran")

    restore = _with_mocks(fake_chat, shell)
    try:
        result = run_model_loop([{"role": "user", "content": "go"}])
    finally:
        restore()
    assert shell.calls == [("echo one", 30), ("echo two", 30)], shell.calls
    assert result["tool_rounds"] == 1, result
    assert result["reply"] == "both ran", result


def t_text_browser_open_mapped():
    """{"tool": "browser_open", ...} runs a real goto on the fake session."""
    created = []
    content = json.dumps({"tool": "browser_open",
                          "arguments": {"url": "https://example.com"},
                          "reason": "open the page"})
    created = []

    def fake_chat(messages, tools=None):
        if not hasattr(fake_chat, "n"):
            fake_chat.n = 0
        fake_chat.n += 1
        if fake_chat.n == 1:
            return _text_response(content)
        return _text_response("opened it")

    restore = _with_mocks(fake_chat, _FakeShell())
    try:
        result = run_model_loop(
            [{"role": "user", "content": "open the page"}],
            session_factory=lambda: created.append(_FakeSession())
            or created[-1])
    finally:
        restore()
    assert result["tool_rounds"] == 1, result
    assert len(result["browser_results"]) == 1, result
    assert result["browser_results"][0]["ok"] is True, result
    assert created and created[0].acts == [
        ("goto", {"url": "https://example.com"})], created[0].acts
    assert result["reply"] == "opened it", result


def t_text_unknown_tool_not_executed():
    """An unknown training-format name is not executed, honestly."""
    content = json.dumps({"tool": "no_such_tool_xyz",
                          "arguments": {"x": 1}, "reason": "r"})
    created = []
    log = {"messages": [], "n": 0}

    def fake_chat(messages, tools=None):
        log["messages"] = list(messages)
        log["n"] += 1
        if log["n"] == 1:
            return _text_response(content)
        tool_msgs = _tool_messages(log)
        assert len(tool_msgs) == 1, log["messages"]
        assert "not executed" in tool_msgs[0]["content"], tool_msgs
        return _text_response("skipped it")

    restore = _with_mocks(fake_chat, _FakeShell())
    try:
        result = run_model_loop(
            [{"role": "user", "content": "go"}],
            session_factory=lambda: created.append(_FakeSession())
            or created[-1])
    finally:
        restore()
    assert result["reply"] == "skipped it", result
    assert result["browser_results"] == [], result
    assert created == [], "unknown tool must not create a browser session"


def t_text_malformed_skipped():
    """Truncated JSON is skipped honestly; content becomes the reply."""
    content = ('{"tool": "shell_execute", '
               '"arguments": {"command": "echo hi"')
    shell = _FakeShell()

    def fake_chat(messages, tools=None):
        return _text_response(content)

    restore = _with_mocks(fake_chat, shell)
    try:
        result = run_model_loop([{"role": "user", "content": "go"}])
    finally:
        restore()
    assert shell.calls == [], shell.calls
    assert result["tool_rounds"] == 0, result
    assert result["reply"] == content, result


def t_text_two_rounds_final_reply():
    """Tool results feed back; the backend's next reply ends the run."""
    content = json.dumps({"tool": "shell_execute",
                          "arguments": {"command": "date"}})
    result, shell, _log = _one_round_then_final(content, "all done")
    assert shell.calls == [("date", 30)], shell.calls
    assert result["tool_rounds"] == 1, result
    assert result["reply"] == "all done", result


def t_text_max_rounds_cap():
    """Endless text invocations stop at MAX_TOOL_ROUNDS, honestly."""
    original_max = model_loop_module.MAX_TOOL_ROUNDS
    model_loop_module.MAX_TOOL_ROUNDS = 2
    shell = _FakeShell()

    def fake_chat(messages, tools=None):
        return _text_response(json.dumps({
            "tool": "shell_execute",
            "arguments": {"command": "echo again"}}))

    restore = _with_mocks(fake_chat, shell)
    try:
        result = run_model_loop([{"role": "user", "content": "go"}])
    finally:
        model_loop_module.MAX_TOOL_ROUNDS = original_max
        restore()
    assert result["tool_rounds"] == 2, result
    assert result["reply"] == "tool rounds exhausted without a final reply", \
        result
    assert shell.calls == [("echo again", 30), ("echo again", 30)], \
        shell.calls


def t_extract_skips_non_tool_objects():
    """Plain JSON without the training shape yields no calls."""
    assert _extract_text_tool_calls('{"ok": true}') == []
    assert _extract_text_tool_calls("just some words") == []
    assert _extract_text_tool_calls("") == []
    assert _extract_text_tool_calls(None) == []


def t_extract_dedupes_repeated_call():
    """The same invocation twice (fenced + bare) runs only once."""
    call = json.dumps({"tool": "shell_execute",
                       "arguments": {"command": "echo dup"}})
    content = "```json\n" + call + "\n```\n" + call
    found = _extract_text_tool_calls(content)
    assert len(found) == 1, found
    assert found[0]["name"] == "shell_execute", found
    assert found[0]["arguments"] == {"command": "echo dup"}, found


def t_extract_run_shell_alias():
    """The exact "run_shell" training name maps to the shell executor."""
    content = json.dumps({"tool": "run_shell",
                          "arguments": {"command": "echo alias"}})
    result, shell, _log = _one_round_then_final(content, "alias ok")
    assert shell.calls == [("echo alias", 30)], shell.calls
    assert result["tool_rounds"] == 1, result


def t_status_copy_param():
    """GET /api/browser/status?copy=<id> probes that copy's session.

    Backward compatibility: without the param the previous shape
    is returned unchanged.
    """
    server = create_server(0, host="127.0.0.1")
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        chat_server_module._get_session("fmt-copy-a")
        try:
            with urllib.request.urlopen(
                    base + "/api/browser/status?copy=fmt-copy-a",
                    timeout=30) as resp:
                assert resp.status == 200, resp.status
                data = json.loads(resp.read())
            assert "fmt-copy-a" in data["sessions"], data
            assert data["active_copy_id"] == "fmt-copy-a", data
            # No page was ever started on this session: honest
            # nulls, no Playwright touched.
            assert data["active_alive"] is None, data
            assert data["active_url"] is None, data
            assert data["active_title"] is None, data
            # An unknown copy id: honest nulls, sessions unchanged.
            with urllib.request.urlopen(
                    base + "/api/browser/status?copy=fmt-nope",
                    timeout=30) as resp:
                data2 = json.loads(resp.read())
            assert data2["active_copy_id"] == "fmt-nope", data2
            assert data2["active_alive"] is None, data2
            assert data2["sessions"] == data["sessions"], data2
            # No param: previous response shape is unchanged.
            with urllib.request.urlopen(
                    base + "/api/browser/status",
                    timeout=30) as resp:
                data3 = json.loads(resp.read())
            assert set(data3) >= {"sessions", "active_copy_id",
                                  "active_alive", "active_url",
                                  "active_title"}, data3
        finally:
            chat_server_module._drop_session("fmt-copy-a")
    finally:
        server.shutdown()
        server.server_close()


def main():
    saved_url = os.environ.get("MODEL_API_URL")
    saved_model = os.environ.get("MODEL_ID")
    try:
        tests = [
            ("text-whole-content-shell_execute",
             t_text_whole_content_shell_execute),
            ("text-fenced-block", t_text_fenced_block),
            ("text-multiple-in-order", t_text_multiple_in_order),
            ("text-browser_open-mapped", t_text_browser_open_mapped),
            ("text-unknown-tool-not-executed",
             t_text_unknown_tool_not_executed),
            ("text-malformed-skipped", t_text_malformed_skipped),
            ("text-two-rounds-final-reply", t_text_two_rounds_final_reply),
            ("text-max-rounds-cap", t_text_max_rounds_cap),
            ("extract-skips-non-tool-objects",
             t_extract_skips_non_tool_objects),
            ("extract-dedupes-repeated-call",
             t_extract_dedupes_repeated_call),
            ("extract-run_shell-alias", t_extract_run_shell_alias),
            ("status-copy-param", t_status_copy_param),
        ]
        for name, fn in tests:
            check(name, fn)
    finally:
        if saved_url is None:
            os.environ.pop("MODEL_API_URL", None)
        else:
            os.environ["MODEL_API_URL"] = saved_url
        if saved_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = saved_model
    print(f"\n{len(PASSED)} passed, {len(SKIPPED)} skipped, "
          f"{len(FAILED)} failed")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
