"""The shared look of a worker session: a role label, the ticket, the dispatch and a status caption.

The dashboard's session list and the worker view's session list build their cards here, so a role
and a state read the same on both. Status is never colour alone: the caption is the status symbol
plus words, and the role label is the role's name."""

from __future__ import annotations

from rich.text import Text

from . import brand
from .status import Status, session_status

ROLES = tuple(brand.ROLE_COLORS)


def role_label(role: str) -> Text:
    """The role name in its own colour; a role outside `ROLES` is shown plainly in the muted tone."""
    color = brand.ROLE_COLORS.get(role, brand.PALETTE["muted"])
    return Text(role, style=f"bold {color}")


def status_caption(status: Status, language: str) -> Text:
    """The status as symbol plus words, in the status colour."""
    return Text(status.render(language), style=status.color)


def session_card(
    *,
    ticket: str,
    role: str,
    dispatch_id: str,
    state: str,
    language: str,
    id_limit: int,
    stacked: bool = False,
) -> Text:
    """Ticket and role on the first line, then the shortened dispatch ID and the status caption:
    on one line, or each on its own when `stacked` (a narrow list)."""
    card = Text(f"{ticket}  ·  ", style=brand.PALETTE["foreground"])
    card.append_text(role_label(role))
    card.append("\n")
    card.append(
        brand.shorten(dispatch_id, id_limit) + ("\n\n" if stacked else "  ·  "),
        style=brand.PALETTE["muted"],
    )
    card.append_text(status_caption(session_status(state), language))
    return card
