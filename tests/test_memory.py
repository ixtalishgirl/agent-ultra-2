"""Tests for memory/store.py.

Run: python3 tests/test_memory.py   (stdlib only, exit 0 on pass)
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.store import MemoryStore  # noqa: E402

passed = 0


def check(label, cond):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"pass: {label}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.jsonl")

        # Empty store recalls nothing.
        check("empty recall is empty", store.recall(key="x") == [])

        store.save("task", {"step": 1}, tags=["work"])
        store.save("task", {"step": 2}, tags=["work", "done"])
        store.save("note", "hello", tags=["personal"])

        # Key round-trip: both records under "task" come back in order.
        by_key = store.recall(key="task")
        check("key recall returns 2 records", len(by_key) == 2)
        check("key recall values round-trip",
              [r["value"] for r in by_key] == [{"step": 1}, {"step": 2}])

        # Tag round-trip: "done" matches only the second task record.
        by_tag = store.recall(tags=["done"])
        check("tag recall returns 1 record", len(by_tag) == 1)
        check("tag recall value is correct",
              by_tag[0]["value"] == {"step": 2})

        # Key + tags together narrow to the intersection.
        both = store.recall(key="task", tags=["done"])
        check("key+tag recall returns 1 record", len(both) == 1)

        # Non-matching recall is empty, not invented.
        check("non-matching key is empty",
              store.recall(key="missing") == [])
        check("non-matching tag is empty",
              store.recall(tags=["nope"]) == [])

        # A fresh store object on the same file sees the same records
        # (file-backed persistence, not in-memory only).
        reopened = MemoryStore(Path(tmp) / "memory.jsonl")
        check("records persist across store objects",
              len(reopened.recall()) == 3)

    print(f"ALL PASS: {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
