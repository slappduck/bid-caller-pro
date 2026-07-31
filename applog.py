"""
applog.py — Splits technical logs (for you) from user messages (for customers).

- debug(msg)  -> written ONLY to bidcaller_debug.log (and console). Customers never see it.
- The app's on-screen log shows only the friendly messages you pass to the UI.

This keeps character counts, tracebacks, and Ollama details out of what
contractors see, while still giving you everything you need to troubleshoot.
"""

import os
import sys
import datetime

if getattr(sys, 'frozen', False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(_BASE, "bidcaller_debug.log")


def debug(msg):
    """Technical log — goes to file + console only. Never shown to the customer."""
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line)  # visible in console if run from a terminal
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
