#!/usr/bin/env python3
"""Tests for the browser worker thread + dead-session recovery.

Run:  python3 tests/test_browser_worker.py
Run from the repo root. Stdlib only. Exit 0 when every
non-skipped test passes.

Regression context: Playwright's sync API is bound to the thread
that created it, and the HTTP server is threaded, so a session
created by one request thread broke on the next request thread
(goto worked, then screenshots and actions failed with a dead
connection). The fix routes every Playwright call through one
dedicated browser worker thread, and recovers dead sessions by
dropping + relaunching once. These tests prove both, including
an end-to-end HTTP test that mirrors the deployed flow (goto on
one request thread, screenshot and scroll on later ones).

A second, subtler failure was seen live on the deployed service
2026-10-29 ~17:12 PKT: a goto on a stale session answered in
0.1ms with "It looks like you are using Playwright Sync API
inside the asyncio loop" -- Playwright's thread-local loop state
was poisoned when the session's driver died mid-call, so
sync_playwright() could never succeed on that thread again.
The fix treats that text as a dead-session marker (drop + retry
once) and makes the worker abandon a poisoned thread for a fresh
one, retrying the call exactly once there. These tests prove
that too, with fakes, so they run on any interpreter.

Real-browser tests run ONLY when Playwright and a Chromium
binary are actually present in this interpreter; otherwise they
are reported as SKIPPED with the real reason, never faked.

A third failure was seen live 2026-10-29 ~17:45 PKT on the build
that already had the 17:12 fix: Open accounts.google.com on a
FRESH copy id answered ok:false in 0.0ms with the same
asyncio-loop text, and the worker thread was never replaced.
Root cause: BrowserSession.act() caught the poison error and
converted it to a failure dict, so submit()'s poison detection
never saw the exception, the poisoned thread was never
abandoned, and the dead-result retry ran on the same poisoned
thread. The fix: act() re-raises the poison error (thread-state
poison is not a session failure), letting submit() heal the
thread and retry on a fresh one. These tests prove that.
"""

import glob
import json
import os
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from browser import playwright_available  # noqa: E402
from browser.profile import safe_copy_id  # noqa: E402
import browser.session as session_mod  # noqa: E402
from browser.session import BrowserSession  # noqa: E402
from browser import worker as worker_mod  # noqa: E402
from browser.worker import BrowserWorker, get_worker  # noqa: E402
import server.chat_server as chat_server  # noqa: E402
from server.chat_server import (  # noqa: E402
    _drop_session,
    _get_session,
    _is_dead_result,
    create_server,
    run_browser_action,
)

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


def skip(reason, *_ignored):
    raise _Skip(reason)


def chromium_present():
    if not playwright_available():
        return False
    candidates = (
        glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        + glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/chrome"))
    )
    return bool(candidates)


# ---------------------------------------------------------------- fakes
class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self._title = "blank"
        self.scroll_y = 0

    def title(self):
        return self._title

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self._title = "Fake Page"

    def screenshot(self, path=None):
        return b"\x89PNG\r\n\x1a\nFAKE-SCREENSHOT-BYTES"

    def evaluate(self, expression):
        if expression == "window.scrollY":
            return self.scroll_y
        return 1

    @property
    def mouse(self):
        page = self

        class _Mouse:
            def wheel(self, dx, dy):
                page.scroll_y = max(0, page.scroll_y + dy)

        return _Mouse()


class DeadPage:
    """A page whose connection is already dead, like the deployed
    session after its creating thread died."""

    def goto(self, url, wait_until=None, timeout=None):
        raise Exception("TargetClosedError: Playwright connection closed")

    def screenshot(self, path=None):
        raise Exception("TargetClosedError: Playwright connection closed")

    def evaluate(self, expression):
        raise Exception("TargetClosedError: Playwright connection closed")


def fake_session(tmp):
    session = BrowserSession(tmp / "profile", headless=True)
    session._page = FakePage()
    return session


# ---------------------------------------------------------------- tests
def t_worker_single_thread():
    worker = BrowserWorker()
    caller_idents = set()
    results = []

    def probe():
        return threading.get_ident()

    def run():
        caller_idents.add(threading.get_ident())
        results.append(worker.submit(probe))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 3, results
    assert len(set(results)) == 1, results
    assert results[0] == worker.thread_ident, (results, worker.thread_ident)
    assert results[0] not in caller_idents, "worker ran on a caller thread"


def t_worker_exception_propagates():
    worker = BrowserWorker()

    def boom():
        raise ValueError("real boom")

    try:
        worker.submit(boom)
    except ValueError as exc:
        assert str(exc) == "real boom"
    else:
        raise AssertionError("exception was swallowed")
    # Worker survives a failing task.
    assert worker.submit(lambda: 42) == 42


def t_worker_nested_inline_no_deadlock():
    worker = BrowserWorker()

    def outer():
        return worker.submit(lambda: "inner") + "-outer"

    assert worker.submit(outer) == "inner-outer"


def t_fake_session_persists_across_caller_threads():
    with tempfile.TemporaryDirectory() as tmp:
        session = fake_session(Path(tmp))
        worker = get_worker()
        results = {}

        def do_goto():
            results["goto"] = worker.submit(
                lambda: session.act("goto", url="https://example.com"))

        def do_shot():
            results["shot"] = worker.submit(
                lambda: session._page.screenshot())

        def do_scroll():
            results["scroll"] = worker.submit(
                lambda: session.act("scroll_down", amount=400))

        threads = [threading.Thread(target=fn)
                   for fn in (do_goto, do_shot, do_scroll)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert results["goto"]["ok"] is True, results
        assert results["shot"].startswith(b"\x89PNG"), results
        assert results["scroll"]["ok"] is True, results
        assert results["scroll"]["detail"]["y"] == 400, results


def t_recovery_dead_page_real():
    """Dead page -> drop + relaunch once -> goto succeeds again.

    Skipped honestly without Playwright/Chromium in this
    interpreter.
    """
    if not chromium_present():
        skip("real-browser",
             "playwright package or Chromium binary not present here")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        page_file = base / "page.html"
        page_file.write_text(
            "<html><head><title>Recovery Page</title></head><body>"
            "<p>filler</p>" * 200 + "</body></html>", encoding="utf-8")
        url = page_file.as_uri()
        first = run_browser_action("recovery-copy", "goto", {"url": url},
                                   base_dir=base)
        assert first["ok"] is True, first
        assert first["detail"]["title"] == "Recovery Page", first
        # Kill the session the way the deployed bug left it: a page
        # object whose connection is dead, still registered.
        session = _get_session("recovery-copy", base_dir=base)
        session._page = DeadPage()
        second = run_browser_action("recovery-copy", "goto", {"url": url},
                                    base_dir=base)
        assert second["ok"] is True, second
        assert second["detail"]["title"] == "Recovery Page", second
        _drop_session("recovery-copy")


def t_http_persists_across_requests_real():
    """The deployed flow over HTTP: goto, screenshot, scroll,
    screenshot again, status — each on its own request thread.

    Skipped honestly without Playwright/Chromium in this
    interpreter.
    """
    if not chromium_present():
        skip("real-browser",
             "playwright package or Chromium binary not present here")
        return
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "page.html"
        page_file.write_text(
            "<html><head><title>HTTP Page</title></head><body>"
            "<p>filler</p>" * 200 + "</body></html>", encoding="utf-8")
        url = page_file.as_uri()
        server = create_server(0, host="127.0.0.1")
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path, obj):
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(obj).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        return resp.status, resp.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            def get(path):
                try:
                    with urllib.request.urlopen(base + path,
                                                timeout=60) as resp:
                        return resp.status, resp.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            code, body = post("/api/browser/action", {
                "action": "goto", "args": {"url": url},
                "copy_id": "http-test-copy"})
            assert code == 200, code
            data = json.loads(body)
            assert data["ok"] is True, data
            assert data["detail"]["title"] == "HTTP Page", data
            code, body = get("/api/browser/screenshot?copy=http-test-copy")
            assert code == 200, (code, body[:200])
            assert body.startswith(b"\x89PNG"), body[:20]
            code, body = post("/api/browser/action", {
                "action": "scroll_down", "args": {"amount": 300},
                "copy_id": "http-test-copy"})
            assert code == 200, code
            data = json.loads(body)
            assert data["ok"] is True, data
            assert data["detail"]["y"] > 0, data
            code, body = get("/api/browser/screenshot?copy=http-test-copy")
            assert code == 200, (code, body[:200])
            assert body.startswith(b"\x89PNG"), body[:20]
            code, body = get("/api/browser/status")
            assert code == 200, code
            data = json.loads(body)
            assert data["active_alive"] is True, data
            assert data["active_title"] == "HTTP Page", data
        finally:
            _drop_session("http-test-copy")
            server.shutdown()
            server.server_close()


# ------------------------------------------------- 17:12 PKT poison
# Exact error text the deployed service returned on a stale
# session: a goto answered in 0.1ms with Playwright's
# sync-in-asyncio-loop error instead of a dead-connection marker.
POISON_TEXT = ("Error: It looks like you are using Playwright Sync API "
               "inside the asyncio loop.\nPlease use the Async API instead.")


def t_poison_text_classified_dead():
    assert _is_dead_result({
        "ok": False, "action": "goto", "ms": 0.1,
        "detail": {"error": POISON_TEXT}}) is True
    assert _is_dead_result({
        "ok": False, "action": "goto", "ms": 0.1,
        "detail": {"error": "TargetClosedError: gone"}}) is True
    assert _is_dead_result({"ok": True, "action": "goto"}) is False


def t_worker_heals_poisoned_thread():
    """A submit that raises the poison error is retried exactly once
    on a FRESH worker thread; the poisoned thread is abandoned."""
    worker = BrowserWorker()
    first_thread = worker.thread_ident
    assert first_thread is not None
    attempts = []

    def flaky():
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            raise Exception(POISON_TEXT)
        return "healed"

    assert worker.submit(flaky) == "healed"
    assert len(attempts) == 2, attempts
    assert attempts[0] == first_thread, attempts
    assert attempts[1] != first_thread, attempts
    assert attempts[1] == worker.thread_ident, attempts
    # The fresh thread keeps working afterwards.
    assert worker.submit(lambda: threading.get_ident()) == \
        worker.thread_ident


def t_worker_no_heal_on_ordinary_error():
    """Ordinary failures never replace the worker thread."""
    worker = BrowserWorker()
    before = worker.thread_ident

    def boom():
        raise ValueError("plain failure")

    try:
        worker.submit(boom)
    except ValueError:
        pass
    else:
        raise AssertionError("exception was swallowed")
    assert worker.thread_ident == before, (worker.thread_ident, before)
    assert worker.submit(lambda: 1) == 1


class _PoisonSession:
    """act() returns the exact 17:12 PKT failure: ok:false in 0.1ms
    with the asyncio-loop error text. Records which thread ran
    every touch."""

    def __init__(self):
        self.acts = []
        self.closed_on = None

    def act(self, action, **kw):
        self.acts.append((threading.get_ident(), action, kw))
        return {"ok": False, "action": action, "ms": 0.1,
                "detail": {"error": POISON_TEXT}}

    def close(self):
        self.closed_on = threading.get_ident()


class _HealSession:
    """The fresh session the retry runs on."""

    def __init__(self):
        self.acts = []
        self.closed_on = None

    def act(self, action, **kw):
        self.acts.append((threading.get_ident(), action, kw))
        return {"ok": True, "action": action, "ms": 3.2,
                "detail": {"url": kw.get("url"), "title": "Healed Page"}}

    def close(self):
        self.closed_on = threading.get_ident()


def t_recovery_asyncio_loop_poison_fake():
    """The deployed 17:12 failure through the real recovery path:
    poison text -> classified dead -> session dropped on the
    browser thread -> exactly one retry on a fresh session ->
    success, with every Playwright touch on the browser thread
    and no sync-in-asyncio error in the final result."""
    made = []

    def fake_get_session(copy_id, base_dir=None):
        cid = safe_copy_id(copy_id)
        with chat_server._sessions_lock:
            sess = chat_server._sessions.get(cid)
            if sess is None:
                sess = _PoisonSession() if not made else _HealSession()
                made.append(sess)
                chat_server._sessions[cid] = sess
            return sess

    real_get = chat_server._get_session
    chat_server._get_session = fake_get_session
    try:
        result = run_browser_action(
            "poison-copy", "goto", {"url": "https://example.com"})
    finally:
        chat_server._get_session = real_get
        with chat_server._sessions_lock:
            chat_server._sessions.pop("poison-copy", None)
    assert result["ok"] is True, result
    assert result["detail"]["title"] == "Healed Page", result
    assert "asyncio" not in json.dumps(result), result
    poison, healed = made
    # The poison session was tried once, dropped, never retried.
    assert len(poison.acts) == 1, poison.acts
    assert len(healed.acts) == 1, healed.acts
    worker = get_worker()
    for ident, _action, _kw in poison.acts + healed.acts:
        assert ident == worker.thread_ident, (ident, worker.thread_ident)
    # The dead session was closed on the browser thread too.
    assert poison.closed_on == worker.thread_ident, poison.closed_on
    # The registry now holds the fresh session, not the poison one.
    with chat_server._sessions_lock:
        assert "poison-copy" not in chat_server._sessions


# ------------------------------------------------- 17:45 PKT round 3
# The 17:12 fix was deployed but INCOMPLETE: a fresh-copy open on
# the live build still failed with the asyncio-loop text because
# BrowserSession.act() swallowed the poison error into a result
# dict, so the worker never healed its thread. These fakes model
# Playwright's sticky thread-local poison: once a thread's loop
# state is poisoned, every later sync_playwright() on THAT thread
# raises; a fresh thread is clean.
ROUND3_POISON = ("Error: It looks like you are using Playwright Sync API "
                 "inside the asyncio loop.\nPlease use the Async API instead.")


class _Round3Page:
    def __init__(self):
        self.url = "about:blank"

    def title(self):
        return "Round3 Page"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url


def _round3_fake_pw():
    page = _Round3Page()

    class FakeCtx:
        @property
        def pages(self):
            return [page]

        def new_page(self):
            return page

        def close(self):
            pass

    class FakeChromium:
        @staticmethod
        def launch_persistent_context(*a, **k):
            return FakeCtx()

    pw = type("FakePW", (), {})()
    pw.chromium = FakeChromium()
    pw.stop = lambda: None
    return pw


class _Round3CM:
    """Fake sync_playwright() context manager. ``first_start_error``
    is raised by the first start() call and poisons that thread's
    loop state (like a dead driver does); afterwards every start()
    on the poisoned thread raises the asyncio-loop error, while any
    fresh thread gets a working fake browser."""

    first_start_error = None
    poison_thread = None
    start_idents = []

    def start(self):
        ident = threading.get_ident()
        type(self).start_idents.append(ident)
        err = type(self).first_start_error
        if err is not None:
            type(self).first_start_error = None
            type(self).poison_thread = ident
            raise err
        if type(self).poison_thread is not None and \
                ident == type(self).poison_thread:
            raise Exception(ROUND3_POISON)
        return _round3_fake_pw()

    def stop(self):
        pass


class _SwappedGlobals:
    """Swap the module-global worker and the session's playwright
    entry point for one test, restoring both afterwards."""

    def __init__(self, worker):
        self.worker = worker

    def __enter__(self):
        self._old_worker = worker_mod._worker
        self._old_npc = session_mod._new_page_context
        worker_mod._worker = self.worker
        session_mod._new_page_context = _Round3CM
        _Round3CM.first_start_error = None
        _Round3CM.poison_thread = None
        _Round3CM.start_idents = []
        return self

    def __exit__(self, *exc):
        worker_mod._worker = self._old_worker
        session_mod._new_page_context = self._old_npc
        _Round3CM.first_start_error = None
        _Round3CM.poison_thread = None
        return False


def t_act_reraises_poison_error():
    """act() must NOT swallow the asyncio-loop poison error: it is
    thread-state poison, not a session failure, and only submit()
    can recover from it."""

    class BoomCM:
        def start(self):
            raise Exception(ROUND3_POISON)

        def stop(self):
            pass

    old = session_mod._new_page_context
    session_mod._new_page_context = BoomCM
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp) / "p", headless=True)
            try:
                session.act("goto", url="https://example.com")
            except Exception as exc:
                assert "inside the asyncio loop" in str(exc), exc
            else:
                raise AssertionError("act() swallowed the poison error")
    finally:
        session_mod._new_page_context = old


def t_act_still_swallows_ordinary_errors():
    """Ordinary Playwright failures keep the result-dict contract:
    only the poison error propagates."""

    class BoomCM:
        def start(self):
            raise Exception("TargetClosedError: gone")

        def stop(self):
            pass

    old = session_mod._new_page_context
    session_mod._new_page_context = BoomCM
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp) / "p", headless=True)
            result = session.act("goto", url="https://example.com")
    finally:
        session_mod._new_page_context = old
    assert result["ok"] is False, result
    assert "TargetClosedError" in result["detail"]["error"], result
    assert "asyncio" not in json.dumps(result), result


def t_round3_fresh_copy_poisoned_thread():
    """The exact 17:45 PKT scenario: fresh copy id, worker thread
    already poisoned. The poison error must reach submit(), the
    thread must be replaced, and the open must succeed -- with no
    asyncio text in the final result and every sync_playwright()
    on a worker thread, never the caller thread."""
    worker = BrowserWorker()
    old_ident = worker.thread_ident
    assert old_ident is not None
    with _SwappedGlobals(worker):
        _Round3CM.first_start_error = Exception(ROUND3_POISON)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = run_browser_action(
                    "round3-login", "goto",
                    {"url": "https://accounts.google.com/"},
                    base_dir=Path(tmp))
            finally:
                _drop_session("round3-login")
            idents = list(_Round3CM.start_idents)
    assert result["ok"] is True, result
    assert "asyncio" not in json.dumps(result), result
    assert result["detail"]["url"] == "https://accounts.google.com/", result
    assert result["detail"]["title"] == "Round3 Page", result
    # The poisoned thread was abandoned for a fresh one.
    assert worker.thread_ident != old_ident, \
        (worker.thread_ident, old_ident)
    # The poisoned thread was tried, then the fresh thread.
    assert idents == [old_ident, worker.thread_ident], idents
    for ident in idents:
        assert ident != threading.get_ident(), ident


def t_round3_dead_driver_then_poison():
    """Two-stage 17:45 chain: a driver dies mid-call
    (TargetClosedError, which poisons the thread), then the
    recovery's fresh session start hits the poison. The combined
    dead-marker retry + thread-heal must still end in ok:true with
    no asyncio text."""
    worker = BrowserWorker()
    with _SwappedGlobals(worker):
        _Round3CM.first_start_error = Exception(
            "TargetClosedError: Playwright connection closed")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = run_browser_action(
                    "round3-dead", "goto",
                    {"url": "https://accounts.google.com/"},
                    base_dir=Path(tmp))
            finally:
                _drop_session("round3-dead")
    assert result["ok"] is True, result
    assert "asyncio" not in json.dumps(result), result
    assert result["detail"]["title"] == "Round3 Page", result


def main():
    tests = [
        ("worker-single-thread", t_worker_single_thread),
        ("worker-exception-propagates", t_worker_exception_propagates),
        ("worker-nested-inline-no-deadlock", t_worker_nested_inline_no_deadlock),
        ("fake-session-persists-across-caller-threads",
         t_fake_session_persists_across_caller_threads),
        ("poison-text-classified-dead", t_poison_text_classified_dead),
        ("worker-heals-poisoned-thread", t_worker_heals_poisoned_thread),
        ("worker-no-heal-on-ordinary-error",
         t_worker_no_heal_on_ordinary_error),
        ("recovery-asyncio-loop-poison-fake",
         t_recovery_asyncio_loop_poison_fake),
        ("act-reraises-poison-error", t_act_reraises_poison_error),
        ("act-still-swallows-ordinary-errors",
         t_act_still_swallows_ordinary_errors),
        ("round3-fresh-copy-poisoned-thread",
         t_round3_fresh_copy_poisoned_thread),
        ("round3-dead-driver-then-poison",
         t_round3_dead_driver_then_poison),
        ("recovery-dead-page-real", t_recovery_dead_page_real),
        ("http-persists-across-requests-real",
         t_http_persists_across_requests_real),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(PASSED)} passed, {len(SKIPPED)} skipped, {len(FAILED)} failed")
    if SKIPPED:
        print("skipped: " + ", ".join(SKIPPED))
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
