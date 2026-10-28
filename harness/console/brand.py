"""The console's look: a warm terracotta palette and the harness mark shown on the dashboard.

Stdlib-only, like the rest of the textual-free seam: the palette is plain hex strings that
app.py turns into a textual Theme, and the banner is plain text, so both are testable without
textual."""

from __future__ import annotations

import json
import re
import subprocess
from string import hexdigits
from dataclasses import dataclass
from pathlib import Path

from harness.health.project_files import canonical_capability

from . import json_fields

THEME_NAME = "harness-warm"
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
# Terminals narrower than this many columns get the shell's narrow layout.
NARROW_WIDTH = 100

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
    "muted": "#AAA398",
}

# Tones of the workspace shell that Textual's theme has no slot for. CSS refers to them as
# `$border-subtle`, `$selection-background` and so on; `shell_css` resolves them, so a screen's
# CSS works in any host app, not only in one that registered the harness theme.
SHELL_TOKENS = {
    # A decorative divider stays faint; a component's own edge (button, field, list, note) must
    # reach 3:1 against the surface, like the selection edge and the scrollbar.
    "border-subtle": "#3a3731",
    "component-border": "#847d70",
    "selection-background": "#45332c",
    "selection-foreground": "#f6baa3",
    "selection-edge": "#d97757",
    # The outline of the selected main-menu entry, drawn around its fill (decorative: the fill
    # and the selection colour of its text already mark it).
    "selection-outline": "#6b4a3c",
    "row-highlight": "#35302b",
    "scrollbar-thumb": "#f2ece1",
    "scrollbar-track": "#8a8378",
    "event-background": "#292621",
    "event-active-background": "#423029",
}
# One colour per worker role, so a role reads the same on the dashboard and in the worker view.
ROLE_COLORS = {
    "architect": "#B79CED",
    "developer": "#E58A6A",
    "code-review": "#E5A04B",
    "qa": "#6FB7C9",
    "publish": "#8FB573",
    "verification": "#7FA3E0",
}
_SHELL_TOKEN = re.compile(
    r"\$(" + "|".join(sorted(SHELL_TOKENS, key=len, reverse=True)) + r")(?![\w-])"
)


def shell_css(css: str) -> str:
    """`css` with every `$<shell token>` replaced by its colour."""
    return _SHELL_TOKEN.sub(lambda match: SHELL_TOKENS[match.group(1)], css)


# The Harness next mark: an H followed by a forward chevron.
LOGO = (
    "   ╭─━━━━─╮   ",
    " ╭─╯ ┃  ┃ ╰─╮ ",
    " │   ┣━━┫   │ ",
    " ╰─╮ ┃  ┃ ╭─╯ ",
    "   ╰─━━━━─╯   ",
)
MARK = " * H› "


PRODUCT = "Harness next console"

# Status is never colour alone: every state also has a glyph (accessibility, monochrome terminals).
STATUS_ICONS = {"ok": "✔", "warn": "⚠", "fail": "✖", "skipped": "○"}
STATUS_COLORS = {
    "ok": PALETTE["success"],
    "warn": PALETTE["warning"],
    "fail": PALETTE["error"],
    "skipped": PALETTE["muted"],
}


def shorten(text: str, limit: int) -> str:
    """Cut `text` to `limit` cells with a trailing ellipsis."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def elide_path(path: str, width: int) -> str:
    """`path` cut from the left to `width` cells at a separator, `…/Code/project`, so its end
    (the project) stays readable; a path that fits is kept whole."""
    if len(path) <= width:
        return path
    parts = path.split("/")
    for start in range(1, len(parts)):
        tail = "…/" + "/".join(parts[start:])
        if len(tail) <= width:
            return tail
    return "…" + path[len(path) - max(width - 1, 0) :] if width > 1 else "…"


def frame_title(title: str) -> str:
    """Add symmetric spacing to Textual's built-in border label padding."""
    return f" {title.strip()} "


def section_title(title: str, note: str = "") -> str:
    """A section card's title for its top border: the title in capitals, then its counter or
    note, if any (`" ТРЕБУЕТ ВНИМАНИЯ  2 "`)."""
    heading = title.strip().upper()
    return frame_title(f"{heading}  {note.strip()}" if note.strip() else heading)


@dataclass(frozen=True)
class BannerInfo:
    """What the banner shows beside the mark."""

    version: str
    capabilities: tuple[str, ...]
    repo: str
    branch: str | None
    # Short HEAD of the selected project (the repo the console is pointed at), not of the console.
    project_commit: str | None = None
    console_commit: str | None = None
    installed_version: str | None = None
    installed_commit: str | None = None


def display_path(repo: Path, home: Path | None = None) -> str:
    """The repository path with the home directory shortened to `~`, as shells show it."""
    home = home if home is not None else Path.home()
    try:
        return "~/" + repo.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return repo.as_posix()


def _installed_lock(repo: Path) -> dict[str, object]:
    try:
        payload = json.loads(
            (repo / ".harness" / "harness.lock").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def installed_capabilities(repo: Path) -> tuple[str, ...]:
    """The capabilities recorded in `.harness/harness.lock`; empty when there is no readable lock."""
    return tuple(
        dict.fromkeys(
            canonical_capability(name)
            for name in json_fields.strings(_installed_lock(repo).get("capabilities"))
        )
    )


def _short_commit(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    revision = value.strip()
    if len(revision) in (40, 64) and all(char in hexdigits for char in revision):
        return revision[:8].lower()
    return None


def source_commit(
    source_root: Path | None = None, *, timeout: float = 5.0
) -> str | None:
    """The running console's source revision; archives must not inherit a host project's HEAD."""
    root = source_root if source_root is not None else _SOURCE_ROOT
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _short_commit(result.stdout) if result.returncode == 0 else None


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


def project_commit(repo: Path, *, timeout: float = 5.0) -> str | None:
    """Short SHA of the project's HEAD; None for a repository without a first commit, a directory
    outside Git, an unavailable git or a timeout. Works on a detached HEAD too."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _short_commit(result.stdout) if result.returncode == 0 else None


def collect_banner(repo: Path, version: str) -> BannerInfo:
    lock = _installed_lock(repo)
    installed_version = lock.get("package_version")
    return BannerInfo(
        version=version,
        capabilities=installed_capabilities(repo),
        repo=display_path(repo),
        branch=current_branch(repo),
        project_commit=project_commit(repo),
        console_commit=source_commit(),
        installed_version=(
            installed_version.strip() or None
            if isinstance(installed_version, str)
            else None
        ),
        installed_commit=_short_commit(lock.get("source_revision")),
    )


def banner_lines(info: BannerInfo) -> tuple[str, ...]:
    """The description beside the mark, one line per row of the logo."""
    console_revision = (
        f"commit {info.console_commit}"
        if info.console_commit
        else "commit не определён"
    )
    project_revision = (
        f"commit {info.project_commit}"
        if info.project_commit
        else "commit не определён"
    )
    if info.branch:
        project_line = f"ветка {info.branch} · {project_revision}"
    elif info.project_commit:
        project_line = f"detached HEAD · {project_revision}"
    else:
        project_line = f"ветка не определена · {project_revision}"
    installation = "харнесс не установлен"
    if info.installed_version or info.installed_commit or info.capabilities:
        installed_revision = (
            f"commit {info.installed_commit}"
            if info.installed_commit
            else "commit не определён"
        )
        installation = (
            f"Installed Harness {info.installed_version or '?'} · {installed_revision}"
        )
        if info.capabilities:
            installation += " · " + " · ".join(info.capabilities)
    return (
        "",
        f"{PRODUCT} {info.version} · {console_revision}",
        installation,
        info.repo,
        project_line,
    )
