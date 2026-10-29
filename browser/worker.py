"""Single-threaded browser worker.

Playwright's sync API is bound to the thread (greenlet) that
created it: a call from any other thread fails with "Cannot switch
to a different thread", and once the creating thread is gone the
connection degrades to "Playwright connection closed"
(TargetClosedError). The HTTP server is threaded, so a browser
session created by one request thread breaks on the next request
thread — goto works, then every screenshot and action fails.
Every browser touch must therefore run on ONE dedicated browser
thread. This module owns that thread: callers submit callables
and wait for the real result (or the real exception).

The worker thread is a daemon: it never blocks process exit. A
task that raises still wakes its waiter, and the loop keeps
running for the next task. submit() never deadlocks when called
from the worker thread itself: nested calls run inline. A submit
wait is bounded by its timeout; on timeout the caller gets a
TimeoutError while the worker keeps working on the stuck call.
"""

import queue
import threading

_DEFAULT_TIMEOUT = 120.0


class BrowserWorker:
    """Owns one thread; every submitted callable runs on it."""

    def __init__(self):
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self._thread_ident = None
        self._start()

    def _start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(target=self._loop, daemon=True,
                                      name="agent-ultra-2-browser")
            thread.start()
            self._thread = thread

    def _loop(self):
        self._thread_ident = threading.get_ident()
        while True:
            fn, box, done = self._queue.get()
            try:
                box["result"] = fn()
            except Exception as exc:  # noqa: BLE001 - re-raised to caller
                box["error"] = exc
            finally:
                done.set()

    @property
    def thread_ident(self):
        """Ident of the worker thread, once it has started."""
        return self._thread_ident

    def submit(self, fn, timeout=_DEFAULT_TIMEOUT):
        """Run fn on the browser thread; return its value or raise
        its real exception. From the browser thread itself, fn runs
        inline instead of deadlocking."""
        if threading.get_ident() == self._thread_ident:
            return fn()
        self._start()
        box = {}
        done = threading.Event()
        self._queue.put((fn, box, done))
        if not done.wait(timeout=timeout):
            raise TimeoutError(
                f"browser worker did not answer within {timeout}s")
        if "error" in box:
            raise box["error"]
        return box["result"]


_worker = None
_worker_lock = threading.Lock()


def get_worker():
    """Process-wide browser worker (created lazily)."""
    global _worker
    if _worker is None:
        with _worker_lock:
            if _worker is None:
                _worker = BrowserWorker()
    return _worker
