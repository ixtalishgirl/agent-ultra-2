"""Browser actions for Agent Ultra 2 (Playwright, sync API).

Adapted from the Agent-Ultra browser action set: 28 canonical actions.
Every handler operates on a session-like object exposing ``page()`` and
returns a plain dict of what actually happened. Timing and ok/fail
wrapping live in :meth:`BrowserSession.act` (see ``session.py``):
every result there carries measured milliseconds, and every failure
carries the real error, never a fabricated success.

Mechanics, no artificial delays: selector waits are bounded
(default 2000 ms, overridable per call) and navigation uses
``wait_until="domcontentloaded"``. The 1-2 s per-action target is a
target, not a guarantee: page load and network time are the site's
and the network's, and the measured milliseconds in each result show
the real number.

Blocks (Cloudflare, captcha, bot checks): an action against a blocked
page behaves like a normal browser attempt. A block surfaces as the
real error or the real page state returned by the action; no bypass
is attempted and none is claimed.

Importing this module never imports Playwright, so it is safe to
import on a host without Playwright installed.
"""

import time
from pathlib import Path

DEFAULT_WAIT_MS = 2000
DEFAULT_NAV_TIMEOUT_MS = 15000
DEFAULT_GOTO_WAIT = "domcontentloaded"
MAX_WAIT_MS = 30000


def _timeout_ms(kw):
    return int(kw.get("timeout_ms", DEFAULT_WAIT_MS))


def _loc(page, kw):
    """Locator for ``selector``, or None for coordinate-based calls."""
    sel = kw.get("selector")
    return page.locator(sel) if sel else None


def _coords(kw):
    return kw.get("x"), kw.get("y")


def _goto(s, **kw):
    url = kw["url"]
    timeout = int(kw.get("timeout_ms", DEFAULT_NAV_TIMEOUT_MS))
    s.page().goto(url, wait_until=DEFAULT_GOTO_WAIT, timeout=timeout)
    return {"url": s.page().url, "title": s.page().title()}


def _click(s, **kw):
    page = s.page()
    loc, (x, y) = _loc(page, kw), _coords(kw)
    if loc is not None:
        loc.click(timeout=_timeout_ms(kw))
    elif x is not None and y is not None:
        page.mouse.click(float(x), float(y))
    else:
        raise ValueError("click needs selector or x+y")
    return {"url": page.url, "title": page.title()}


def _dblclick(s, **kw):
    page = s.page()
    loc, (x, y) = _loc(page, kw), _coords(kw)
    if loc is not None:
        loc.dblclick(timeout=_timeout_ms(kw))
    elif x is not None and y is not None:
        page.mouse.dblclick(float(x), float(y))
    else:
        raise ValueError("dblclick needs selector or x+y")
    return {"url": page.url}


def _type(s, **kw):
    page = s.page()
    text = kw.get("text", "")
    loc = _loc(page, kw)
    if loc is not None:
        loc.click(timeout=_timeout_ms(kw))
        loc.fill(text, timeout=_timeout_ms(kw))
        return {"filled": text, "url": page.url}
    page.keyboard.type(text)
    return {"typed_chars": len(text), "url": page.url}


def _fill(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("fill needs selector")
    loc.fill(kw.get("text", ""), timeout=_timeout_ms(kw))
    return {"filled": True, "value": loc.input_value()}


def _press(s, **kw):
    s.page().keyboard.press(kw["key"])
    return {"key": kw["key"], "url": s.page().url}


def _scroll_down(s, **kw):
    amount = float(kw.get("amount", 600))
    s.page().mouse.wheel(0, amount)
    return {"scrolled": amount, "y": s.page().evaluate("window.scrollY")}


def _scroll_up(s, **kw):
    amount = float(kw.get("amount", 600))
    s.page().mouse.wheel(0, -amount)
    return {"scrolled": -amount, "y": s.page().evaluate("window.scrollY")}


def _scroll_to(s, **kw):
    y = float(kw.get("y", 0))
    s.page().evaluate(f"window.scrollTo(0, {y})")
    return {"y": s.page().evaluate("window.scrollY")}


def _move(s, **kw):
    x, y = _coords(kw)
    if x is None or y is None:
        raise ValueError("move needs x+y")
    s.page().mouse.move(float(x), float(y))
    return {"x": x, "y": y}


def _hover(s, **kw):
    page = s.page()
    loc, (x, y) = _loc(page, kw), _coords(kw)
    if loc is not None:
        loc.hover(timeout=_timeout_ms(kw))
    elif x is not None and y is not None:
        page.mouse.move(float(x), float(y))
    else:
        raise ValueError("hover needs selector or x+y")
    return {"hovered": True}


def _drag(s, **kw):
    page = s.page()
    x1, y1 = float(kw["x1"]), float(kw["y1"])
    x2, y2 = float(kw["x2"]), float(kw["y2"])
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move(x2, y2, steps=5)
    page.mouse.up()
    return {"from": [x1, y1], "to": [x2, y2]}


def _copy(s, **kw):
    """Copy, then read the clipboard back to verify. A denied
    clipboard read is reported as unverified, not as a success."""
    page = s.page()
    loc, (x, y) = _loc(page, kw), _coords(kw)
    if loc is not None:
        loc.click(timeout=_timeout_ms(kw))
        page.keyboard.press("Control+A")
    elif x is not None and y is not None:
        page.mouse.click(float(x), float(y))
        page.keyboard.press("Control+A")
    else:
        page.keyboard.press("Control+A")
    page.keyboard.press("Control+C")
    try:
        text = page.evaluate("navigator.clipboard.readText()")
        return {"copied_chars": len(text or ""), "text": (text or "")[:500]}
    except Exception as exc:
        return {
            "copied": "unverified",
            "note": "clipboard read was denied by the browser; "
                    "Control+C was sent but the content could not be "
                    f"read back ({type(exc).__name__}).",
        }


def _paste(s, **kw):
    """Paste into a focused element; read the element value back when
    it is an input/textarea, so the paste is verified, not assumed."""
    page = s.page()
    loc, (x, y) = _loc(page, kw), _coords(kw)
    if loc is not None:
        loc.click(timeout=_timeout_ms(kw))
    elif x is not None and y is not None:
        page.mouse.click(float(x), float(y))
    page.keyboard.press("Control+V")
    detail = {"pasted": True}
    if loc is not None:
        try:
            detail["value_after_paste"] = loc.input_value()
        except Exception:
            pass
    return detail


def _back(s, **kw):
    s.page().go_back(wait_until=DEFAULT_GOTO_WAIT)
    return {"url": s.page().url, "title": s.page().title()}


def _forward(s, **kw):
    s.page().go_forward(wait_until=DEFAULT_GOTO_WAIT)
    return {"url": s.page().url, "title": s.page().title()}


def _reload(s, **kw):
    s.page().reload(wait_until=DEFAULT_GOTO_WAIT)
    return {"url": s.page().url, "title": s.page().title()}


def _screenshot(s, **kw):
    page = s.page()
    out = Path(kw["path"]) if kw.get("path") else Path.cwd() / "screenshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    return {"path": str(out), "bytes": out.stat().st_size}


def _wait_selector(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("wait_selector needs selector")
    loc.wait_for(state=kw.get("state", "visible"), timeout=_timeout_ms(kw))
    return {"found": True, "selector": kw["selector"]}


def _wait(s, **kw):
    """Deliberate pause of ``ms`` milliseconds, capped at MAX_WAIT_MS.
    This is the only action that sleeps on purpose; the reported
    ``waited_ms`` is the capped value actually slept."""
    ms = int(kw.get("ms", 1000))
    ms = max(0, min(ms, MAX_WAIT_MS))
    time.sleep(ms / 1000.0)
    return {"waited_ms": ms}


def _get_text(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        return {"text": page.inner_text("body")[:5000]}
    try:
        return {"text": loc.inner_text(timeout=_timeout_ms(kw))}
    except Exception:
        return {"text": loc.text_content(timeout=_timeout_ms(kw))}


def _get_title(s, **kw):
    return {"title": s.page().title(), "url": s.page().url}


def _select_option(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("select_option needs selector")
    value = kw.get("value")
    label = kw.get("label")
    if value is not None:
        picked = loc.select_option(value=value, timeout=_timeout_ms(kw))
    elif label is not None:
        picked = loc.select_option(label=label, timeout=_timeout_ms(kw))
    else:
        raise ValueError("select_option needs value or label")
    return {"selected": picked}


def _check(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("check needs selector")
    loc.check(timeout=_timeout_ms(kw))
    return {"checked": loc.is_checked()}


def _uncheck(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("uncheck needs selector")
    loc.uncheck(timeout=_timeout_ms(kw))
    return {"checked": loc.is_checked()}


def _focus(s, **kw):
    page = s.page()
    loc = _loc(page, kw)
    if loc is None:
        raise ValueError("focus needs selector")
    loc.focus(timeout=_timeout_ms(kw))
    return {"focused": True}


def _evaluate(s, **kw):
    return {"value": s.page().evaluate(kw["expression"])}


# Canonical 28 actions. The task lists 27 names but says 28; the
# source set's 28th action is get_url, so it is included here to
# reconcile the count with the source. Aliases are separate, below.
_CANONICAL = [
    "goto", "click", "dblclick", "type", "fill", "press",
    "scroll_down", "scroll_up", "scroll_to", "move", "hover", "drag",
    "copy", "paste", "back", "forward", "reload", "screenshot",
    "wait_selector", "get_text", "get_title", "get_url", "select_option",
    "check", "uncheck", "focus", "evaluate", "wait",
]

assert len(_CANONICAL) == 28, _CANONICAL

_ACTIONS = {
    "goto": _goto,
    "click": _click,
    "dblclick": _dblclick,
    "type": _type,
    "fill": _fill,
    "press": _press,
    "scroll_down": _scroll_down,
    "scroll_up": _scroll_up,
    "scroll_to": _scroll_to,
    "move": _move,
    "hover": _hover,
    "drag": _drag,
    "copy": _copy,
    "paste": _paste,
    "back": _back,
    "forward": _forward,
    "reload": _reload,
    "screenshot": _screenshot,
    "wait_selector": _wait_selector,
    "get_text": _get_text,
    "get_title": _get_title,
    "get_url": lambda s, **kw: {"url": s.page().url},
    "select_option": _select_option,
    "check": _check,
    "uncheck": _uncheck,
    "focus": _focus,
    "evaluate": _evaluate,
    "wait": _wait,
    # Aliases accepted by dispatch, not counted in the canonical 28.
    "navigate": _goto,
    "wait_for_selector": _wait_selector,
}

_ACTION_PARAMS = {
    "goto": ["url"],
    "click": ["selector?", "x?", "y?"],
    "dblclick": ["selector?", "x?", "y?"],
    "type": ["selector?", "text"],
    "fill": ["selector", "text"],
    "press": ["key"],
    "scroll_down": ["amount?"],
    "scroll_up": ["amount?"],
    "scroll_to": ["y"],
    "move": ["x", "y"],
    "hover": ["selector?", "x?", "y?"],
    "drag": ["x1", "y1", "x2", "y2"],
    "copy": ["selector?", "x?", "y?"],
    "paste": ["selector?", "x?", "y?"],
    "back": [],
    "forward": [],
    "reload": [],
    "screenshot": ["path?"],
    "wait_selector": ["selector", "state?"],
    "get_text": ["selector?"],
    "get_title": [],
    "get_url": [],
    "select_option": ["selector", "value?|label?"],
    "check": ["selector"],
    "uncheck": ["selector"],
    "focus": ["selector"],
    "evaluate": ["expression"],
    "wait": ["ms?"],
}


def list_browser_tools():
    """The 28 canonical actions with their parameter names."""
    return [
        {"tool": name, "action": name, "params": list(_ACTION_PARAMS[name])}
        for name in _CANONICAL
    ]


def dispatch_browser_action(session, tool_name, args=None):
    """Dispatch one action on a session object exposing ``act``.

    Returns the timed result dict from ``session.act``. A session
    without ``act`` yields an honest failure, never a success.
    """
    act = getattr(session, "act", None)
    if not callable(act):
        return {"ok": False, "action": tool_name, "ms": 0.0,
                "detail": {"error": "no open BrowserSession given"}}
    return act(tool_name, **(args or {}))
