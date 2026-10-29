#!/usr/bin/env python3
"""End-to-end integration tests for Agent Ultra 2, via the server API.

Run:  python3 tests/test_integration.py
Run from the repo root. Stdlib only. Exit 0 when every
non-skipped test passes.

Flow proven here, all through HTTP on an ephemeral port:
health -> chat (honest not-live) -> tools -> arena run ->
the arena winner from the API response is the same winner found
in the arena's own reports.jsonl, and every copy reported.

The live-browser leg runs ONLY when Playwright and a Chromium
binary are really present in this interpreter; otherwise it is
reported as SKIPPED with the real reason, never faked as a pass.
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
from server.chat_server import create_server  # noqa: E402

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


BASE = None


def http_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as resp:
            return (resp.status, resp.read(),
                    resp.headers.get("Content-Type"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type")


def http_post(path, obj):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def t_end_to_end_arena_through_api():
    # 1. Health is real and ok.
    code, body, _ = http_get("/health")
    assert code == 200, code
    assert json.loads(body)["ok"] is True, body
    # 2. Chat is honestly not live with no backend configured.
    code, body = http_post("/api/chat", {
        "messages": [{"role": "user", "content": "hello"}]})
    assert code == 200, code
    assert json.loads(body)["reply"] == "model not live yet", body
    # 3. Tools list answers.
    code, body, _ = http_get("/api/tools")
    assert code == 200, code
    assert isinstance(json.loads(body)["tools"], list), body
    # 4. Arena runs through the API and its winner flows through.
    code, body = http_post("/api/arena/run", {
        "task_id": "integration-task", "copies": 4, "max_concurrent": 4})
    assert code == 200, code
    data = json.loads(body)
    assert data["reports"] == 4 and data["missing"] == [], data
    winner = data["winner"]
    assert winner is not None, data
    winner_id = winner.get("copy_id") or winner.get("worker_id")
    assert winner_id is not None, winner
    # The winner is the top comparison row, not just any report.
    assert data["comparison"][0]["worker_id"] == winner_id, data
    # 5. The same winner is in the arena's own reports file.
    reports = [json.loads(ln) for ln in
               Path(data["reports_file"]).read_text(encoding="utf-8")
               .splitlines() if ln.strip()]
    assert len(reports) == 4, reports
    by_id = {r.get("copy_id") or r.get("worker_id"): r for r in reports}
    assert winner_id in by_id, by_id
    assert by_id[winner_id]["status"] == winner["status"], by_id[winner_id]
    # 6. Orders were broadcast before reports: one order per copy.
    orders = [json.loads(ln) for ln in
              Path(data["orders_file"]).read_text(encoding="utf-8")
              .splitlines() if ln.strip()]
    assert len(orders) == 4, orders
    assert {o["copy_id"] for o in orders} == set(by_id), orders


def t_live_browser_through_api():
    """Live browser via the API: real Chromium, real page, real PNG.

    Skips honestly when Playwright or Chromium is absent here.
    """
    if not playwright_available():
        raise _Skip("playwright package not importable "
                    "in this interpreter (system python3)")
    candidates = (
        glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        + glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/chrome"))
    )
    if not candidates:
        raise _Skip("no Chromium binary found under ~/.cache/ms-playwright")
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "page.html"
        page_file.write_text(
            "<html><head><title>Integration Page</title></head>"
            "<body><h1>hello</h1></body></html>", encoding="utf-8")
        code, body = http_post("/api/browser/action", {
            "action": "goto", "args": {"url": page_file.as_uri()},
            "copy_id": "integration"})
        assert code == 200, code
        result = json.loads(body)
        assert result["ok"] is True, result
        assert result["detail"]["title"] == "Integration Page", result
        code, shot, ctype = http_get("/api/browser/screenshot")
        assert code == 200, code
        assert "image/png" in (ctype or ""), ctype
        assert shot.startswith(b"\x89PNG"), shot[:16]


def main():
    global BASE
    saved_url = os.environ.get("MODEL_API_URL")
    saved_model = os.environ.get("MODEL_ID")
    os.environ.pop("MODEL_API_URL", None)
    os.environ.pop("MODEL_ID", None)
    server = create_server(0, host="127.0.0.1")
    port = server.server_address[1]
    BASE = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name, fn in [
            ("end-to-end-arena-through-api", t_end_to_end_arena_through_api),
            ("live-browser-through-api", t_live_browser_through_api),
        ]:
            check(name, fn)
    finally:
        if saved_url is not None:
            os.environ["MODEL_API_URL"] = saved_url
        else:
            os.environ.pop("MODEL_API_URL", None)
        if saved_model is not None:
            os.environ["MODEL_ID"] = saved_model
        else:
            os.environ.pop("MODEL_ID", None)
        server.shutdown()
        server.server_close()
    print(f"\n{len(PASSED)} passed, {len(SKIPPED)} skipped, {len(FAILED)} failed")
    if SKIPPED:
        print("skipped: " + ", ".join(SKIPPED))
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
