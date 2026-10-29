"""Agent Ultra 2 browser package: live Playwright browser per copy.

Exports the exact interfaces a later wave wires into the server.
Importing this package never imports Playwright; Playwright is
imported lazily when a session starts.
"""

from .actions import dispatch_browser_action, list_browser_tools
from .cdp import CDPClient
from .screencast import Screencast, start_screencast
from .session import BrowserNotAvailable, BrowserSession, playwright_available

__all__ = [
    "BrowserSession",
    "dispatch_browser_action",
    "list_browser_tools",
    "BrowserNotAvailable",
    "playwright_available",
    "CDPClient",
    "Screencast",
    "start_screencast",
]
