#!/usr/bin/env python3
"""
Roundhouse entry point.

Sleep proxy, process control, and the dashboard live in `mc_host`.
Production launches this file: `python3 /server/manager.py`.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer

from mc_host.main import main
from mc_host.proxy import SleepProxy

__all__ = ["SleepProxy", "ThreadingHTTPServer", "main"]


if __name__ == "__main__":
    main()
