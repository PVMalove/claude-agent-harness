"""Leaving a screen for a section by closing it, as Esc does."""

from __future__ import annotations

from ..shell import ConsoleShell


def leave_for_section(screen: ConsoleShell, index: int | None) -> None:
    """Close `screen` as Esc does (`dismiss(None)` runs the push callback that refreshes the opener),
    then, for a section, open it from the app's loop through the shell's own opener.

    `ConsoleShell.open_section` is called unbound so the screen's own override does not run again.
    For Home the shell's pop is a no-op when the dashboard is on top and closes the rest otherwise."""
    screen.dismiss(None)
    if index is not None:
        screen.app.call_later(ConsoleShell.open_section, screen, index)
