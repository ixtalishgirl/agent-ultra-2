"""Backend client for Agent Ultra 2: environment-only, stdlib only.

The backend is configured ONLY through the environment:

  - MODEL_API_URL: the full chat-completions endpoint URL.
  - MODEL_ID: the model identifier sent in the request body.

There are no values, no defaults, and no hardcoded identifiers in
this module. If either variable is unset, the backend is not live
and ``chat()`` raises ``RuntimeError("model not live yet")`` instead
of returning a fabricated reply. Values from the environment are
never echoed in return values or error messages.

``chat()`` speaks the OpenAI-compatible chat-completions shape:
POST JSON ``{"model", "messages", "max_tokens", "tools?"}`` and
read ``choices[0].message`` back. Timeout is 120 seconds and
``max_tokens`` defaults to 256.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_TOKENS = 256
NOT_LIVE = "model not live yet"


def _configured():
    """Read the backend configuration from the environment now.

    Read at call time (not import time) so a test or a deploy can
    set or unset the variables and the next call sees the change.
    Returns (url, model_id), either of which may be None.
    """
    url = os.environ.get("MODEL_API_URL") or None
    model_id = os.environ.get("MODEL_ID") or None
    return url, model_id


def health():
    """Backend liveness as booleans only; no values are returned."""
    url, model_id = _configured()
    live = bool(url and model_id)
    return {
        "live": live,
        "url_configured": bool(url),
        "model_configured": bool(model_id),
    }


def chat(messages, tools=None, max_tokens=DEFAULT_MAX_TOKENS,
         timeout=DEFAULT_TIMEOUT):
    """POST one chat-completions request; return the reply message.

    Returns a dict::

        {"content": str | None,
         "tool_calls": [{"id", "name", "arguments"}],
         "message": <raw message dict from the backend>}

    ``arguments`` is the parsed JSON object when the backend sent a
    JSON string, otherwise the raw string under ``arguments_raw``.
    Raises RuntimeError when the backend is not configured, when
    the request fails, or when the response carries no usable
    message. It never invents a reply.
    """
    url, model_id = _configured()
    if not url or not model_id:
        raise RuntimeError(NOT_LIVE)
    body = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools is not None:
        body["tools"] = tools
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"model request failed: HTTP {exc.code} {detail}".strip()
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"model request failed: {type(exc).__name__}: {exc}"
        ) from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise RuntimeError("model response had no choices")
    message = choices[0].get("message") or {}
    tool_calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments", "")
        entry = {"id": call.get("id"), "name": fn.get("name")}
        if isinstance(raw_args, dict):
            entry["arguments"] = raw_args
        else:
            try:
                entry["arguments"] = json.loads(raw_args) if raw_args else {}
            except ValueError:
                entry["arguments"] = {}
                entry["arguments_raw"] = raw_args
        tool_calls.append(entry)
    return {
        "content": message.get("content"),
        "tool_calls": tool_calls,
        "message": message,
    }
