"""Per-copy persistent browser profile.

Each copy/worker gets its own Chromium user-data directory, so its
cookies and sessions persist across sessions and stay separate from
every other copy's. Profile directories live OUTSIDE the repo, under
a base directory chosen by the caller (default: the user's cache
directory), and must never be committed to the repo.

Sign-in is never automated here and no password, token, or cookie
value is ever stored or printed by this module. The owner signs in
herself through the browser; Chromium then keeps the session in the
profile directory. The only cookie information ever reported is a
row COUNT from a read-only query of the profile's Cookies database
(values are never read); anything unreadable is reported as unknown,
never as signed in.
"""

import json
import re
from pathlib import Path

DEFAULT_BASE_DIR = Path.home() / ".cache" / "agent-ultra-2"

_SAFE_COPY_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_copy_id(copy_id):
    """Sanitise a copy id into a single safe directory name."""
    cleaned = _SAFE_COPY_ID.sub("-", str(copy_id)).strip(".-") or "copy"
    if cleaned in (".", ".."):
        cleaned = "copy"
    return cleaned


def profile_dir_for(base_dir, copy_id):
    """User-data directory for one copy under ``base_dir/profiles/``."""
    return Path(base_dir) / "profiles" / safe_copy_id(copy_id)


class BrowserProfile:
    """A per-copy persistent profile directory."""

    def __init__(self, copy_id, base_dir=None):
        self.copy_id = safe_copy_id(copy_id)
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self.path = profile_dir_for(self.base_dir, self.copy_id)
        self.path.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.path / "PROFILE.json"
        self._write_meta()

    def _write_meta(self):
        meta = {
            "copy_id": self.copy_id,
            "profile_path": str(self.path),
            "sign_in": (
                "Owner signs in herself through the browser; the "
                "session persists via Chromium cookies/storage in "
                "this directory. No password is stored here, and no "
                "login is automated."
            ),
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
        return meta

    def status(self):
        """Report profile state. Cookie values are never read; only
        a Google cookie row count is reported, when readable."""
        cookies_db = self.path / "Cookies"
        result = {
            "copy_id": self.copy_id,
            "profile_path": str(self.path),
            "profile_exists": self.path.exists(),
            "cookies_db_exists": cookies_db.exists(),
            "google_cookie_count": None,
            "signed_in_likely": False,
            "status": "no-profile-yet" if not cookies_db.exists() else "unknown",
        }
        if not cookies_db.exists():
            result["note"] = (
                "No Chromium Cookies database in this profile yet — "
                "no browser has run against it, or the owner has not "
                "signed in through it."
            )
            return result
        try:
            import sqlite3

            uri = f"file:{cookies_db}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cookies "
                    "WHERE host_key LIKE '%google.com%'"
                ).fetchone()
            finally:
                conn.close()
            count = int(row[0]) if row else 0
            result["google_cookie_count"] = count
            result["signed_in_likely"] = count > 0
            result["status"] = (
                "google-cookies-present" if count > 0 else "no-google-cookies"
            )
        except Exception as exc:  # locked DB, missing table, ...
            result["status"] = "unreadable"
            result["note"] = (
                "Cookies DB could not be read: "
                f"{type(exc).__name__}. Sign-in state is unknown."
            )
        return result


def primary_profile_dir(base_dir=None):
    """Directory of the shared primary profile, if a caller opts in."""
    base = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
    return profile_dir_for(base, "primary")
