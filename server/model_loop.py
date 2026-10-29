"""Model loop for Agent Ultra 2: messages plus one browser tool.

Rules enforced here:

  - The caller's ``messages`` are passed to the backend UNCHANGED:
    nothing is inserted into them and the caller's list is never
    mutated (a copy is used for the working thread). Only the
    assistant and tool messages produced by the tool rounds are
    appended to the working copy.
  - Exactly ONE tool is offered, ``browser_open``, carrying name
    plus parameter schema ONLY. No other key is present on the
    tool definition, and no model-facing text is written anywhere
    in this module.
  - A ``browser_open`` call runs a real ``BrowserSession`` goto
    and feeds the real, measured result back as a tool message.
    At most 3 tool rounds run.
  - Any other tool name is NOT executed. The tool message for it
    says so honestly.
  - If the backend is not configured, the reply is exactly
    ``"model not live yet"`` -- never a fabricated reply.
"""

import json

from browser.profile import DEFAULT_BASE_DIR, profile_dir_for
from browser.session import BrowserSession
from browser.worker import get_worker
from server import model_client

MAX_TOOL_ROUNDS = 3

BROWSER_OPEN_TOOL = {
    "type": "function",
    "function": {
        "name": "browser_open",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
}

NOT_LIVE = "model not live yet"


def _default_session_factory():
    return BrowserSession(
        profile_dir_for(DEFAULT_BASE_DIR, "model-loop"),
        headless=True,
    )


def _tool_result_for_unknown(name):
    return {"ok": False, "error": f"unknown tool {name!r}: not executed"}


def run(messages, session_factory=None):
    """Run one user thread through the backend with the browser tool.

    ``messages`` is a list of ``{"role", "content"}`` dicts (tool
    messages are appended by the loop itself on later rounds).
    ``session_factory`` is an injection point for tests; when it is
    None a real ``BrowserSession`` is created lazily on the first
    ``browser_open`` call and closed when the run ends.

    Returns a dict::

        {"reply": str, "live": bool, "tool_rounds": int,
         "browser_results": [real BrowserSession.act results],
         "error": str | None}

    ``reply`` is the backend's final content, or an honest string
    when there is no backend, no content, or the tool rounds run
    out. ``browser_results`` holds only results from real goto
    calls (or real failures from them).
    """
    if not model_client.health()["live"]:
        return {
            "reply": NOT_LIVE,
            "live": False,
            "tool_rounds": 0,
            "browser_results": [],
            "error": None,
        }
    working = list(messages)
    browser_results = []
    tool_rounds = 0
    session = None
    session_owned = False
    error = None
    reply = None
    try:
        while True:
            try:
                response = model_client.chat(
                    working, tools=[BROWSER_OPEN_TOOL])
            except RuntimeError as exc:
                if str(exc) == NOT_LIVE:
                    return {
                        "reply": NOT_LIVE,
                        "live": False,
                        "tool_rounds": tool_rounds,
                        "browser_results": browser_results,
                        "error": None,
                    }
                error = str(exc)
                reply = f"model request failed: {exc}"
                break
            content = response.get("content")
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                if isinstance(content, str) and content:
                    reply = content
                else:
                    reply = "model returned no content"
                break
            if tool_rounds >= MAX_TOOL_ROUNDS:
                # Cap reached: the extra tool call is NOT executed.
                reply = "tool rounds exhausted without a final reply"
                break
            # One tool round: append the backend's own assistant
            # message verbatim, then one tool message per call.
            working.append(response.get("message") or {
                "role": "assistant", "content": content,
                "tool_calls": [],
            })
            tool_rounds += 1
            for call in tool_calls:
                name = call.get("name")
                args = call.get("arguments") or {}
                if name == "browser_open":
                    url = args.get("url") if isinstance(args, dict) else None
                    if not isinstance(url, str) or not url:
                        result = {"ok": False,
                                  "error": "browser_open needs a url string"}
                    else:
                        if session is None:
                            factory = session_factory or _default_session_factory
                            session = factory()
                            session_owned = session_factory is None
                        # The loop runs on an HTTP handler thread;
                        # Playwright itself only ever runs on the
                        # dedicated browser worker thread.
                        result = get_worker().submit(
                            lambda: session.act("goto", url=url))
                        browser_results.append(result)
                else:
                    result = _tool_result_for_unknown(name)
                working.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result),
                })
    finally:
        if session is not None and session_owned:
            try:
                get_worker().submit(session.close, timeout=30)
            except Exception:
                pass
    return {
        "reply": reply,
        "live": True,
        "tool_rounds": tool_rounds,
        "browser_results": browser_results,
        "error": error,
    }
