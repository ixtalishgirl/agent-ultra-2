"""Approach pool for Agent Ultra 2 copies.

Every entry is a real, runnable task step (stdlib only), not a
label. Each approach function takes (task, browser=None) and
returns (result, evidence_list). Failures raise, and the caller
turns them into error evidence instead of a faked result.

Browser approaches are duck-typed: the browser object only needs
act(action, **kw) and screenshot(). When no browser is passed in,
the caller reports the browser as unavailable and does not run a
browser step. This module never imports the browser package: the
real browser is injected later by the wiring wave.

Waves note: 100 simultaneous copies on one host are NOT claimed.
Copies run in sequential waves of at most max_concurrent; an
approach may repeat across waves, never within one wave.
"""

import ast
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path


def _task_text(task):
    if isinstance(task, dict):
        return task.get("text") or task.get("task_id") or ""
    if isinstance(task, str):
        return task
    return ""


def _calculator(task, browser=None):
    expr = "2+3*4"
    if isinstance(task, dict) and task.get("expression"):
        expr = str(task["expression"])
    tree = ast.parse(expr, mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div,
                          ast.Mod, ast.Pow)):
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Mod):
                return left % right
            return left ** right
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.USub, ast.UAdd)):
            value = _eval(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        raise ValueError(f"unsupported expression part: {ast.dump(node)}")

    result = _eval(tree)
    return result, [f"calculator {expr}={result}"]


def _text_count(task, browser=None):
    text = _task_text(task)
    words = text.split()
    return len(words), [f"text_count words={len(words)} chars={len(text)}"]


def _text_reverse(task, browser=None):
    text = _task_text(task)
    result = text[::-1]
    return result, [f"text_reverse chars={len(text)}"]


def _text_upper(task, browser=None):
    text = _task_text(task)
    return text.upper(), [f"text_upper chars={len(text)}"]


def _shell_echo(task, browser=None):
    # Fixed probe string, passed as a single argv element (no shell),
    # so task content can never become a shell command.
    proc = subprocess.run(["echo", "arena-probe"], capture_output=True,
                          text=True, timeout=30)
    result = proc.stdout.strip()
    if proc.returncode != 0 or result != "arena-probe":
        raise RuntimeError(f"shell echo failed rc={proc.returncode}")
    return result, ["shell echo arena-probe rc=0"]


def _registry_count(task, browser=None):
    count = len(APPROACH_NAMES)
    return count, [f"registry approach_count={count}"]


def _json_roundtrip(task, browser=None):
    payload = task if isinstance(task, dict) else {"task": task}
    text = json.dumps(payload, sort_keys=True)
    restored = json.loads(text)
    return len(restored), [f"json_roundtrip keys={len(restored)}"]


def _hash_digest(task, browser=None):
    text = _task_text(task)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest, [f"hash_digest sha256 chars={len(text)}"]


def _sort_words(task, browser=None):
    words = _task_text(task).split()
    result = sorted(words)
    return result, [f"sort_words words={len(words)}"]


def _sum_numbers(task, browser=None):
    numbers = [1, 2, 3, 4]
    if isinstance(task, dict) and isinstance(task.get("numbers"), list):
        numbers = task["numbers"]
    total = sum(numbers)
    return total, [f"sum_numbers n={len(numbers)} total={total}"]


def _browser_open(task, browser=None):
    url = "https://example.com"
    if isinstance(task, dict) and task.get("url"):
        url = str(task["url"])
    outcome = browser.act("open", url=url)
    return outcome, [f"browser open {url} -> {outcome}"]


def _browser_screenshot(task, browser=None):
    outcome = browser.screenshot()
    return outcome, [f"browser screenshot -> {outcome}"]


def _file_write_read(task, browser=None):
    text = _task_text(task)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.txt"
        path.write_text(text, encoding="utf-8")
        result = path.read_text(encoding="utf-8")
    if result != text:
        raise RuntimeError("file_write_read mismatch")
    return result, [f"file_write_read chars={len(text)}"]


def _word_frequency(task, browser=None):
    words = _task_text(task).split()
    if not words:
        return None, ["word_frequency no words"]
    word, count = Counter(words).most_common(1)[0]
    return word, [f"word_frequency top={word} count={count}"]


def _list_dedup(task, browser=None):
    items = [1, 2, 2, 3, 3, 3]
    if isinstance(task, dict) and isinstance(task.get("items"), list):
        items = task["items"]
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen, [f"list_dedup in={len(items)} out={len(seen)}"]


def _arithmetic_sequence(task, browser=None):
    n = 10
    if isinstance(task, dict) and isinstance(task.get("n"), int):
        n = task["n"]
    total = sum(range(1, n + 1))
    return total, [f"arithmetic_sequence n={n} total={total}"]


APPROACHES = {
    "calculator": _calculator,
    "text_count": _text_count,
    "text_reverse": _text_reverse,
    "text_upper": _text_upper,
    "shell_echo": _shell_echo,
    "registry_count": _registry_count,
    "json_roundtrip": _json_roundtrip,
    "hash_digest": _hash_digest,
    "sort_words": _sort_words,
    "sum_numbers": _sum_numbers,
    "browser_open": _browser_open,
    "browser_screenshot": _browser_screenshot,
    "file_write_read": _file_write_read,
    "word_frequency": _word_frequency,
    "list_dedup": _list_dedup,
    "arithmetic_sequence": _arithmetic_sequence,
}

APPROACH_NAMES = tuple(APPROACHES.keys())

# Browser approaches need a browser object; without one the step is
# reported as unavailable, never faked.
BROWSER_APPROACHES = ("browser_open", "browser_screenshot")


def get_approach(name):
    """Return the callable for a named approach, or None."""
    return APPROACHES.get(name)


def run_approach(name, task, browser=None):
    """Run one named approach. Returns a dict with status, result
    and evidence. Unknown approaches and browser steps without a
    browser are error evidence, never faked results."""
    fn = APPROACHES.get(name)
    if fn is None:
        return {"status": "error", "result": None,
                "evidence": [f"unknown approach: {name!r}; not executed"]}
    if name in BROWSER_APPROACHES and browser is None:
        return {"status": "error", "result": None,
                "evidence": [f"browser unavailable: no browser object "
                             f"passed in; {name} not executed"]}
    try:
        result, evidence = fn(task, browser=browser)
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        return {"status": "error", "result": None,
                "evidence": [f"approach {name} raised: "
                             f"{type(exc).__name__}: {exc}"]}
    if isinstance(result, dict) and result.get("ok") is False:
        return {"status": "error", "result": result,
                "evidence": list(evidence) + ["browser action reported not ok"]}
    return {"status": "ok", "result": result, "evidence": list(evidence)}
