"""Stdlib HTTP server for Agent Ultra 2. No new dependencies.

Endpoints:

  GET  /health                 real health() from server/health.py
  GET  /ui                     chat page (plus /ui/style.css, /ui/app.js)
  GET  /api/browser/screenshot live PNG from the active session
                               (or ?copy=<id> for one copy),
                               or honest 404 "no live browser session"
  GET  /api/browser/status    session list + active copy liveness,
                               probed for real on the browser thread
                               (?copy=<id> pins the probe to one copy)
  POST /api/browser/action     {"action", "args", "copy_id"} on a
                               per-copy BrowserSession, created lazily
                               with that copy's own profile
  GET  /api/tools              registered tools (name + args only)
  POST /api/shell              {"command", "timeout_s"} runs one bash
                               command in the pinned work directory
                               and returns the real result
  POST /api/chat               {"messages"} through the model loop,
                               or the honest not-live reply
  POST /api/arena/run          {"task_id", "copies", "max_concurrent"}
                               runs the arena; small defaults for API
                               use, full 100 allowed via waves

Browser sessions are held in memory, one per copy id, each bound
to its own profile directory under the cache base. Nothing about
a profile (cookies, tokens, or any stored values) is ever returned
by any endpoint. A screenshot is served only when a session has a
real open page; otherwise the endpoint answers 404, never a
placeholder image.

THREAD OWNERSHIP (fix for the deployed session bug): Playwright's
sync API is bound to the thread that created it, and the HTTP
server is threaded, so a session created by one request thread
broke on the next request thread (goto worked, then every
screenshot and action failed with a dead connection). Every
Playwright call here therefore runs on the single browser worker
thread (browser/worker.py); HTTP threads never touch Playwright
objects directly. If a session's connection dies anyway (killed
Chromium on a small host), the action path drops the dead session
and retries exactly once on a fresh session; the screenshot path
drops it and reports the real error, so the next Open rebuilds.
A further failure mode is a poisoned worker thread: when a driver
dies mid-call, Playwright's thread-local loop state can make the
next sync_playwright() raise its sync-in-asyncio-loop error on
that thread. That text is a dead-session marker (drop + retry
once), and the worker itself abandons the poisoned thread for a
fresh one and retries the call there exactly once.
"""

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from browser import dispatch_browser_action
from browser.profile import DEFAULT_BASE_DIR, profile_dir_for, safe_copy_id
from browser.session import BrowserSession
from browser.worker import get_worker
from copies.arena import run_arena
from server.health import health as real_health
from server.model_loop import run as run_model_loop
from tools.registry import Registry, ToolRegistrationError
from tools.shell_tool import run_shell

_UI_DIR = Path(__file__).resolve().parent / "ui"
_UI_FILES = {
    "/ui": ("index.html", "text/html"),
    "/ui/index.html": ("index.html", "text/html"),
    "/ui/style.css": ("style.css", "text/css"),
    "/ui/app.js": ("app.js", "application/javascript"),
}

_sessions = {}
_sessions_lock = threading.Lock()
_last_copy_id = {"copy_id": None}
_PROFILE_BASE_DIR = DEFAULT_BASE_DIR


def _get_session(copy_id, base_dir=None):
    """Return the session object for one copy, creating it lazily.

    Creating the object touches no Playwright state (only the
    profile directory); starting the browser happens on the
    browser worker thread when an action runs.
    """
    copy_id = safe_copy_id(copy_id)
    base = Path(base_dir) if base_dir is not None else _PROFILE_BASE_DIR
    with _sessions_lock:
        session = _sessions.get(copy_id)
        if session is None:
            session = BrowserSession(
                profile_dir_for(base, copy_id),
                headless=True,
            )
            _sessions[copy_id] = session
        _last_copy_id["copy_id"] = copy_id
        return session


def _active_session():
    with _sessions_lock:
        copy_id = _last_copy_id["copy_id"]
        if copy_id is None:
            return None
        return _sessions.get(copy_id)


# Error text that means "this session's browser connection is dead"
# (killed Chromium, dead creating thread). Only these trigger the
# drop-and-retry recovery; any other failure is returned as-is.
_DEAD_MARKERS = (
    "TargetClosedError",
    "Playwright connection closed",
    "Cannot switch to a different thread",
    "Target page, context or browser has been closed",
    "Browser has been closed",
    # Playwright's sync API can also fail with its sync-in-asyncio
    # error when a dead driver left the thread's loop state
    # poisoned (seen live 2026-10-29: a goto on a stale session
    # answered in 0.1ms with exactly this text). It means the
    # session is unusable, so it gets the same drop-and-retry
    # treatment; the worker thread itself self-heals separately
    # (browser/worker.py) when the error is raised through it.
    "inside the asyncio loop",
)


def _is_dead_error_text(text):
    text = text or ""
    return any(marker in text for marker in _DEAD_MARKERS)


def _is_dead_result(result):
    if not isinstance(result, dict) or result.get("ok") is not False:
        return False
    detail = result.get("detail") or {}
    error = detail.get("error") if isinstance(detail, dict) else None
    return isinstance(error, str) and _is_dead_error_text(error)


def _drop_session(copy_id):
    """Remove a session and close it on the browser thread,
    best-effort, freeing its Chromium before any relaunch."""
    copy_id = safe_copy_id(copy_id)
    with _sessions_lock:
        session = _sessions.pop(copy_id, None)
    if session is not None:
        try:
            get_worker().submit(session.close, timeout=30)
        except Exception:
            pass


def run_browser_action(copy_id, action, args, base_dir=None):
    """Run one browser action on the browser worker thread.

    The session is created lazily. If the action fails because the
    browser connection is dead, the dead session is dropped and
    the action is retried exactly once on a fresh session. Any
    other failure is returned as-is, never retried: a retry after
    a dead connection can repeat an action that partially landed,
    so one retry is the limit.
    """
    worker = get_worker()
    session = _get_session(copy_id, base_dir=base_dir)
    result = worker.submit(
        lambda: dispatch_browser_action(session, action, args))
    if _is_dead_result(result):
        _drop_session(copy_id)
        session = _get_session(copy_id, base_dir=base_dir)
        result = worker.submit(
            lambda: dispatch_browser_action(session, action, args))
    return result


def create_server(port, host="0.0.0.0"):
    """Build (but do not start) the HTTP server."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def serve(port, host="0.0.0.0"):
    """Serve forever on the given port."""
    server = create_server(port, host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentUltra2/1.0"

    # ---------------------------------------------------------- helpers
    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, code, text, ctype="text/plain"):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            size = int(length)
        except ValueError:
            raise ValueError("invalid Content-Length")
        raw = self.rfile.read(size) if size > 0 else b""
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def log_message(self, fmt, *args):  # keep stdout clean
        pass

    # ---------------------------------------------------------- routing
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            result = real_health()
            self._send_json(200 if result["ok"] else 503, result)
        elif path in _UI_FILES:
            name, ctype = _UI_FILES[path]
            file_path = _UI_DIR / name
            if not file_path.is_file():
                self._send_text(404, "ui file not found")
            else:
                self._send_text(200, file_path.read_text(encoding="utf-8"),
                                ctype)
        elif path == "/":
            self.send_response(302)
            self.send_header("Location", "/ui")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/browser/screenshot":
            self._handle_screenshot()
        elif path == "/api/browser/status":
            self._handle_browser_status()
        elif path == "/api/tools":
            self._handle_tools()
        else:
            self._send_json(404, {"error": f"unknown route: {path}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        if path == "/api/browser/action":
            self._handle_browser_action(body)
        elif path == "/api/shell":
            self._handle_shell(body)
        elif path == "/api/chat":
            self._handle_chat(body)
        elif path == "/api/arena/run":
            self._handle_arena(body)
        else:
            self._send_json(404, {"error": f"unknown route: {path}"})

    # ---------------------------------------------------------- handlers
    def _handle_screenshot(self):
        # ?copy=<id> pins the screenshot to one copy; without it
        # the last copy used by an action is shown.
        query = parse_qs(urlsplit(self.path).query)
        wanted = query.get("copy", [None])[0]
        if wanted:
            with _sessions_lock:
                _last_copy_id["copy_id"] = safe_copy_id(wanted)
        session = _active_session()
        page = getattr(session, "_page", None) if session is not None else None
        if session is None or page is None:
            self._send_text(404, "no live browser session")
            return
        try:
            # The page object is only CALLED on the browser thread;
            # calling it here would break the thread binding.
            shot = get_worker().submit(lambda: page.screenshot(), timeout=30)
        except Exception as exc:  # real error, reported, not faked
            dropped = False
            if _is_dead_error_text(f"{type(exc).__name__}: {exc}"):
                with _sessions_lock:
                    doomed = _last_copy_id["copy_id"]
                if doomed is not None:
                    _drop_session(doomed)
                    dropped = True
            self._send_json(502, {
                "error": f"screenshot failed: {type(exc).__name__}: {exc}",
                "session_dropped": dropped})
            return
        if not isinstance(shot, (bytes, bytearray)) or not shot:
            self._send_text(404, "no live browser session")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(shot)))
        self.end_headers()
        self.wfile.write(bytes(shot))

    def _handle_browser_status(self):
        # ?copy=<id> pins the probe to one copy's session; without
        # it the last copy used by an action is probed (previous
        # behavior). The probe is read-only: it never mutates the
        # active copy.
        query = parse_qs(urlsplit(self.path).query)
        wanted = query.get("copy", [None])[0]
        with _sessions_lock:
            copy_ids = sorted(_sessions.keys())
            if wanted:
                target_id = safe_copy_id(wanted)
            else:
                target_id = _last_copy_id["copy_id"]
            session = _sessions.get(target_id) if target_id else None
            page = getattr(session, "_page", None) if session else None
        alive = None
        url = None
        title = None
        if page is not None:
            try:
                # evaluate("1") is a real driver round-trip: it
                # succeeds only while the connection is alive.
                get_worker().submit(lambda: page.evaluate("1"), timeout=30)
                alive = True
            except Exception:
                alive = False
            if alive:
                try:
                    url, title = get_worker().submit(
                        lambda: (page.url, page.title()), timeout=30)
                except Exception:
                    url, title = None, None
        self._send_json(200, {
            "sessions": copy_ids,
            "active_copy_id": target_id,
            "active_alive": alive,
            "active_url": url,
            "active_title": title,
        })

    def _handle_tools(self):
        try:
            listed = Registry().list()
        except ToolRegistrationError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        tools = [{"tool": name, "arg_names": rec.get("arg_names", [])}
                 for name, rec in sorted(listed.items())]
        self._send_json(200, {"tools": tools})

    def _handle_shell(self, body):
        command = body.get("command")
        timeout_s = body.get("timeout_s", 30)
        if not isinstance(command, str) or not command.strip():
            self._send_json(
                400, {"error": "command must be a non-empty string"})
            return
        if isinstance(timeout_s, bool) or not isinstance(
                timeout_s, (int, float)):
            self._send_json(400, {"error": "timeout_s must be a number"})
            return
        # Deterministic proof path: real bash in the pinned work
        # directory, no model involved.
        result = run_shell(command, timeout_s=timeout_s)
        self._send_json(200, result)

    def _handle_browser_action(self, body):
        action = body.get("action")
        args = body.get("args") or {}
        copy_id = body.get("copy_id") or "primary"
        if not isinstance(action, str) or not action:
            self._send_json(400, {"error": "action must be a non-empty string"})
            return
        if not isinstance(args, dict):
            self._send_json(400, {"error": "args must be an object"})
            return
        if not isinstance(copy_id, str) or not copy_id:
            self._send_json(400, {"error": "copy_id must be a non-empty string"})
            return
        result = run_browser_action(copy_id, action, args)
        self._send_json(200, result)

    def _handle_chat(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list):
            self._send_json(400, {"error": "messages must be a list"})
            return
        result = run_model_loop(messages)
        self._send_json(200, result)

    def _handle_arena(self, body):
        task_id = body.get("task_id") or "api-task"
        copies = body.get("copies", 4)
        max_concurrent = body.get("max_concurrent", 4)
        if not isinstance(task_id, str) or not task_id:
            self._send_json(400, {"error": "task_id must be a non-empty string"})
            return
        if not isinstance(copies, int) or isinstance(copies, bool) \
                or copies < 1:
            self._send_json(400, {"error": "copies must be an integer >= 1"})
            return
        if not isinstance(max_concurrent, int) \
                or isinstance(max_concurrent, bool) or max_concurrent < 1:
            self._send_json(
                400, {"error": "max_concurrent must be an integer >= 1"})
            return
        base_dir = tempfile.mkdtemp(prefix="agent-ultra-2-arena-")
        try:
            result = run_arena(task_id, base_dir, copies=copies,
                               max_concurrent=max_concurrent)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, result)
