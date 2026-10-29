#!/usr/bin/env python3
"""Tests for the agent-ultra-2 browser package.

Run:  python3 tests/test_browser.py
Stdlib only. Exit 0 when every non-skipped test passes.

Unit tests use fake page/session objects — no real browser needed.
One real-browser test runs ONLY when Playwright and a Chromium
binary are actually present in this interpreter; otherwise it is
reported as SKIPPED with the real reason, never faked as a pass.
"""

import base64
import glob
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from browser import (  # noqa: E402
    BrowserNotAvailable,
    BrowserSession,
    CDPClient,
    Screencast,
    dispatch_browser_action,
    list_browser_tools,
    playwright_available,
)
from browser.profile import BrowserProfile, profile_dir_for, safe_copy_id  # noqa: E402

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


def skip(name, reason):
    raise _Skip(reason)


# ---------------------------------------------------------------- fakes
class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.value = ""
        self.checked = False

    def click(self, timeout=None):
        self.page.calls.append(("click", self.selector))
        if self.selector == "timeout":
            raise TimeoutError("locator.click: Timeout 2000ms exceeded")

    def dblclick(self, timeout=None):
        self.page.calls.append(("dblclick", self.selector))

    def fill(self, text, timeout=None):
        self.value = text
        self.page.calls.append(("fill", self.selector, text))

    def hover(self, timeout=None):
        self.page.calls.append(("hover", self.selector))

    def wait_for(self, state="visible", timeout=None):
        self.page.calls.append(("wait_for", self.selector, state))

    def inner_text(self, timeout=None):
        return f"text of {self.selector}"

    def text_content(self, timeout=None):
        return f"content of {self.selector}"

    def input_value(self):
        return self.value

    def select_option(self, value=None, label=None, timeout=None):
        picked = value if value is not None else label
        return [picked]

    def check(self, timeout=None):
        self.checked = True

    def uncheck(self, timeout=None):
        self.checked = False

    def is_checked(self):
        return self.checked

    def focus(self, timeout=None):
        self.page.calls.append(("focus", self.selector))


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.calls.append(("mouse.click", x, y))

    def dblclick(self, x, y):
        self.page.calls.append(("mouse.dblclick", x, y))

    def move(self, x, y, steps=None):
        self.page.calls.append(("mouse.move", x, y))

    def wheel(self, dx, dy):
        self.page.scroll_y = max(0, self.page.scroll_y + dy)
        self.page.calls.append(("mouse.wheel", dx, dy))

    def down(self):
        self.page.calls.append(("mouse.down",))

    def up(self):
        self.page.calls.append(("mouse.up",))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text):
        self.page.calls.append(("keyboard.type", text))

    def press(self, key):
        self.page.calls.append(("keyboard.press", key))


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self._title = "blank"
        self.calls = []
        self.scroll_y = 0
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)
        self._locators = {}

    def title(self):
        return self._title

    def locator(self, selector):
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self, selector)
        return self._locators[selector]

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self._title = "Example Domain" if "example" in url else f"title of {url}"

    def go_back(self, wait_until=None):
        self.url = "https://back.example/"

    def go_forward(self, wait_until=None):
        self.url = "https://forward.example/"

    def reload(self, wait_until=None):
        pass

    def screenshot(self, path=None):
        data = b"\x89PNG\r\n\x1a\nFAKE-SCREENSHOT-BYTES"
        if path:
            Path(path).write_bytes(data)
        return data

    def inner_text(self, selector):
        return "body text here"

    def evaluate(self, expression):
        if expression == "window.scrollY":
            return self.scroll_y
        m = re.match(r"window\.scrollTo\(0, ([0-9.]+)\)", expression)
        if m:
            self.scroll_y = float(m.group(1))
            return None
        if expression == "navigator.clipboard.readText()":
            return "hello-clip"
        return f"eval:{expression}"


class FakeCDPSession:
    def __init__(self):
        self.sent = []
        self.handlers = {}
        self.detached = False

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\x89PNG-FAKE").decode()}
        if method == "Runtime.evaluate":
            return {"result": {"type": "number", "value": 42}}
        return {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def off(self, event, handler):
        self.handlers.pop(event, None)

    def detach(self):
        self.detached = True

    def emit(self, event, params):
        self.handlers[event](params)


def fake_session(tmp):
    """A real BrowserSession bound to a fake page (no launch)."""
    session = BrowserSession(tmp / "profile", headless=True)
    session._page = FakePage()
    return session


# ---------------------------------------------------------------- tests
def t_list_tools():
    tools = list_browser_tools()
    names = [t["tool"] for t in tools]
    assert len(tools) == 28, f"expected 28 tools, got {len(tools)}: {names}"
    for wanted in ("goto", "click", "wait_selector", "wait", "evaluate",
                   "screenshot", "get_url", "select_option"):
        assert wanted in names, wanted
    for t in tools:
        assert isinstance(t["params"], list), t


def t_dispatch_unknown_and_no_session():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        r = s.act("no_such_action")
        assert r["ok"] is False and "unknown browser action" in r["detail"]["error"]
        assert isinstance(r["ms"], float) and r["ms"] >= 0
        r2 = dispatch_browser_action(None, "goto", {"url": "https://x.example/"})
        assert r2["ok"] is False
        r3 = dispatch_browser_action(s, "get_title")
        assert r3["ok"] is True and r3["detail"]["title"] == "blank"


def t_goto_click_navigation():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        assert s.act("goto", url="https://example.com")["detail"]["title"] == "Example Domain"
        assert s.act("get_url")["detail"]["url"] == "https://example.com"
        # Regression: a selector action must NOT sleep (the old code
        # let the `wait` action handler shadow the timeout helper,
        # which made every selector call sleep ~1s).
        t0 = time.perf_counter()
        r = s.act("click", selector="a")
        assert time.perf_counter() - t0 < 0.5, "selector click slept"
        assert r["ok"] is True and ("click", "a") in s.page().calls
        r = s.act("click", x=10, y=20)
        assert r["ok"] is True and ("mouse.click", 10.0, 20.0) in s.page().calls
        r = s.act("click")
        assert r["ok"] is False and "selector or x+y" in r["detail"]["error"]
        assert s.act("dblclick", selector="a")["ok"] is True
        assert s.act("back")["detail"]["url"] == "https://back.example/"
        assert s.act("forward")["detail"]["url"] == "https://forward.example/"
        assert s.act("reload")["ok"] is True


def t_type_fill_press():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        s.act("goto", url="https://example.com")
        r = s.act("type", selector="#q", text="hello")
        assert r["ok"] is True and r["detail"]["filled"] == "hello"
        r = s.act("type", text="abc")
        assert r["detail"]["typed_chars"] == 3
        r = s.act("fill", selector="#q", text="world")
        assert r["detail"] == {"filled": True, "value": "world"}
        assert s.act("fill", text="x")["ok"] is False
        assert s.act("press", key="Enter")["detail"]["key"] == "Enter"


def t_scroll_move_hover_drag():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        assert s.act("scroll_down", amount=500)["detail"]["y"] == 500
        assert s.act("scroll_up", amount=200)["detail"]["y"] == 300
        assert s.act("scroll_to", y=42)["detail"]["y"] == 42
        assert s.act("move", x=5, y=6)["detail"] == {"x": 5, "y": 6}
        assert s.act("move")["ok"] is False
        assert s.act("hover", selector="a")["detail"] == {"hovered": True}
        assert s.act("hover", x=1, y=2)["ok"] is True
        r = s.act("drag", x1=0, y1=0, x2=10, y2=10)
        assert r["detail"] == {"from": [0.0, 0.0], "to": [10.0, 10.0]}


def t_copy_paste():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        r = s.act("copy", selector="p")
        assert r["ok"] is True and r["detail"]["text"] == "hello-clip"
        assert r["detail"]["copied_chars"] == len("hello-clip")
        s.act("fill", selector="#q", text="before")
        r = s.act("paste", selector="#q")
        assert r["ok"] is True and r["detail"]["pasted"] is True


def t_screenshot_wait_gettext():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        s = fake_session(tmp)
        out = tmp / "shot.png"
        r = s.act("screenshot", path=str(out))
        assert r["ok"] is True and r["detail"]["bytes"] > 0
        assert out.read_bytes().startswith(b"\x89PNG")
        r = s.act("wait_selector", selector="h1")
        assert r["detail"] == {"found": True, "selector": "h1"}
        assert s.act("wait_selector")["ok"] is False
        r = s.act("wait", ms=50)
        assert r["detail"] == {"waited_ms": 50}
        # Cap is verified with sleep patched, so the test does not
        # actually sleep 30 seconds.
        with mock.patch.object(time, "sleep") as slept:
            r = s.act("wait", ms=999999)
        assert r["detail"]["waited_ms"] == 30000
        slept.assert_called_once_with(30.0)
        assert s.act("get_text")["detail"]["text"] == "body text here"
        assert s.act("get_text", selector="h1")["detail"]["text"] == "text of h1"
        assert s.act("get_title")["detail"]["title"] == "blank"


def t_select_check_focus_evaluate():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        assert s.act("select_option", selector="select", value="a")["detail"] == {"selected": ["a"]}
        assert s.act("select_option", selector="select", label="B")["detail"] == {"selected": ["B"]}
        assert s.act("select_option", selector="select")["ok"] is False
        assert s.act("check", selector="#c")["detail"] == {"checked": True}
        assert s.act("uncheck", selector="#c")["detail"] == {"checked": False}
        assert s.act("focus", selector="#q")["detail"] == {"focused": True}
        assert s.act("evaluate", expression="1+1")["detail"]["value"] == "eval:1+1"


def t_failure_is_honest_and_timed():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(Path(tmp))
        r = s.act("click", selector="timeout")
        assert r["ok"] is False, r
        assert "TimeoutError" in r["detail"]["error"], r
        assert s.timings and s.timings[-1]["ok"] is False
        assert all(isinstance(t["ms"], float) and t["ms"] >= 0 for t in s.timings)


def t_profile():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        p = BrowserProfile("copy-7", base)
        assert p.path == base / "profiles" / "copy-7"
        assert p.path.exists() and p.meta_path.exists()
        assert p.status()["status"] == "no-profile-yet"
        assert safe_copy_id("../evil") == "evil"
        assert "/" not in safe_copy_id("a/b\\c")
        assert profile_dir_for(base, "copy-7") == p.path
        assert (base / "profiles" / "copy-7").exists()


def t_cdp_client():
    fake = FakeCDPSession()
    page = FakePage()
    client = CDPClient(page, cdp_session=fake)
    assert client.send("Page.enable") == {}
    assert client.enable_network() == {}
    assert client.enable_runtime() == {}
    assert client.page_navigate("https://example.com") == {}
    assert client.page_reload() == {}
    assert client.page_capture_screenshot() == b"\x89PNG-FAKE"
    assert client.runtime_evaluate("1+1")["result"]["value"] == 42
    methods = [m for m, _ in fake.sent]
    for m in ("Page.enable", "Network.enable", "Runtime.enable",
              "Page.navigate", "Page.reload", "Page.captureScreenshot",
              "Runtime.evaluate"):
        assert m in methods, m
    assert all(t["ok"] is True for t in client.timings)
    client.close()
    assert fake.detached is True


def t_screencast_fake():
    fake = FakeCDPSession()
    page = FakePage()
    cast = Screencast(page, cdp=CDPClient(page, cdp_session=fake))
    cast.start()
    assert any(m == "Page.startScreencast" for m, _ in fake.sent)
    frame_bytes = b"\xff\xd8\xff\xe0FAKE-JPEG"
    fake.emit("Page.screencastFrame", {
        "data": base64.b64encode(frame_bytes).decode(),
        "sessionId": "sess-1",
    })
    it = cast.frames(timeout=2)
    assert next(it) == frame_bytes
    assert any(m == "Page.screencastFrameAck" for m, _ in fake.sent)
    cast.stop()
    try:
        next(it)
        raise AssertionError("frames() should end after stop()")
    except StopIteration:
        pass
    assert any(m == "Page.stopScreencast" for m, _ in fake.sent)


def t_playwright_available_bool():
    assert isinstance(playwright_available(), bool)


def t_browser_absent_raises_when_no_playwright():
    if playwright_available():
        skip("browser-absent-raises",
             "playwright IS importable here; covered by the real test")
        return
    with tempfile.TemporaryDirectory() as tmp:
        s = BrowserSession(Path(tmp) / "profile")
        try:
            s.start()
        except BrowserNotAvailable as exc:
            assert "PLAYWRIGHT-ABSENT" in str(exc)
        else:
            raise AssertionError("start() should raise BrowserNotAvailable")
        r = s.act("goto", url="https://example.com")
        assert r["ok"] is False and "PLAYWRIGHT-ABSENT" in r["detail"]["error"]


def t_real_browser():
    """One real-browser test; runs only if Chromium is really here.

    NOTE on the page used: this VM reaches the internet through an
    authenticated egress proxy. Direct Chromium navigation fails
    with ERR_TUNNEL_CONNECTION_FAILED, and the proxy flag fails with
    ERR_NO_SUPPORTED_PROXIES, so an external page is not reachable
    from this VM's Chromium. The test therefore drives a REAL
    Chromium render of a local page (real launch, real click, real
    scroll, real PNG screenshot bytes) and separately checks that a
    goto to an unreachable host returns an honest failure, not a
    fake success. On a host with direct internet, the same actions
    against an external URL exercise the same code path.
    """
    if not playwright_available():
        skip("real-browser", "playwright package not importable "
             "in this interpreter (system python3)")
        return
    candidates = (
        glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        + glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/chrome"))
    )
    if not candidates:
        skip("real-browser", "no Chromium binary found under ~/.cache/ms-playwright")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        page_file = tmp / "page.html"
        page_file.write_text(
            "<html><head><title>Real Browser Page</title></head><body>"
            "<a id='jump' href='#bottom'>jump</a>"
            + "<p>filler</p>" * 200
            + "<div id='bottom'>bottom</div></body></html>",
            encoding="utf-8",
        )
        session = BrowserSession(tmp / "profile", headless=True)
        try:
            session.start()
            r = session.act("goto", url=page_file.as_uri())
            assert r["ok"] is True, r
            assert session.page().title() == "Real Browser Page"
            r = session.act("click", selector="#jump")
            assert r["ok"] is True, r
            assert session.page().url.endswith("#bottom"), session.page().url
            before = session.act("evaluate", expression="window.scrollY")
            assert before["ok"] is True, before
            r = session.act("scroll_down", amount=300)
            assert r["ok"] is True and r["detail"]["y"] > 0, r
            out = tmp / "real.png"
            r = session.act("screenshot", path=str(out))
            assert r["ok"] is True and r["detail"]["bytes"] > 0, r
            assert out.read_bytes().startswith(b"\x89PNG")
            # Real CDP screencast: one genuine JPEG frame, clean stop.
            cast = Screencast(session.page(),
                              cdp=CDPClient(session.page()))
            cast.start()
            it = cast.frames(timeout=10)
            frame = next(it)
            assert frame[:3] == b"\xff\xd8\xff" and len(frame) > 0, len(frame)
            cast.stop()
            try:
                next(it)
                raise AssertionError("frames() should end after stop()")
            except StopIteration:
                pass
            # Honest failure: unreachable host must not fake success.
            r = session.act("goto", url="https://nonexistent.invalid/",
                            timeout_ms=5000)
            assert r["ok"] is False and "error" in r["detail"], r
        finally:
            session.close()


def main():
    tests = [
        ("list-tools-28", t_list_tools),
        ("dispatch-unknown-and-no-session", t_dispatch_unknown_and_no_session),
        ("goto-click-navigation", t_goto_click_navigation),
        ("type-fill-press", t_type_fill_press),
        ("scroll-move-hover-drag", t_scroll_move_hover_drag),
        ("copy-paste", t_copy_paste),
        ("screenshot-wait-gettext", t_screenshot_wait_gettext),
        ("select-check-focus-evaluate", t_select_check_focus_evaluate),
        ("failure-honest-and-timed", t_failure_is_honest_and_timed),
        ("profile", t_profile),
        ("cdp-client", t_cdp_client),
        ("screencast-fake", t_screencast_fake),
        ("playwright-available-bool", t_playwright_available_bool),
        ("browser-absent-raises", t_browser_absent_raises_when_no_playwright),
        ("real-browser", t_real_browser),
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
