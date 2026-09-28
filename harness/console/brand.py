"""The console's look: a warm terracotta palette and the harness mark shown on the dashboard.

Stdlib-only, like the rest of the textual-free seam: the palette is plain hex strings that
app.py turns into a textual Theme, and the banner is plain text, so both are testable without
textual."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import json_fields

THEME_NAME = "harness-warm"

# Terracotta/coral accents and amber on a soft graphite background.
PALETTE = {
    "primary": "#D97757",  # terracotta: the mark, titles, focus
    "secondary": "#E5A04B",  # amber
    "accent": "#EA580C",  # coral-orange highlight
    "warning": "#E5A04B",
    "error": "#E06C75",
    "success": "#8FB573",
    "foreground": "#EDE8E1",
    "background": "#1F1E1C",
    "surface": "#262523",
    "panel": "#2E2C29",
    "muted": "#9C958C",
}

# The harness mark: an "H" in a ring - the harness holding the parts together.
LOGO = (
    "   ╭─━━━━─╮   ",
    " ╭─╯ ┃  ┃ ╰─╮ ",
    " │   ┣━━┫   │ ",
    " ╰─╮ ┃  ┃ ╭─╯ ",
    "   ╰─━━━━─╯   ",
)

PRODUCT = "Agent Harness console"


@dataclass(frozen=True)
class BannerInfo:
    """What the banner shows beside the mark."""

    version: str
    capabilities: tuple[str, ...]
    repo: str
    branch: str | None


def display_path(repo: Path, home: Path | None = None) -> str:
    """The repository path with the home directory shortened to `~`, as shells show it."""
    home = home if home is not None else Path.home()
    try:
        return "~/" + repo.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return repo.as_posix()


def installed_capabilities(repo: Path) -> tuple[str, ...]:
    """The capabilities recorded in `.harness/harness.lock`; empty when there is no readable lock."""
    try:
        payload = json.loads(
            (repo / ".harness" / "harness.lock").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(json_fields.strings(payload.get("capabilities")))


def current_branch(repo: Path, *, timeout: float = 5.0) -> str | None:
    """The checked-out branch, or None when detached or git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def collect_banner(repo: Path, version: str) -> BannerInfo:
    return BannerInfo(
        version=version,
        capabilities=installed_capabilities(repo),
        repo=display_path(repo),
        branch=current_branch(repo),
    )


def banner_lines(info: BannerInfo) -> tuple[str, ...]:
    """The description beside the mark, one line per row of the logo."""
    capabilities = (
        " · ".join(info.capabilities) if info.capabilities else "харнесс не установлен"
    )
    return (
        "",
        f"{PRODUCT} {info.version}",
        capabilities,
        info.repo,
        f"ветка {info.branch}" if info.branch else "ветка не определена",
    )
