"""Local file-backed memory store. No network, no keys, no secrets.

Records are appended, one JSON object per line, to a local JSONL
file:

  {"key": "...", "value": ..., "tags": [...],
   "created_utc": "ISO-8601 timestamp"}

The store saves only what the caller passes. It collects no personal
data, credentials, or profile data on its own, and it never sends
anything anywhere. "value" must be JSON-serializable.

recall(key=None, tags=None):
  - key given: records whose key matches exactly.
  - tags given: records carrying at least one of the given tags.
  - both given: records matching both conditions.
  - neither given: all records, oldest first.

Stdlib only.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent / "memory.jsonl"


class MemoryStore:
    """Append-only JSONL memory."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_MEMORY_PATH

    def save(self, key, value, tags=None):
        """Append one record. Returns the record that was written."""
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        tags = list(tags) if tags is not None else []
        if not all(isinstance(t, str) for t in tags):
            raise ValueError("tags must be a list of strings")
        record = {
            "key": key,
            "value": value,
            "tags": tags,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            line = json.dumps(record)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value is not JSON-serializable: {exc}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record

    def _read_all(self):
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # skip a corrupt line, keep the rest
                if isinstance(record, dict) and "key" in record:
                    records.append(record)
        return records

    def recall(self, key=None, tags=None):
        """Return matching records, oldest first."""
        wanted_tags = set(tags) if tags is not None else None
        out = []
        for record in self._read_all():
            if key is not None and record.get("key") != key:
                continue
            if wanted_tags is not None and \
                    not wanted_tags.intersection(record.get("tags", [])):
                continue
            out.append(record)
        return out
