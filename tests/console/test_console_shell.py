"""The shared console shell: wide and narrow terminals, status of the active section, keyboard
and RU/EN. A stub host screen stands in for the screens that use the shell."""

from __future__ import annotations

# ruff: noqa: E402 — optional Textual imports must follow importorskip.

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

pytest.importorskip("textual")
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Button, ListItem, ListView, Static

from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import DashboardScreen
from harness.console.crumbs import Crumb
from harness.console.wrap import SEPARATORS
from harness.console.shell import (
    SECTIONS,
    Breadcrumbs,
    CrumbLink,
    ConsoleShell,
    ContextPanel,
    FieldGrid,
    ShellIds,
    ShellSidebar,
)

LONG_ID = "dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f287"
LONG_PATH = "/home/user/Projects/Code/some-project/.harness/.sandboxes/worktrees/issue-721-console"


class StubShell(ConsoleShell):
    SECTION = "Reports"
    CRUMBS = "Главная › Отчёты"
    HAS_CONTEXT = True
    IDS = ShellIds(menu="stub-sections")

    def compose_strip(self) -> ComposeResult:
        yield Static(LONG_PATH, id="stub-path", classes="no-i18n", markup=False)

    def compose_content(self) -> ComposeResult:
        yield FieldGrid(
            [
                ("Dispatch", LONG_ID),
                ("Worktree", LONG_PATH),
                ("Причина блокировки", "stop"),
            ],
            id="stub-fields",
        )
        yield Button("Действие", id="stub-action")

    def compose_context(self) -> ComposeResult:
        yield Static("Контекст", id="stub-context-text")


async def _open(app: HarnessConsoleApp, pilot: object) -> StubShell:
    screen = StubShell()
    await app.push_screen(screen)
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    return screen


def test_wide_shell_shows_menu_breadcrumbs_content_and_context_side_by_side(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            sidebar = screen.query_one(ShellSidebar)
            context = screen.query_one(ContextPanel)
            fields = screen.query_one("#stub-fields", FieldGrid)
            assert sidebar.display
            assert not screen.query_one("#shell-menu", Button).display
            assert sidebar.region.right <= fields.region.x
            assert fields.region.right <= context.region.x
            assert screen.query_one("#shell-breadcrumbs", Breadcrumbs).text == (
                "Главная  ›  Отчёты"
            )
            menu = screen.query_one("#stub-sections", ListView)
            assert [item.name for item in menu.children] == list(SECTIONS)
            assert menu.index == SECTIONS.index("Reports")
            assert menu.children[SECTIONS.index("Reports")].has_class("-active")
            cells = list(fields.query(".field").results(Static))
            assert cells[0].region.y == cells[2].region.y
            assert cells[0].region.x < cells[2].region.x
            assert cells[1].region.y > cells[0].region.y

    asyncio.run(scenario())


def test_narrow_shell_collapses_menu_folds_context_and_wraps_long_values(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(70, 40)) as pilot:
            screen = await _open(app, pilot)
            sidebar = screen.query_one(ShellSidebar)
            toggle = screen.query_one("#shell-menu", Button)
            assert not sidebar.display
            assert toggle.display
            await pilot.click("#shell-menu")
            await pilot.pause()
            assert sidebar.display
            assert screen.query_one("#stub-sections", ListView).has_focus
            await pilot.click("#shell-menu")
            await pilot.pause()
            assert not sidebar.display
            # The context panel is folded under the content, not hidden.
            context = screen.query_one(ContextPanel)
            fields = screen.query_one("#stub-fields", FieldGrid)
            assert context.display and context.region.height > 0
            assert context.region.y >= fields.region.bottom
            # The content keeps the whole screen; the panel is one scroll below it.
            body = screen.query_one("#shell-body")
            assert body.max_scroll_y > 0
            body.scroll_to_widget(context, animate=False)
            await pilot.pause()
            assert screen.region.contains_region(context.region)
            # One column; long IDs and paths wrap whole instead of being cut short.
            cells = list(fields.query(".field").results(Static))
            assert len({cell.region.x for cell in cells}) == 1
            rows = [cell.region.y for cell in cells]
            assert rows == sorted(set(rows))
            for cell in cells[:2]:
                assert screen.region.contains_region(cell.region)
                assert cell.styles.text_overflow != "ellipsis"
            assert cells[1].region.height >= 3
            path = screen.query_one("#stub-path", Static)
            assert path.region.height > 1 and screen.region.contains_region(path.region)
            assert screen.region.contains_region(
                screen.query_one("#stub-action").region
            )

    asyncio.run(scenario())


def test_wider_than_140_columns_gives_the_context_panel_more_room(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[int]:
        widths = []
        for columns in (120, 150):
            app = HarnessConsoleApp(tmp_path)
            async with app.run_test(size=(columns, 40)) as pilot:
                screen = await _open(app, pilot)
                widths.append(screen.query_one(ContextPanel).region.width)
        return widths

    wide, spacious = asyncio.run(scenario())
    assert spacious > wide


def test_number_keys_open_other_sections_and_the_active_one_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []

    async def record(_self: DashboardScreen, index: int) -> None:
        opened.append(index)

    monkeypatch.setattr(DashboardScreen, "action_open_section", record)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await pilot.press("6")  # Reports: this screen's own section
            await pilot.pause()
            assert opened == []
            await pilot.press("5")
            await pilot.pause()
            assert opened == [SECTIONS.index("Orchestration")]
            menu = screen.query_one("#stub-sections", ListView)
            menu.focus()
            await pilot.pause()
            await pilot.press("enter")  # Enter on the active entry is a no-op as well
            await pilot.pause()
            assert opened == [SECTIONS.index("Orchestration")]
            await pilot.press("up", "enter")
            await pilot.pause()
            assert opened[-1] == SECTIONS.index("Reports") - 1

    asyncio.run(scenario())


def test_escape_goes_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            assert app.screen is screen
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen

    asyncio.run(scenario())


def test_language_switch_relabels_menu_and_breadcrumbs(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], str, list[str], str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)

            def labels() -> list[str]:
                return [
                    str(item.query_one(Static).content)
                    for item in screen.query("#stub-sections > ListItem").results(
                        ListItem
                    )
                ]

            def crumbs() -> str:
                return screen.query_one("#shell-breadcrumbs", Breadcrumbs).text

            before = labels(), crumbs()
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            return before[0], before[1], labels(), crumbs()

    ru_labels, ru_crumbs, en_labels, en_crumbs = asyncio.run(scenario())
    assert ru_labels[5] == "5 Оркестрация" and ru_crumbs == "Главная  ›  Отчёты"
    assert en_labels[5] == "5 Orchestration" and en_crumbs == "Home  ›  Reports"
    assert ru_labels[0] == "0 Главная" and en_labels[0] == "0 Home"


def test_home_is_item_zero_and_keys_one_to_nine_keep_their_sections() -> None:
    assert SECTIONS == (
        "Home",
        "Open project",
        "Update Harness",
        "Diagnostics",
        "Harness",
        "Orchestration",
        "Reports",
        "Repo Map",
        "Configuration",
        "Help",
    )
    actions = {
        binding.key: binding.action
        for binding in ConsoleShell.BINDINGS
        if isinstance(binding, Binding)
    }
    for number in range(10):
        assert actions[str(number)] == f"section({number})"


def test_the_dashboard_highlights_home(tmp_path: Path) -> None:
    async def scenario() -> tuple[int | None, list[bool]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            menu = app.screen.query_one("#section-menu", ListView)
            return menu.index, [item.has_class("-active") for item in menu.children]

    index, active = asyncio.run(scenario())
    assert index == SECTIONS.index("Home")
    assert active == [True] + [False] * (len(SECTIONS) - 1)


@pytest.mark.parametrize("how", ["key", "menu"])
def test_home_from_another_screen_returns_to_the_dashboard_and_runs_nothing(
    tmp_path: Path, how: str
) -> None:
    async def scenario() -> tuple[bool, int]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _open(app, pilot)
            screen = await _open(app, pilot)  # two screens above the dashboard
            if how == "key":
                await pilot.press("0")
            else:
                menu = screen.query_one("#stub-sections", ListView)
                menu.focus()
                menu.index = SECTIONS.index("Home")
                await pilot.pause()
                await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            return isinstance(app.screen, DashboardScreen), len(app.screen_stack)

    on_dashboard, depth = asyncio.run(scenario())
    assert on_dashboard
    assert depth == 2  # the app's default screen and the dashboard


class _Recorder:
    """A command runner that remembers every call; navigation must make none."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


class DeepShell(StubShell):
    """A screen three levels deep, as the worker view is."""

    CRUMBS = "Главная › Оркестрация › Worker"

    def crumbs(self) -> list[Crumb]:
        return [
            Crumb("Главная", section="Home"),
            Crumb("Оркестрация", section="Orchestration"),
            Crumb("batch-31007025-83d2-4b27-9994-fda1b366f4c9", action="note"),
            Crumb("Worker / Наблюдение"),
        ]

    def action_note(self) -> None:
        self.noted = True


def test_every_breadcrumb_but_the_last_is_a_focusable_link(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[bool], str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 40)) as pilot:
            screen = DeepShell()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            crumbs = screen.query_one(Breadcrumbs)
            links = list(crumbs.query(CrumbLink))
            current = crumbs.query_one(".crumb-current", Static)
            return (
                [str(link.content) for link in links],
                [link.can_focus for link in links],
                str(current.content),
            )

    links, focusable, current = asyncio.run(scenario())
    assert links == [
        "Главная",
        "Оркестрация",
        "batch-31007025-83d2-4b27-9994-fda1b366f4c9",
    ]
    assert all(focusable)
    assert current == "Worker / Наблюдение"


@pytest.mark.parametrize("how", ["click", "enter"])
def test_the_home_crumb_returns_to_the_dashboard_and_runs_no_command(
    tmp_path: Path, how: str
) -> None:
    recorder = _Recorder()

    async def scenario() -> tuple[bool, int]:
        app = HarnessConsoleApp(tmp_path, command_runner=recorder)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            screen = await _open(app, pilot)
            home = screen.query(CrumbLink).first()
            if how == "click":
                await pilot.click(CrumbLink, offset=(1, 0))
            else:
                home.focus()
                await pilot.pause()
                await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            return isinstance(app.screen, DashboardScreen), len(app.screen_stack)

    on_dashboard, depth = asyncio.run(scenario())
    assert on_dashboard and depth == 2
    assert recorder.calls == []


def test_tab_reaches_the_crumb_links_and_an_action_crumb_runs_its_view(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[bool, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            screen = DeepShell()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            reached = False
            for _ in range(40):
                await pilot.press("tab")
                if isinstance(app.focused, CrumbLink):
                    reached = True
                    break
            batch = screen.query(CrumbLink).last()
            batch.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return reached, getattr(screen, "noted", False)

    reached, noted = asyncio.run(scenario())
    assert reached and noted


def test_a_narrow_terminal_collapses_the_middle_crumbs_into_an_ellipsis(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[str, list[str], list[str]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 40)) as pilot:
            screen = DeepShell()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            wide = screen.query_one(Breadcrumbs).text
            await pilot.resize_terminal(60, 30)
            await pilot.pause()
            await pilot.pause()
            crumbs = screen.query_one(Breadcrumbs)
            return (
                wide,
                [str(link.content) for link in crumbs.query(CrumbLink)],
                [
                    str(label.content)
                    for label in crumbs.query(".crumb-sep").results(Static)
                ],
            )

    wide, links, separators = asyncio.run(scenario())
    assert "…" not in wide and "batch-31007025" in wide
    assert links == [
        "Главная"
    ]  # only the first segment stays a link; the batch is folded away
    assert "…" in separators


def test_breadcrumbs_follow_the_language(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 40)) as pilot:
            screen = DeepShell()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            before = screen.query_one(Breadcrumbs).text
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            return before, screen.query_one(Breadcrumbs).text

    ru, en = asyncio.run(scenario())
    assert ru.startswith("Главная  ›  Оркестрация")
    assert en.startswith("Home  ›  Orchestration")


def test_field_values_wrap_at_separators_and_the_panel_reserves_a_scrollbar_gutter(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[str], list[int], str, int]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await _open(app, pilot)
            cells = list(
                screen.query_one("#stub-fields", FieldGrid)
                .query(".field")
                .results(Static)
            )
            texts = [str(cell.content) for cell in cells]
            return (
                texts,
                [cell.content_size.width for cell in cells],
                str(screen.query_one(ContextPanel).styles.scrollbar_gutter),
                len(cells),
            )

    texts, widths, gutter, count = asyncio.run(scenario())
    assert count == 3 and gutter == "stable"
    for text, width in zip(texts[:2], widths):
        value = text.split("\n", 1)[1]
        assert "".join(value.split("\n")) in (LONG_ID, LONG_PATH)
        lines = value.split("\n")
        assert len(lines) > 1
        assert all(len(line) <= width for line in lines)
        assert all(line[-1] in SEPARATORS for line in lines[:-1])
