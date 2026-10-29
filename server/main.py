"""Entrypoint for Agent Ultra 2: ``python3 -m server.main``.

Serves on $PORT (Render sets it; 8000 is only the local fallback).
Run from the repo root. The repo root is added to sys.path first
so the sibling top-level packages (browser, agent, copies, tools,
memory) import no matter where the command is launched from.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.chat_server import serve  # noqa: E402


def main():
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        port = 8000
    print(f"Agent Ultra 2 listening on 0.0.0.0:{port} "
          f"(/health, /ui)", flush=True)
    serve(port, host="0.0.0.0")


if __name__ == "__main__":
    main()
