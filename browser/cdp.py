"""Direct CDP (Chrome DevTools Protocol) control via Playwright.

A CDP session is opened on a live Playwright page with
``page.context.new_cdp_session(page)``. No dependency beyond
Playwright is used. ``send()`` returns the raw CDP result and records
the measured milliseconds in ``timings``; helpers for the Page,
Network, and Runtime domains wrap the calls a later panel or
controller needs, including a screenshot captured through CDP.

No cookie, token, or credential values are read or returned by any
helper here.
"""

import base64
import time


class CDPClient:
    """Direct CDP control for one Playwright page."""

    def __init__(self, page, cdp_session=None):
        self.page = page
        self._session = cdp_session
        self.timings = []  # [{"method", "ms", "ok"}] — measured only

    def start(self):
        """Open the CDP session if one was not supplied."""
        if self._session is None:
            self._session = self.page.context.new_cdp_session(self.page)
        return self._session

    def session(self):
        if self._session is None:
            return self.start()
        return self._session

    def send(self, method, params=None):
        """Send one CDP command; return the raw CDP result dict.

        Raises the real CDP/Playwright error on failure; timings are
        recorded either way.
        """
        t0 = time.perf_counter()
        try:
            result = self.session().send(method, params or {})
            ok = True
        except Exception:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            self.timings.append({"method": method, "ms": ms, "ok": False})
            raise
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.timings.append({"method": method, "ms": ms, "ok": ok})
        return result

    def close(self):
        """Detach the CDP session. The page itself is left open;
        closing the page/context is the session's job."""
        sess, self._session = self._session, None
        if sess is not None:
            detach = getattr(sess, "detach", None)
            if callable(detach):
                detach()

    # ------------------------------------------------ Page domain
    def enable_page(self):
        return self.send("Page.enable")

    def disable_page(self):
        return self.send("Page.disable")

    def page_navigate(self, url):
        return self.send("Page.navigate", {"url": url})

    def page_reload(self, ignore_cache=False):
        return self.send("Page.reload", {"ignoreCache": bool(ignore_cache)})

    def page_capture_screenshot(self, format="png", quality=None):
        """Capture via CDP; returns real image bytes."""
        params = {"format": format}
        if format == "jpeg" and quality is not None:
            params["quality"] = int(quality)
        result = self.send("Page.captureScreenshot", params)
        return base64.b64decode(result["data"])

    # ------------------------------------------------ Network domain
    def enable_network(self):
        return self.send("Network.enable")

    def disable_network(self):
        return self.send("Network.disable")

    def set_cache_disabled(self, disabled=True):
        return self.send("Network.setCacheDisabled",
                         {"cacheDisabled": bool(disabled)})

    # ------------------------------------------------ Runtime domain
    def enable_runtime(self):
        return self.send("Runtime.enable")

    def disable_runtime(self):
        return self.send("Runtime.disable")

    def runtime_evaluate(self, expression, return_by_value=True):
        """Evaluate a JS expression via Runtime; returns the CDP
        result object (``result``/``exceptionDetails`` as returned)."""
        return self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": bool(return_by_value),
        })

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False
