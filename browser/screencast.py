"""Live screen via CDP screencast.

Starts a Page screencast on a live Playwright page through CDP and
yields real JPEG frames. Each received frame is acknowledged back to
the browser (``Page.screencastFrameAck``), which is what keeps the
stream flowing. ``stop()`` ends the screencast cleanly and detaches
the listener; the page itself is left open.

This is what a later UI panel will stream. No frames are fabricated:
if the browser sends no frames, none are yielded.
"""

import base64
import queue
import threading
import time

from .cdp import CDPClient


class Screencast:
    """A live screencast stream from one Playwright page."""

    def __init__(self, page, cdp=None, format="jpeg", quality=80,
                 max_width=1280, max_height=800):
        self.page = page
        self.cdp = cdp if cdp is not None else CDPClient(page)
        self.format = format
        self.quality = int(quality)
        self.max_width = int(max_width)
        self.max_height = int(max_height)
        self._frames = queue.Queue()
        self._running = False
        self._started = False
        self._lock = threading.Lock()

    def _on_frame(self, params):
        try:
            data = base64.b64decode(params.get("data", ""))
            self._frames.put(data)
            self.cdp.send("Page.screencastFrameAck",
                          {"sessionId": params["sessionId"]})
        except Exception:
            # A bad frame must not kill the stream; it is dropped
            # and the next real frame is awaited instead.
            pass

    def start(self):
        """Start the screencast. Returns self; idempotent."""
        with self._lock:
            if self._started:
                return self
            session = self.cdp.session()
            on = getattr(session, "on", None)
            if callable(on):
                on("Page.screencastFrame", self._on_frame)
            params = {
                "format": self.format,
                "quality": self.quality,
                "maxWidth": self.max_width,
                "maxHeight": self.max_height,
            }
            self.cdp.send("Page.startScreencast", params)
            self._running = True
            self._started = True
            return self

    def _pump(self, pump_ms):
        """Let Playwright deliver pending CDP events.

        The sync Playwright API only dispatches CDP events while a
        Playwright call is in progress, so a plain blocking wait on
        the frame queue would starve the stream. A short
        ``wait_for_timeout`` gives the driver a chance to deliver.
        Pages without that method (fakes) simply skip pumping.
        """
        fn = getattr(self.page, "wait_for_timeout", None)
        if callable(fn):
            try:
                fn(pump_ms)
            except Exception:
                pass

    def frames(self, timeout=None, pump_ms=100):
        """Yield real JPEG frames until ``stop()`` is called.

        ``timeout`` (seconds) bounds each wait for the next frame;
        on timeout the generator ends. Frames already queued are
        still yielded first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self._running and self._frames.empty():
                return
            wait = pump_ms / 1000.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                wait = min(wait, remaining)
            try:
                frame = self._frames.get(timeout=wait)
            except queue.Empty:
                self._pump(pump_ms)
                continue
            if frame is None:  # stop sentinel
                return
            yield frame

    def stop(self):
        """Stop the screencast cleanly. Idempotent."""
        with self._lock:
            if not self._started:
                return
            self._running = False
            try:
                self.cdp.send("Page.stopScreencast")
            except Exception:
                pass  # page already gone: nothing left to stop
            session = self.cdp.session() if self.cdp._session else None
            if session is not None:
                off = getattr(session, "off", None)
                if callable(off):
                    try:
                        off("Page.screencastFrame", self._on_frame)
                    except Exception:
                        pass
            self._frames.put(None)
            self._started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def start_screencast(page, cdp=None, **kw):
    """Start a screencast on ``page`` and return the Screencast."""
    return Screencast(page, cdp=cdp, **kw).start()
