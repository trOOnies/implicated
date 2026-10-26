"""Open files with the default app of the computer the server runs on.

The app is meant to run on your own machine, so "the server's computer" is yours. Under WSL the
file opens in the Windows app associated with it (Notepad, for a ``.txt``, unless changed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def open_command(path: Path) -> list[str]:
    """The command that opens ``path``; raises ``OSError`` when there is none."""
    if sys.platform == "darwin":
        return ["open", str(path)]
    if _is_wsl():
        if shutil.which("wslview"):
            return ["wslview", str(path)]
        if shutil.which("explorer.exe") and shutil.which("wslpath"):
            result = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True)
            if result.returncode == 0:
                return ["explorer.exe", result.stdout.strip()]
    if shutil.which("xdg-open"):
        return ["xdg-open", str(path)]
    raise OSError("no program to open files with (install xdg-open, or wslu under WSL)")


def open_file(path: Path) -> None:
    """Open ``path`` without waiting for the app to close."""
    if sys.platform == "win32":
        os.startfile(path)  # only exists on Windows
        return
    subprocess.Popen(
        open_command(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
