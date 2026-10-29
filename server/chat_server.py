"""Stdlib HTTP server for Agent Ultra 2. No new dependencies.

Endpoints:

  GET  /health                 real health() from server/health.py
  GET  /ui                     chat page (plus /ui/style.css, /ui/app.js)
  GET  /api/browser/screenshot live PNG from the active session,
                               or honest 404 "no live browser session"
  POST /api/browser/action     {"action", "args", "copy_id"} on a
                               per-copy BrowserSession, created lazily
                               with that copy's own profile
  GET  /api/tools              registered tools (name + args only)
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
"""

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from browser import dispatch_browser_action
from browser.profile import DEFAULT_BASE_DIR, profile_dir_for, safe_copy_id
from browser.session import BrowserSession
from copies.arena import run_arena
from server.health import health as real_health
from server.model_loop import run as run_model_loop
from tools.registry import Registry, ToolRegistrationError

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


def _get_session(copy_id):
    """Return the session for one copy, creating it lazily."""
    copy_id = safe_copy_id(copy_id)
    with _sessions_lock:
        session = _sessions.get(copy_id)
        if session is None:
            session = BrowserSession(
                profile_dir_for(DEFAULT_BASE_DIR, copy_id),
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
        elif path == "/api/chat":
            self._handle_chat(body)
        elif path == "/api/arena/run":
            self._handle_arena(body)
        else:
            self._send_json(404, {"error": f"unknown route: {path}"})

    # ---------------------------------------------------------- handlers
    def _handle_screenshot(self):
        session = _active_session()
        page = getattr(session, "_page", None) if session is not None else None
        if session is None or page is None:
            self._send_text(404, "no live browser session")
            return
        try:
            shot = page.screenshot()
        except Exception as exc:  # real error, reported, not faked
            self._send_json(502, {
                "error": f"screenshot failed: {type(exc).__name__}: {exc}"})
            return
        if not isinstance(shot, (bytes, bytearray)) or not shot:
            self._send_text(404, "no live browser session")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(shot)))
        self.end_headers()
        self.wfile.write(bytes(shot))

    def _handle_tools(self):
        try:
            listed = Registry().list()
        except ToolRegistrationError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        tools = [{"tool": name, "arg_names": rec.get("arg_names", [])}
                 for name, rec in sorted(listed.items())]
        self._send_json(200, {"tools": tools})

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
        session = _get_session(copy_id)
        result = dispatch_browser_action(session, action, args)
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
