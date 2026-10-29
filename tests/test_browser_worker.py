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

Real-browser tests run ONLY when Playwright and a Chromium
binary are actually present in this interpreter; otherwise they
are reported as SKIPPED with the real reason, never faked.
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
from browser.session import BrowserSession  # noqa: E402
from browser.worker import BrowserWorker, get_worker  # noqa: E402
from server.chat_server import (  # noqa: E402
    _drop_session,
    _get_session,
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


def skip(reason):
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


def main():
    tests = [
        ("worker-single-thread", t_worker_single_thread),
        ("worker-exception-propagates", t_worker_exception_propagates),
        ("worker-nested-inline-no-deadlock", t_worker_nested_inline_no_deadlock),
        ("fake-session-persists-across-caller-threads",
         t_fake_session_persists_across_caller_threads),
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
