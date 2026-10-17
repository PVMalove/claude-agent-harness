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
from harness.console.data import DashboardData
from harness.console.screens.dashboard import DashboardScreen
from harness.console.crumbs import Crumb
from harness.console.wrap import SEPARATORS
from tests.console._pilot_wait import crumb_text
from harness.console.shell import (
    SECTIONS,
    Breadcrumbs,
    CrumbLink,
    ConsoleShell,
    ContextPanel,
    FieldGrid,
    Fold,
    JumpLink,
    SectionCard,
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
            assert crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs)) == (
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


def _footer_keys(screen: object) -> list[str]:
    from textual.widgets._footer import FooterKey

    return [key.key for key in screen.query(FooterKey)]  # type: ignore[attr-defined]


def _home_data() -> DashboardData:
    return DashboardData(
        ok=5,
        warn=0,
        fail=2,
        skipped=1,
        repo_map_tier="Repo Map: tier=full (offline parser bundle detected)",
        harness_version="9.9.9",
        drift_state="drift",
        active_batches=3,
        problems=(
            ("fail", "files.skill_snapshot", "snapshot differs"),
            ("fail", "files.integrations", "integrations need a check"),
        ),
    )


def _home(tmp_path: Path) -> DashboardScreen:
    return DashboardScreen(
        tmp_path, collect_dashboard=lambda _repo, *, online=False: _home_data()
    )


def test_escape_on_home_keeps_the_screen_and_the_footer_has_no_back_key(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[bool, list[str], list[str], bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            home = _home(tmp_path)
            await app.switch_screen(home)
            await pilot.pause()
            home_keys = _footer_keys(home)
            await pilot.press("escape")
            await pilot.pause()
            stayed = app.screen is home
            other = await _open(app, pilot)
            other_keys = _footer_keys(other)
            await pilot.press("escape")
            await pilot.pause()
            return stayed, home_keys, other_keys, app.screen is home

    stayed, home_keys, other_keys, back_home = asyncio.run(scenario())
    assert stayed
    assert "escape" not in home_keys
    assert "escape" in other_keys
    assert back_home


LONG_BRANCH = "feature/issue-721-console-design-system-with-a-long-tail"


def _git_repo(path: Path, branch: str) -> Path:
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True
    )
    return path


@pytest.mark.parametrize("branch", [LONG_BRANCH, None])
@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
def test_the_top_bar_shows_project_and_branch_on_every_width(
    tmp_path: Path, size: tuple[int, int], branch: str | None
) -> None:
    repo = _git_repo(tmp_path / "project", branch) if branch else tmp_path

    async def scenario() -> tuple[str, bool]:
        app = HarnessConsoleApp(repo)
        async with app.run_test(size=size) as pilot:
            screen = await _open(app, pilot)
            right = screen.query_one("#topbar-right", Static)
            return str(right.render()), right.display and right.region.width > 0

    text, shown = asyncio.run(scenario())
    assert shown
    assert not text.endswith("·") and " · " not in text.replace(" · ⎇", "")
    if branch is None:
        assert text == repo.name
    elif size[0] >= 100:
        assert text == f"{repo.name} · ⎇ {LONG_BRANCH}"
    else:
        # Narrow: the branch alone, shortened to the room the bar has.
        assert text.startswith("⎇ feature/issue-721") and text.endswith("…")


def test_where_text_never_leaves_a_dangling_separator() -> None:
    from harness.console.chrome import where_text

    assert where_text("project", None, 160) == "project"
    assert where_text("project", "", 80) == "project"
    assert where_text("project", "main", 160) == "project · ⎇ main"
    assert where_text("project", "main", 80) == "⎇ main"
    assert len(where_text("project", LONG_BRANCH, 80)) <= (80 - 4) // 2


def _color(widget: object) -> str:
    from textual.color import Color

    return Color.from_rich_color(widget.rich_style.color).hex.lower()  # type: ignore[attr-defined]


def test_shell_text_uses_the_foreground_token(tmp_path: Path) -> None:
    from harness.console import brand

    async def scenario() -> list[str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            home = _home(tmp_path)
            await app.switch_screen(home)
            await pilot.pause()
            colors = [
                _color(home.query_one("#topbar-left")),
                _color(home.query_one("#dashboard-offline")),
                _color(home.query_one("#dashboard-online")),
            ]
            screen = await _open(app, pilot)
            colors.append(_color(screen.query_one(".crumb-current")))
            return colors

    top, offline, online, crumb = asyncio.run(scenario())
    assert top == online == crumb == brand.PALETTE["foreground"].lower()
    # The current mode of the Offline | Online toggle is drawn in the selection colours.
    assert offline == brand.SHELL_TOKENS["selection-foreground"]


def test_the_shell_css_has_no_default_text_token() -> None:
    import re

    from harness.console import brand
    from harness.console.chrome import WorkspaceHeader
    from harness.console.shell import SHELL_CSS

    for css in (SHELL_CSS, WorkspaceHeader.DEFAULT_CSS, DashboardScreen.CSS):
        assert not re.search(r"\$text(?![\w-])", brand.shell_css(css))


@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
def test_the_crumb_row_starts_with_the_menu_toggle_and_home_has_one_crumb(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    async def scenario() -> tuple[str, bool, bool, str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            home = _home(tmp_path)
            await app.switch_screen(home)
            await pilot.pause()
            row = home.query_one(".crumb-row")
            toggle = row.children[0]
            crumbs = home.query_one(Breadcrumbs)
            assert isinstance(toggle, Button) and toggle.id == "dashboard-menu"
            left_of_crumbs = toggle.region.right <= crumbs.region.x
            ru = crumb_text(crumbs)
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            return (
                str(toggle.label),
                toggle.display and left_of_crumbs,
                not home.query(".context-strip .menu-toggle"),
                ru,
                crumb_text(crumbs),
            )

    label, toggle_first, strip_free, ru, en = asyncio.run(scenario())
    assert label == "≡"
    assert toggle_first == (size[0] < 100)
    assert strip_free
    assert (ru, en) == ("Главная", "Home")


def _region_text(app: HarnessConsoleApp, widget: object) -> list[str]:
    """The cells `widget` occupies on screen, row by row, as plain text."""
    region = widget.region  # type: ignore[attr-defined]
    strips = app.screen._compositor.render_strips()
    lines = ["".join(segment.text for segment in strip) for strip in strips]
    return [line[region.x : region.right] for line in lines[region.y : region.bottom]]


def _menu_text(item: ListItem) -> str:
    """The number and name of a menu entry, without its badge."""
    label = item.query_one(Static).content
    return str(label.columns[0]._cells[0])  # type: ignore[union-attr]


def _menu_item(screen: ConsoleShell, name: str) -> ListItem:
    return next(
        item
        for item in screen.query("ShellSidebar ListItem").results(ListItem)
        if item.name == name
    )


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", [(160, 52), (100, 30), (80, 24)])
def test_the_main_menu_fits_update_harness_and_its_badge_on_one_line(
    tmp_path: Path, size: tuple[int, int], language: str
) -> None:
    from harness.console import brand

    async def scenario() -> tuple[int, str, list[str], list[str], object]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            home = _home(tmp_path)
            await app.switch_screen(home)
            await pilot.pause()
            if language == "en":
                await pilot.press("f2")
                await pilot.pause()
            if size[0] < 100:
                await pilot.click("#dashboard-menu")
                await pilot.pause()
            sidebar = home.query_one(ShellSidebar)
            update = _menu_item(home, "Update Harness").query_one(Static)
            selected = _menu_item(home, "Home")
            return (
                sidebar.region.width,
                str(sidebar.query_one(".menu-heading", Static).render()),
                _region_text(app, update),
                _region_text(app, selected.query_one(Static)),
                selected.styles,
            )

    width, heading, update, selected, styles = asyncio.run(scenario())
    # 27 columns between 100 and 140 columns, where the content needs the room.
    assert width == (27 if 100 <= size[0] < 140 else 30)
    assert heading == ("ГЛАВНОЕ МЕНЮ" if language == "ru" else "MAIN MENU")
    label = "2 Обновить Harness" if language == "ru" else "2 Update Harness"
    assert len(update) == 1
    assert update[0].startswith(label) and update[0].rstrip().endswith("⚠")
    assert selected[0].startswith("0 ")
    # The selected entry: the selection fill, an outline on a wide terminal, no left bar.
    tokens = brand.SHELL_TOKENS
    assert styles.background.hex.lower() == tokens["selection-background"]  # type: ignore[attr-defined]
    assert styles.border_left[0] in ("", "none")  # type: ignore[attr-defined]
    outline = styles.outline_top  # type: ignore[attr-defined]
    if size[1] >= 38:
        assert outline[0] == "round"
        assert outline[1].hex.lower() == tokens["selection-outline"]
    else:
        assert outline[0] in ("", "none")


def test_a_menu_number_takes_the_colour_of_its_entry() -> None:
    from rich.text import Text

    from harness.console.shell import menu_label

    label = menu_label(2, "Update Harness", ("⚠", "#E5A04B"))
    name, badge = (column._cells[0] for column in label.columns)
    assert isinstance(name, Text) and name.plain == "2 Обновить Harness"
    assert not name.style and not name.spans
    assert isinstance(badge, Text) and badge.plain.strip() == "⚠"


def test_section_badges_come_from_the_health_facts() -> None:
    from harness.console import brand
    from harness.console.shell import section_badges

    badges = section_badges(_home_data())
    assert badges == {
        "Diagnostics": ("✖ 2", brand.STATUS_COLORS["fail"]),
        "Harness": ("⚠", brand.STATUS_COLORS["warn"]),
        "Update Harness": ("⚠", brand.STATUS_COLORS["warn"]),
        "Orchestration": ("3", brand.PALETTE["muted"]),
        "Repo Map": ("full", brand.STATUS_COLORS["ok"]),
    }
    clean = DashboardData(
        ok=1,
        warn=1,
        fail=0,
        skipped=0,
        repo_map_tier="Repo Map не установлен в проект",
        harness_version="9.9.9",
        drift_state="clean",
        active_batches=None,
    )
    assert section_badges(clean) == {
        "Diagnostics": ("⚠ 1", brand.STATUS_COLORS["warn"]),
        "Harness": ("✔", brand.STATUS_COLORS["ok"]),
    }


def test_the_menu_badges_are_shown_on_another_shell_screen(tmp_path: Path) -> None:
    from harness.console.screens.diagnostics import DiagnosticsScreen
    from harness.health.model import Report

    report = Report(schema_version=1, repo=str(tmp_path), online=False, checks=[])

    async def scenario() -> tuple[list[str], list[str]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await app.switch_screen(_home(tmp_path))
            await pilot.pause()
            diagnostics = DiagnosticsScreen(
                tmp_path, collect_diagnostics=lambda _repo, *, online=False: report
            )
            await app.push_screen(diagnostics)
            await pilot.pause()
            await pilot.pause()
            return (
                _region_text(
                    app, _menu_item(diagnostics, "Diagnostics").query_one(Static)
                ),
                _region_text(
                    app, _menu_item(diagnostics, "Orchestration").query_one(Static)
                ),
            )

    diagnostics, orchestration = asyncio.run(scenario())
    assert diagnostics[0].startswith("3 Диагностика")
    assert diagnostics[0].rstrip().endswith("✖ 2")
    assert orchestration[0].rstrip().endswith("3")


def test_a_section_title_is_in_capitals_with_its_note_after_it() -> None:
    from harness.console import brand, i18n

    assert brand.section_title("Требует внимания", "2") == " ТРЕБУЕТ ВНИМАНИЯ  2 "
    assert brand.section_title(" Действия ") == " ДЕЙСТВИЯ "
    # A title in capitals still finds its English text.
    assert i18n.tr("en", "HEALTH-ОТЧЁТ") == "HEALTH REPORT"
    assert i18n.tr("en", "СОСТОЯНИЕ · 3") == "STATUS · 3"
    assert i18n.tr("ru", "HEALTH-ОТЧЁТ") == "HEALTH-ОТЧЁТ"


class CardShell(StubShell):
    """A shell screen whose page is one section card and one fold."""

    def compose_content(self) -> ComposeResult:
        yield SectionCard(
            Static("Содержимое", id="card-body"),
            title="Проверки",
            note="3",
            id="stub-card",
        )
        yield Fold(
            Static("Подробности", id="fold-body"),
            title="Подробный отчёт",
            summary="27 проверок · 11 пропущено",
            id="stub-fold",
        )


def test_section_cards_and_folds_follow_the_language_with_muted_titles(
    tmp_path: Path,
) -> None:
    from textual.widgets._collapsible import CollapsibleTitle

    from harness.console import brand

    async def scenario() -> tuple[list[str], list[str], str, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = CardShell()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            card = screen.query_one("#stub-card", SectionCard)
            fold = screen.query_one("#stub-fold", Fold)
            title = fold.query_one(CollapsibleTitle)

            def texts() -> list[str]:
                return [str(card.border_title), str(title.render())]

            ru = texts()
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            en = texts()
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            assert texts() == ru
            title.focus()
            await pilot.press("enter")
            await pilot.pause()
            return ru, en, card.styles.border_title_color.hex, not fold.collapsed

    ru, en, color, opened = asyncio.run(scenario())
    assert ru[0] == " ПРОВЕРКИ  3 "
    # The summary sits at the right end of the fold's title row.
    assert ru[1].startswith("▸ Подробный отчёт  ")
    assert ru[1].endswith("  27 проверок · 11 пропущено")
    assert len(ru[1]) > len("▸ Подробный отчёт  27 проверок · 11 пропущено") + 20
    assert en[0] == " CHECKS  3 " and en[1].startswith("▸ Detailed report  ")
    assert color.lower() == brand.PALETTE["muted"].lower()
    assert opened


def test_frame_titles_of_shell_screens_are_muted_capitals(tmp_path: Path) -> None:
    from harness.console import brand
    from harness.console.screens.diagnostics import DiagnosticsScreen
    from harness.health.model import Report

    report = Report(schema_version=1, repo=str(tmp_path), online=False, checks=[])

    async def scenario() -> tuple[str, str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = DiagnosticsScreen(
                tmp_path, collect_diagnostics=lambda _repo, *, online=False: report
            )
            await app.push_screen(screen)
            await pilot.pause()
            frame = screen.query_one("#diagnostics-scroll")
            ru = str(frame.border_title)
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            return ru, str(frame.border_title), frame.styles.border_title_color.hex

    ru, en, color = asyncio.run(scenario())
    assert (ru, en) == (" HEALTH-ОТЧЁТ ", " HEALTH REPORT ")
    assert color.lower() == brand.PALETTE["muted"].lower()


@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
def test_a_narrow_footer_shows_the_mockup_keys_and_hidden_keys_still_work(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    calls: list[bool] = []

    def collect(_repo: Path, *, online: bool = False) -> DashboardData:
        calls.append(online)
        return _home_data()

    async def scenario() -> set[str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            home = DashboardScreen(tmp_path, collect_dashboard=collect)
            await app.switch_screen(home)
            await pilot.pause()
            keys = set(_footer_keys(home)) - {app.COMMAND_PALETTE_BINDING}
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return keys

    keys = asyncio.run(scenario())
    assert keys == ({"p", "f1", "f2"} if size[0] < 100 else {"r", "o", "p", "f1", "f2"})
    # `r` is not in the narrow footer, but it still reruns the offline checks.
    assert calls == [False, False]


def test_the_footer_follows_a_resize_across_the_narrow_threshold(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[set[str], set[str]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            home = _home(tmp_path)
            await app.switch_screen(home)
            await pilot.pause()
            wide = set(_footer_keys(home))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.pause()
            return wide, set(_footer_keys(home))

    wide, narrow = asyncio.run(scenario())
    assert {"r", "o"} <= wide
    assert not {"r", "o"} & narrow and "p" in narrow


def test_every_health_check_has_a_readable_title_and_a_menu_section() -> None:
    from harness.console import i18n
    from harness.console.shell import CHECK_ROUTES, check_route
    from harness.health.registry import REGISTRY

    ids = [check_id for check_id, _check in REGISTRY]
    assert set(ids) == set(CHECK_ROUTES)
    for check_id in ids:
        route = check_route(check_id)
        assert route.section in SECTIONS and route.section != "Home", check_id
        assert route.title != check_id
        assert i18n.tr("en", route.title) != route.title, route.title
    assert check_route("files.skill_snapshot").section == "Update Harness"
    assert check_route("files.integrations").section == "Configuration"
    unknown = check_route("net.something")
    assert (unknown.title, unknown.section) == ("net.something", "Diagnostics")


def test_a_jump_is_labelled_by_its_section_or_its_action() -> None:
    from harness.console import i18n

    assert i18n.jump_label("ru", "Update Harness") == "Обновить Harness"
    assert i18n.jump_label("en", "Update Harness") == "Update Harness"
    assert i18n.jump_label("ru", "Configuration") == "Открыть настройку"
    assert i18n.jump_label("en", "Configuration") == "Open setup"


class JumpShell(StubShell):
    def compose_content(self) -> ComposeResult:
        yield JumpLink("Update Harness", id="jump-update")
        yield JumpLink("Configuration", id="jump-configuration")


@pytest.mark.parametrize("how", ["click", "enter"])
def test_a_jump_link_opens_its_section_and_runs_no_command(
    tmp_path: Path, how: str
) -> None:
    from harness.console.screens.update_harness import UpdateHarnessScreen

    recorder = _Recorder()

    async def scenario() -> tuple[list[str], list[str], bool]:
        app = HarnessConsoleApp(tmp_path, command_runner=recorder)
        async with app.run_test(size=(160, 52)) as pilot:
            await app.switch_screen(
                DashboardScreen(
                    tmp_path,
                    collect_dashboard=lambda _repo, *, online=False: _home_data(),
                    command_runner=recorder,
                )
            )
            await pilot.pause()
            screen = JumpShell()
            await app.push_screen(screen)
            await pilot.pause()
            links = [str(link.render()) for link in screen.query(JumpLink)]
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            english = [str(link.render()) for link in screen.query(JumpLink)]
            if how == "click":
                await pilot.click("#jump-update")
            else:
                screen.query_one("#jump-update", JumpLink).focus()
                await pilot.press("enter")
            for _ in range(3):
                await pilot.pause()
            return links, english, isinstance(app.screen, UpdateHarnessScreen)

    links, english, opened = asyncio.run(scenario())
    assert links == ["Обновить Harness  2 →", "Открыть настройку  8 →"]
    assert english == ["Update Harness  2 →", "Open setup  8 →"]
    assert opened
    assert recorder.calls == []


def test_a_jump_link_shows_only_the_number_on_a_narrow_terminal(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = JumpShell()
            await app.push_screen(screen)
            await pilot.pause()
            return [str(link.render()) for link in screen.query(JumpLink)]

    assert asyncio.run(scenario()) == ["2 →", "8 →"]


def test_language_switch_relabels_menu_and_breadcrumbs(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], str, list[str], str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)

            def labels() -> list[str]:
                return [
                    _menu_text(item)
                    for item in screen.query("#stub-sections > ListItem").results(
                        ListItem
                    )
                ]

            def crumbs() -> str:
                return crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))

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
            wide = crumb_text(screen.query_one(Breadcrumbs))
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
            before = crumb_text(screen.query_one(Breadcrumbs))
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            return before, crumb_text(screen.query_one(Breadcrumbs))

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


class ResultShell(ConsoleShell[str]):
    """A shell screen that hands a string back, like Open project hands back a path."""

    SECTION = "Reports"
    CRUMBS = "Главная › Отчёты"


def test_a_shell_screen_is_dismissed_with_its_own_result_type(tmp_path: Path) -> None:
    results: list[str | None] = []

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = ResultShell()
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.dismiss("chosen")
            await pilot.pause()
            await pilot.pause()
            assert app.screen is not screen

    asyncio.run(scenario())
    assert results == ["chosen"]


def test_the_four_screens_carry_no_local_colours_top_bar_or_number_keys() -> None:
    """The Home, Open project, Diagnostics and Harness screens take colours, the top bar and the
    navigation from the shell: their sources hold none of their own."""
    import re

    screens = Path(__file__).resolve().parents[2] / "harness" / "console" / "screens"
    names = ("dashboard", "harness", "diagnostics", "project_setup", "commands")
    for name in names:
        source = (screens / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"#[0-9A-Fa-f]{6}\b", source), f"{name}: a hex colour"
        assert not re.search(r"Binding\(\s*[\"']\d", source), f"{name}: a number key"
        if name != "commands":
            # `CommandMenuScreen.compose` is the one left-over of the old frame (no screen uses it).
            assert "TopBar(" not in source, f"{name}: a top bar of its own"
