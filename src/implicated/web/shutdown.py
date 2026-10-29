"""Stopping the server from the page, with the header's exit button."""

from __future__ import annotations

import os
import signal

# Set by ``python -m implicated.web`` to its own process id. With ``--reload`` the app runs in a
# worker process that uvicorn's reloader (that first process) starts, and stopping only the
# worker would leave the reloader running.
SERVER_PID = "IMPLICATED_SERVER_PID"


def stop() -> None:
    """Shut the server down gracefully, as Ctrl+C in its terminal would: the library is saved on
    every change, so nothing is lost."""
    pid = int(os.environ.get(SERVER_PID) or os.getpid())
    os.kill(pid, signal.SIGINT)
