"""One live browser session bound to one persistent profile directory.

Uses a persistent Chromium context, so cookies and sessions stored in
the profile directory survive across sessions. Sign-in is never
automated here and no password is ever stored: the owner signs in
herself through the browser, and the profile keeps the session.

Lazy import: Playwright is imported inside ``start()`` only, so
importing this module never crashes on a host without Playwright.
If Playwright or Chromium is absent, ``start()`` raises
``BrowserNotAvailable`` with the real error, and ``act()`` returns
that error as a failure, never as a fake page state.
"""

import time
from pathlib import Path

from .actions import _ACTIONS


class BrowserNotAvailable(Exception):
    """Playwright or Chromium is missing, or Chromium failed to launch."""


def playwright_available():
    """True only if the playwright package imports."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _new_page_context():
    """Return sync_playwright() context manager, or raise honestly."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserNotAvailable(
            "PLAYWRIGHT-ABSENT: the playwright package is not installed "
            "on this host. Install it (pip install playwright) and run "
            "`playwright install chromium` first."
        ) from exc
    return sync_playwright()


class BrowserSession:
    """One live browser session bound to one profile directory."""

    def __init__(self, profile_dir, headless=True, viewport=None,
                 args=None):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.viewport = viewport or {"width": 1280, "height": 800}
        # Extra Chromium flags (e.g. "--proxy-server=..."); the two
        # sandbox/memory flags below are always applied as well.
        self.extra_args = list(args) if args else []
        self.timings = []  # [{"action", "ms", "ok"}] — measured only
        self._pw = None
        self._ctx = None
        self._page = None

    def start(self):
        """Launch the persistent context. Raises BrowserNotAvailable
        (not a fake page) when Playwright/Chromium is missing."""
        if self._page is not None:
            return self._page
        pw_cm = _new_page_context()
        self._pw = pw_cm.start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport=self.viewport,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
                + self.extra_args,
            )
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise BrowserNotAvailable(
                "BROWSER-ABSENT: Chromium could not be launched "
                f"({exc}). Run `playwright install chromium` first."
            ) from exc
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    def page(self):
        if self._page is None:
            return self.start()
        return self._page

    def close(self):
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._ctx = None
            self._page = None
            self._pw = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def act(self, action, **kw):
        """Run one action, timed. Returns a real result dict with
        measured milliseconds in ``ms`` and the real error in
        ``detail`` on failure."""
        t0 = time.perf_counter()
        try:
            detail = _ACTIONS[action](self, **kw)
            ok = True
        except KeyError:
            detail = {"error": f"unknown browser action: {action!r}"}
            ok = False
        except BrowserNotAvailable as exc:
            detail = {"error": str(exc)}
            ok = False
        except Exception as exc:  # Playwright errors: timeout, closed, ...
            detail = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False
        ms = round((time.perf_counter() - t0) * 1000, 1)
        result = {"ok": ok, "action": action, "ms": ms, "detail": detail}
        self.timings.append({"action": action, "ms": ms, "ok": ok})
        return result
