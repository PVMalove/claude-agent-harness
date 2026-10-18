"""The operating-system clipboard for the console.

Textual keeps its own clipboard: text copied in the app never reaches the system, and text copied
elsewhere is not pasted with Ctrl+V. Terminals often drop the OSC 52 sequence Textual relies on
for copying, so the console also talks to the platform's clipboard tool. A missing tool or an
error means "no system clipboard"; the app clipboard still works then."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

TIMEOUT = 2.0
LIMIT = 1_000_000


def _tools() -> list[tuple[list[str], list[str]]]:
    """(paste argv, copy argv) pairs for this platform, in order of preference."""
    if sys.platform == "darwin":
        return [(["pbpaste"], ["pbcopy"])]
    if sys.platform.startswith("win"):
        return [
            (
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                ["clip"],
            )
        ]
    tools: list[tuple[list[str], list[str]]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        tools.append((["wl-paste", "--no-newline"], ["wl-copy"]))
    if os.environ.get("DISPLAY"):
        tools.append(
            (
                ["xclip", "-selection", "clipboard", "-o"],
                ["xclip", "-selection", "clipboard", "-i"],
            )
        )
        tools.append(
            (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"])
        )
    # WSL: the Windows clipboard.
    tools.append(
        (
            ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            ["clip.exe"],
        )
    )
    return tools


def enabled() -> bool:
    return os.environ.get("HARNESS_CONSOLE_SYSTEM_CLIPBOARD", "1") != "0"


def read_system() -> str | None:
    """Text on the system clipboard, or None when it is unavailable or empty."""
    if not enabled():
        return None
    for paste, _copy in _tools():
        if shutil.which(paste[0]) is None:
            continue
        try:
            done = subprocess.run(
                paste,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout and len(done.stdout) <= LIMIT:
            return done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    return None


def write_system(text: str) -> bool:
    """Put `text` on the system clipboard; False when no tool accepted it."""
    if not enabled():
        return False
    for _paste, copy in _tools():
        if shutil.which(copy[0]) is None:
            continue
        try:
            # Output goes to /dev/null: wl-copy stays alive to serve the selection and would
            # hold a pipe open.
            done = subprocess.run(
                copy,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return True
    return False
