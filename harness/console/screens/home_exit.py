"""Leaving a screen for Home by closing it."""

from __future__ import annotations

from textual.screen import Screen

from ..shell import HOME_SECTION, SECTIONS


def closes_into_home(screen: Screen[None], index: int) -> bool:
    """Whether section `index` is Home and the home dashboard lies right under `screen`.

    The shell goes Home with `pop_screen`, which drops the callback the dashboard pushed the screen
    with (it refreshes Home). Closing the screen with `dismiss(None)` runs it, as Esc does."""
    from .dashboard import DashboardScreen

    stack = screen.app.screen_stack
    return (
        SECTIONS[index] == HOME_SECTION
        and len(stack) > 1
        and stack[-1] is screen
        and isinstance(stack[-2], DashboardScreen)
    )
