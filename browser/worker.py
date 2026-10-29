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

Poisoned thread: Playwright's sync API deliberately leaves its
asyncio loop marked as the calling thread's running loop between
sync calls (see wait_for_future in playwright's _greenlets.py).
When a session's driver dies mid-call (killed Chromium on a small
host), the next sync_playwright() on that thread can raise "It
looks like you are using Playwright Sync API inside the asyncio
loop" instead of launching a browser, and no later
sync_playwright() can ever succeed on that thread again. submit()
detects that exact error, abandons the poisoned thread for a
fresh one (each thread generation owns its own queue, so the
retired thread can never grab the retried work), and retries the
callable exactly once on the fresh thread.
"""

import queue
import threading

_DEFAULT_TIMEOUT = 120.0

# Error text meaning the worker thread's Playwright thread-state is
# poisoned (see the module docstring). Only this text triggers the
# abandon-thread-and-retry recovery; any other failure is returned
# to the caller unchanged, never retried here.
_POISON_MARKERS = ("inside the asyncio loop",)


def _is_poison_error(exc):
    """True when exc is Playwright's sync-in-asyncio-loop error."""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _POISON_MARKERS)


class BrowserWorker:
    """Owns one thread; every submitted callable runs on it."""

    def __init__(self):
        self._queue = queue.Queue()
        # RLock: _replace_thread() calls _start() while holding it.
        self._lock = threading.RLock()
        self._thread = None
        self._thread_ident = None
        # Bumped every time the worker thread is replaced. A thread
        # only ever drains the queue that was current when it
        # started, so a retired (poisoned) thread can never grab
        # work submitted after its replacement.
        self._generation = 0
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
        # This thread drains exactly one queue: the one that was
        # current when it started. After a _replace_thread(), new
        # submissions go to the new queue, so this thread can never
        # steal work meant for its replacement. Once retired and
        # drained, it exits; it is a daemon either way.
        my_queue = self._queue
        my_generation = self._generation
        while True:
            try:
                fn, box, done = my_queue.get(timeout=1.0)
            except queue.Empty:
                if my_generation != self._generation:
                    return
                continue
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

    def _replace_thread(self):
        """Abandon the current worker thread and start a fresh one.

        The old thread is a daemon: if it is stuck inside
        Playwright's greenlet machinery it is simply left alone and
        can never block process exit. It keeps draining the queue it
        started with until that queue is empty, then exits; the new
        queue belongs to the new thread alone. Must not be called
        from the worker thread itself.
        """
        with self._lock:
            self._generation += 1
            self._queue = queue.Queue()
            self._thread = None
            self._thread_ident = None
            self._start()

    def _submit_once(self, fn, timeout):
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

    def submit(self, fn, timeout=_DEFAULT_TIMEOUT):
        """Run fn on the browser thread; return its value or raise
        its real exception. From the browser thread itself, fn runs
        inline instead of deadlocking.

        If fn raises Playwright's sync-in-asyncio-loop error, the
        worker thread is poisoned: it is abandoned for a fresh
        thread and fn is retried exactly once there. A poisoned
        thread can never recover on its own, so one retry on a
        clean thread is the fix, not a loop.
        """
        if threading.get_ident() == self._thread_ident:
            return fn()
        try:
            return self._submit_once(fn, timeout)
        except Exception as exc:
            if _is_poison_error(exc) and \
                    threading.get_ident() != self._thread_ident:
                self._replace_thread()
                return self._submit_once(fn, timeout)
            raise


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
