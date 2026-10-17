"""The console-wide status vocabulary: six kinds, each a symbol plus text, in Russian and English."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.text import Text

from harness.console import brand, status

LEDGER_STATES = (
    "working",
    "dispatched",
    "approved",
    "reported",
    "blocked",
    "failed",
    "cancelled",
    "abandoned",
)


def _every_status() -> list[status.Status]:
    return [
        *(status.session_status(state) for state in (*LEDGER_STATES, "unknown")),
        status.PERMISSION_PENDING,
        *(status.process_status(worker) for worker in ("absent", "stopped", "?")),
    ]


def test_there_are_exactly_six_kinds_with_a_single_cell_symbol_and_a_palette_colour() -> (
    None
):
    assert set(status.KINDS) == {
        "accepted",
        "running",
        "paused",
        "not_started",
        "error",
        "warning",
    }
    symbols = [symbol for symbol, _ in status.KINDS.values()]
    assert all(cell_len(symbol) == 1 for symbol in symbols)
    assert len(set(symbols)) == len(symbols)
    assert all(color in brand.PALETTE for _, color in status.KINDS.values())


def test_render_is_the_symbol_plus_the_text_in_the_language() -> None:
    working = status.session_status("working")
    assert working.render("ru") == "▶ Выполняется"
    assert working.render("en") == "▶ Running"
    assert working.color == brand.PALETTE["success"]


@pytest.mark.parametrize("current", _every_status())
def test_every_status_has_a_symbol_and_text_in_both_languages(
    current: status.Status,
) -> None:
    for language in ("ru", "en"):
        rendered = current.render(language)
        assert rendered.startswith(current.symbol + " ")
        assert rendered.removeprefix(current.symbol + " ").strip()
    assert current.ru != current.en


def test_session_states_map_onto_the_kinds() -> None:
    kinds = {state: status.session_status(state).kind for state in LEDGER_STATES}
    assert kinds["working"] == "running"
    assert kinds["reported"] == "accepted"
    assert kinds["approved"] == "not_started"
    assert kinds["blocked"] == kinds["failed"] == "error"
    assert kinds["dispatched"] == kinds["cancelled"] == kinds["abandoned"] == "paused"


def test_an_unknown_state_is_unconfirmed_not_silently_fine() -> None:
    unknown = status.session_status("something-new")
    assert unknown.kind == "warning"
    assert unknown.render("en") == "⚠ Unconfirmed"


def test_permission_and_process_states_have_their_own_text() -> None:
    assert status.PERMISSION_PENDING.render("en") == "⚠ Permission pending"
    assert status.process_status("absent").render("en") == "⚠ Process not found"
    assert status.process_status("stopped").render("ru") == "Ⅱ Процесс завершён"
    assert status.process_status("anything else").render("en") == "⚠ Outcome unknown"


def test_session_state_labels_are_not_duplicated_in_the_interface_strings() -> None:
    from harness.console.i18n_strings import EN

    for current in _every_status():
        assert not any(key.partition(" ")[2] == current.ru for key in EN), current.ru


def test_status_module_does_not_import_textual() -> None:
    source = Path(status.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*(?:from|import)\s+(?:textual|rich)\b", source, re.MULTILINE
    )


def test_every_role_has_its_own_colour_and_an_unknown_role_is_muted() -> None:
    from harness.console import session_card

    assert set(brand.ROLE_COLORS) == {
        "architect",
        "developer",
        "code-review",
        "qa",
        "publish",
        "verification",
    }
    colors = [brand.ROLE_COLORS[role] for role in brand.ROLE_COLORS]
    assert len(set(colors)) == len(colors)
    for role in brand.ROLE_COLORS:
        label = session_card.role_label(role)
        assert label.plain == role
        assert brand.ROLE_COLORS[role] in str(label.style)
    assert brand.PALETTE["muted"] in str(session_card.role_label("planner").style)


def test_status_caption_is_symbol_plus_words_in_the_status_colour() -> None:
    from harness.console import session_card

    running = status.session_status("working")
    caption = session_card.status_caption(running, "en")
    assert caption.plain == "▶ Running"
    assert running.color in str(caption.style)


def test_session_card_carries_ticket_role_dispatch_and_caption() -> None:
    from harness.console import session_card

    def card(*, stacked: bool) -> Text:
        return session_card.session_card(
            ticket="#721",
            role="developer",
            dispatch_id="dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f287",
            state="working",
            language="ru",
            id_limit=22,
            stacked=stacked,
        )

    inline = card(stacked=False)
    assert inline.plain.splitlines() == [
        "#721  ·  developer",
        "dispatch-e98a4a72-50a…  ·  ▶ Выполняется",
    ]
    stacked = card(stacked=True)
    assert stacked.plain.splitlines() == [
        "#721  ·  developer",
        "dispatch-e98a4a72-50a…",
        "",
        "▶ Выполняется",
    ]
    colors = {str(span.style) for span in inline.spans}
    assert f"bold {brand.ROLE_COLORS['developer']}" in colors
    assert status.session_status("working").color in colors
