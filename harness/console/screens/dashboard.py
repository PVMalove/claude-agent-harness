"""The console's home screen: what needs attention first, short cards, the full report on demand
and a numbered section menu - Diagnostics, Harness, Orchestration, Reports, Repo Map, Help."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Protocol

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, ListItem, ListView, Static

from .. import brand, i18n
from ..chrome import TopBar
from ..translate import Translated
from .. import data as console_data
from ..data import DashboardData
from ..runner import CommandRunner, capturing_runner
from .commands import call_from_worker

CONFIGURATION_SECTION = "Configuration"
SECTIONS = (
    "Open project",
    "Update Harness",
    "Diagnostics",
    "Harness",
    "Orchestration",
    "Reports",
    "Repo Map",
    CONFIGURATION_SECTION,
    "Help",
)


DRIFT_LABELS = {"clean": "нет", "drift": "есть", "missing": "не установлен"}
DRIFT_LABELS_EN = {"clean": "none", "drift": "yes", "missing": "not installed"}


class _CollectDashboard(Protocol):
    def __call__(self, repo: Path, /, *, online: bool = False) -> DashboardData: ...


def _counter(name: str, value: int, color: str) -> str:
    """A health counter, coloured only when it is non-zero so a clean report stays calm."""
    text = f"{name}={value}"
    return f"[{color}]{text}[/]" if value else f"[{brand.PALETTE['muted']}]{text}[/]"


def _summary_label(label: str) -> str:
    return f"[bold {brand.PALETTE['primary']}]{escape(label)}:[/]"


_DIGEST = re.compile(r"\b([0-9a-f]{12})[0-9a-f]{20,}\b")


def _render_summary(
    data: DashboardData, *, online: bool = False, language: str = "ru"
) -> str:
    return _DIGEST.sub(
        r"\1…", _render_summary_full(data, online=online, language=language)
    )


def _render_summary_full(
    data: DashboardData, *, online: bool = False, language: str = "ru"
) -> str:
    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    active_batches = (
        str(data.active_batches)
        if data.active_batches is not None
        else t("не подключено")
    )
    mode = "online" if online else "offline"
    # The Repo Map message joins its facts with "; ": one fact per line keeps it readable.
    first, *rest = data.repo_map_tier.split("; ")
    first = first.removeprefix("Repo Map").lstrip(": ")
    repo_map_lines = [f"{_summary_label('repo map')} {escape(first)}"]
    for part in rest:
        part = part.removeprefix("Repo Map ")
        label, separator, value = part.partition(": ")
        repo_map_lines.append(
            f"  {_summary_label(label)} {escape(value)}"
            if separator
            else f"  {escape(part)}"
        )
    memory = t("не определено")
    if data.memory_index is not None:
        check = data.memory_index
        color = brand.PALETTE[
            {"ok": "success", "warn": "warning", "fail": "error", "skipped": "muted"}[
                check.status
            ]
        ]
        memory = f"[{color}]{check.status}[/] — {escape(check.message)}"
    lines = [
        f"{_summary_label(f'health ({mode})')} "
        f"{_counter('ok', data.ok, brand.PALETTE['success'])} "
        f"{_counter('warn', data.warn, brand.PALETTE['warning'])} "
        f"{_counter('fail', data.fail, brand.PALETTE['error'])} "
        f"{_counter('skipped', data.skipped, brand.PALETTE['muted'])}",
        f"{_summary_label('active batches')} {active_batches}",
        "\n".join(repo_map_lines),
        f"{_summary_label('memory')} {memory}",
        f"{_summary_label('harness')} {escape(data.harness_version)} (drift: {escape(data.drift_state)})",
    ]
    if data.problems:
        lines.append("")
        lines.append(_summary_label(t("ошибки и предупреждения")))
        colors = {"fail": brand.PALETTE["error"], "warn": brand.PALETTE["warning"]}
        lines.extend(
            f"  [{colors.get(status, brand.PALETTE['muted'])}]{status:<4}[/] "
            f"{escape(check_id)} — {escape(message)}"
            for status, check_id, message in data.problems
        )
    return "\n".join(lines)


def _color(status: str) -> str:
    return brand.STATUS_COLORS[status]


def _mode(online: bool) -> str:
    return "online" if online else "offline"


def _repo_map_tier_name(data: DashboardData) -> str | None:
    found = re.search(r"tier=(\w+)", data.repo_map_tier)
    return found.group(1) if found else None


def _section_badges(data: DashboardData) -> dict[str, tuple[str, str]]:
    """A short status per menu entry: (text, colour). Sections without a status have no badge."""
    badges: dict[str, tuple[str, str]] = {}
    if data.fail:
        badges["Diagnostics"] = (
            f"{brand.STATUS_ICONS['fail']}{data.fail}",
            _color("fail"),
        )
    elif data.warn:
        badges["Diagnostics"] = (
            f"{brand.STATUS_ICONS['warn']}{data.warn}",
            _color("warn"),
        )
    else:
        badges["Diagnostics"] = (brand.STATUS_ICONS["ok"], _color("ok"))
    if data.drift_state == "clean":
        badges["Harness"] = (brand.STATUS_ICONS["ok"], _color("ok"))
    elif data.drift_state == "drift":
        badges["Harness"] = (brand.STATUS_ICONS["warn"], _color("warn"))
    if data.active_batches is not None:
        badges["Orchestration"] = (str(data.active_batches), brand.PALETTE["muted"])
    tier = _repo_map_tier_name(data)
    if tier is not None:
        badges["Repo Map"] = (
            tier,
            _color("ok") if tier == "full" else _color("warn"),
        )
    return badges


def _menu_label(
    number: int, key: str, badge: tuple[str, str] | None, language: str = "ru"
) -> Table:
    row = Table.grid(expand=True)
    row.add_column(ratio=1)
    row.add_column(justify="right")
    row.add_row(
        Text.assemble(
            (f"{number} ", brand.PALETTE["muted"]), i18n.section_label(language, key)
        ),
        Text(badge[0], style=badge[1]) if badge else Text(""),
    )
    return row


def _render_attention(
    data: DashboardData,
    *,
    online: bool,
    include_mode: bool = True,
    language: str = "ru",
) -> str:
    """Failed and warning checks first - the only thing on the home screen that asks for action."""
    muted = brand.PALETTE["muted"]
    if not data.problems:
        result = (
            f"[{_color('ok')}]{brand.STATUS_ICONS['ok']} "
            f"{i18n.tr_text(language, 'Проблем не найдено')}[/]"
        )
        if include_mode:
            mode = i18n.tr_text(language, f"{_mode(online)} · пропущено {data.skipped}")
            result += f" [{muted}]· {mode}[/]"
        return result
    lines = [
        f"[{_color(status)}]{brand.STATUS_ICONS.get(status, '·')}[/] "
        f"[bold]{escape(check_id)}[/]  {escape(message)}"
        for status, check_id, message in data.problems
    ]
    if include_mode:
        mode = i18n.tr_text(language, f"{_mode(online)} · пропущено {data.skipped}")
        lines.append(f"[{muted}]{mode}[/]")
    return "\n".join(lines)


def _render_cards(
    data: DashboardData,
    installed_version: str | None,
    installed_commit: str | None = None,
    capabilities: tuple[str, ...] = (),
    language: str = "ru",
) -> tuple[str, str, str]:
    """Contents of the Harness, Orchestration and Repo Map cards."""
    muted = brand.PALETTE["muted"]
    version = (
        f"[{muted}]Установлен[/] {escape(installed_version)}"
        if installed_version
        else (
            f"[{muted}]не установлен[/]"
            if data.drift_state == "missing"
            else f"[{muted}]Установлен[/] ?"
        )
    )
    drift_status = {"clean": "ok", "drift": "warn"}.get(data.drift_state, "skipped")
    harness = [version]
    if installed_commit:
        harness.append(f"[{muted}]commit[/] {escape(installed_commit)}")
    if capabilities:
        harness.append(f"[{muted}]{escape(' · '.join(capabilities))}[/]")
    drift_labels = DRIFT_LABELS if language == "ru" else DRIFT_LABELS_EN
    harness.append(
        f"дрейф: [{_color(drift_status)}]"
        f"{escape(drift_labels.get(data.drift_state, data.drift_state))}[/]"
    )
    if data.memory_index is not None:
        status = data.memory_index.status
        harness.append(f"память: [{_color(status)}]{status}[/]")
    orchestration = (
        f"активных batch: {data.active_batches}"
        if data.active_batches is not None
        else f"[{muted}]не подключено[/]"
    )
    tier = _repo_map_tier_name(data)
    repo_map = [f"tier: {escape(tier)}" if tier else f"[{muted}]не установлен[/]"]
    detail = re.search(r"\(([^)]*)\)", data.repo_map_tier.split("; ")[0])
    if detail:
        repo_map.append(f"[{muted}]{escape(detail.group(1))}[/]")

    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    return t("\n".join(harness)), t(orchestration), t("\n".join(repo_map))


def _render_mark() -> Text:
    return Text("\n".join(brand.LOGO), style=f"bold {brand.PALETTE['primary']}")


def _render_banner(info: brand.BannerInfo, language: str = "ru") -> Text:
    """Console version and project context beside the mark; the installed Harness is on its card."""
    blank, title, _installation, repo, branch = brand.banner_lines(info)
    banner = Text(blank + "\n")
    banner.append(title + "\n", style=f"bold {brand.PALETTE['primary']}")
    banner.append(repo + "\n", style=brand.PALETTE["foreground"])
    banner.append(i18n.tr_text(language, branch), style=brand.PALETTE["muted"])
    return banner


def _default_banner(repo: Path) -> brand.BannerInfo:
    return brand.collect_banner(repo, console_data.harness_version())


class DashboardScreen(Translated, Screen[None]):
    """Home screen: attention list and cards, the full report on demand, a numbered section menu."""

    # The section menu owns the keyboard on arrival.
    AUTO_FOCUS = "#section-menu"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (34, "-tall")]
    BINDINGS = [
        Binding("r", "checks_offline", "Офлайн"),
        Binding("o", "checks_online", "Онлайн"),
        Binding("p", "open_project", "Проект"),
        *(
            Binding(str(number), f"open_section({number - 1})", show=False)
            for number in range(1, len(SECTIONS) + 1)
        ),
    ]
    DEFAULT_CSS = """
    DashboardScreen { overflow: hidden hidden; }
    DashboardScreen #dashboard-content { width: 100%; height: 1fr; overflow-x: hidden; }
    DashboardScreen #brand { height: 5; margin: 0 1; padding: 0 1; overflow: hidden hidden; }
    DashboardScreen.-short #brand { display: none; }
    DashboardScreen #brand-mark { width: 15; height: 5; }
    DashboardScreen #brand-info {
        width: 1fr; height: 5; padding-left: 2;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    DashboardScreen #dashboard-body { height: auto; overflow: hidden hidden; }
    DashboardScreen.-wide #dashboard-body { min-height: 11; }
    DashboardScreen #dashboard-main { height: auto; }
    DashboardScreen #dashboard-side { height: auto; }
    DashboardScreen.-wide #dashboard-side { dock: left; width: 30; height: 100%; }
    DashboardScreen #dashboard-sections { height: auto; margin: 0 0 0 1; }
    DashboardScreen.-wide #dashboard-sections { height: 100%; }
    DashboardScreen #section-menu { height: auto; margin: 0; padding: 0; border: none; }
    DashboardScreen #section-menu:focus { border: none; }
    DashboardScreen.-wide #section-menu { height: auto; }
    DashboardScreen #section-menu > ListItem { height: 1; padding: 0; }
    DashboardScreen #dashboard-attention-actions Button { width: auto; margin-right: 1; }
    DashboardScreen #dashboard-attention-scroll { height: auto; max-height: 12; }
    DashboardScreen.-narrow #dashboard-attention-scroll { max-height: 6; }
    DashboardScreen #dashboard-attention { height: auto; }
    DashboardScreen #dashboard-attention-actions { height: 1; }
    DashboardScreen #dashboard-attention-mode { width: 1fr; padding-left: 1; color: $text-muted; }
    DashboardScreen #dashboard-cards { height: auto; margin: 0 1; }
    DashboardScreen.-narrow #dashboard-cards { display: none; }
    DashboardScreen .card:last-of-type { margin-right: 0; }
    DashboardScreen .card {
        width: 1fr; height: 7; padding: 0 1; margin-right: 1;
        border: round $panel-lighten-2; border-title-color: $primary;
    }
    DashboardScreen #dashboard-details { height: auto; max-height: 14; }
    DashboardScreen.-narrow #dashboard-details { max-height: 6; }
    DashboardScreen #dashboard-details Static { height: auto; }
    """

    def __init__(
        self,
        repo: Path,
        *,
        collect_dashboard: _CollectDashboard = console_data.collect_dashboard,
        collect_banner: Callable[[Path], brand.BannerInfo] = _default_banner,
        command_runner: CommandRunner = capturing_runner,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_dashboard = collect_dashboard
        self._collect_banner = collect_banner
        self._command_runner = command_runner
        self._installed_version: str | None = None
        self._installed_commit: str | None = None
        self._capabilities: tuple[str, ...] = ()
        self._language = "ru"
        self._data: DashboardData | None = None
        self._online = False
        self._banner: brand.BannerInfo | None = None
        self._badges: dict[str, tuple[str, str]] = {}
        self._sections: dict[str, Callable[[], Screen[None]]] = {
            "Diagnostics": self._diagnostics,
            "Harness": self._harness,
            "Orchestration": self._orchestration,
            "Reports": self._reports,
            "Repo Map": self._repo_map,
            CONFIGURATION_SECTION: self._configuration,
            "Help": self._help,
        }

    def compose(self) -> ComposeResult:
        banner = self._collect_banner(self.repo)
        self._installed_version = banner.installed_version
        self._installed_commit = banner.installed_commit
        self._capabilities = banner.capabilities
        data = self._collect_dashboard(self.repo)
        self._publish_counts(data)
        self._language = getattr(self.app, "language", "ru")
        self._data, self._online, self._banner = data, False, banner
        self._badges = _section_badges(data)
        harness, orchestration, repo_map = _render_cards(
            data,
            self._installed_version,
            self._installed_commit,
            self._capabilities,
            self._language,
        )
        yield TopBar("Главная")
        with VerticalScroll(id="dashboard-content"):
            with Horizontal(id="brand"):
                yield Static(_render_mark(), id="brand-mark")
                yield Static(
                    _render_banner(banner, self._language),
                    id="brand-info",
                    classes="no-i18n",
                )
            with Container(id="dashboard-body"):
                with Vertical(id="dashboard-side"):
                    with Vertical(id="dashboard-sections", classes="frame") as sections:
                        sections.border_title = brand.frame_title(
                            i18n.ui(self._language, "sections")
                        )
                        yield ListView(
                            *(
                                ListItem(
                                    Static(
                                        _menu_label(
                                            number,
                                            name,
                                            self._badges.get(name),
                                            self._language,
                                        )
                                    ),
                                    name=name,
                                    id={
                                        "Open project": "open-project",
                                        "Update Harness": "update-harness",
                                    }.get(name),
                                )
                                for number, name in enumerate(SECTIONS, start=1)
                            ),
                            id="section-menu",
                        )
                with Vertical(id="dashboard-main"):
                    with VerticalScroll(
                        id="dashboard-attention-scroll", classes="frame"
                    ) as scroll:
                        scroll.border_title = self._attention_title(data)
                        yield Static(
                            _render_attention(
                                data,
                                online=False,
                                include_mode=False,
                                language=self._language,
                            ),
                            id="dashboard-attention",
                            classes="no-i18n",
                        )
                        with Horizontal(id="dashboard-attention-actions"):
                            yield Button(
                                i18n.ui(self._language, "offline"),
                                id="dashboard-offline",
                                compact=True,
                            )
                            yield Button(
                                i18n.ui(self._language, "online"),
                                id="dashboard-online",
                                compact=True,
                            )
                            yield Static(
                                self._attention_mode(
                                    data, online=False, language=self._language
                                ),
                                id="dashboard-attention-mode",
                                classes="no-i18n",
                            )
                    with Horizontal(id="dashboard-cards"):
                        for card_id, title, text in (
                            ("card-harness", "Harness", harness),
                            ("card-orchestration", "Оркестрация", orchestration),
                            ("card-repo-map", "Repo Map", repo_map),
                        ):
                            card = Static(text, id=card_id, classes="card no-i18n")
                            card.border_title = brand.frame_title(title)
                            yield card
                    with VerticalScroll(
                        id="dashboard-details", classes="frame"
                    ) as details:
                        details.border_title = brand.frame_title("Подробный отчёт")
                        yield Static(
                            _render_summary(data, language=self._language),
                            id="dashboard-summary",
                            classes="no-i18n",
                        )
        yield Footer()

    def sync_language(self) -> None:
        """Re-label the section menu after the console language changed."""
        language = getattr(self.app, "language", "ru")
        if language == self._language:
            return
        self._language = language
        self._render_menu()
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the data-driven texts in the current language from the stored data."""
        data = getattr(self, "_data", None)
        if data is None or not self.is_mounted:
            return
        language = self._language
        harness, orchestration, repo_map = _render_cards(
            data,
            self._installed_version,
            self._installed_commit,
            self._capabilities,
            language,
        )
        self.query_one("#dashboard-attention", Static).update(
            _render_attention(
                data, online=self._online, include_mode=False, language=language
            )
        )
        self.query_one("#dashboard-attention-mode", Static).update(
            self._attention_mode(data, online=self._online, language=language)
        )
        self.query_one("#card-harness", Static).update(harness)
        self.query_one("#card-orchestration", Static).update(orchestration)
        self.query_one("#card-repo-map", Static).update(repo_map)
        self.query_one("#dashboard-summary", Static).update(
            _render_summary(data, online=self._online, language=language)
        )
        if self._banner is not None:
            self.query_one("#brand-info", Static).update(
                _render_banner(self._banner, language)
            )

    def on_screen_resume(self) -> None:
        self.sync_language()

    def _render_menu(self) -> None:
        for number, item in enumerate(self.query("#section-menu > ListItem"), start=1):
            name = item.name or ""
            item.query_one(Static).update(
                _menu_label(number, name, self._badges.get(name), self._language)
            )
        self.query_one("#dashboard-sections").border_title = brand.frame_title(
            i18n.ui(self._language, "sections")
        )
        for button_id, key in (
            ("dashboard-offline", "offline"),
            ("dashboard-online", "online"),
        ):
            button = self.query_one(f"#{button_id}", Button)
            button.label = i18n.ui(self._language, key)
            button.refresh(layout=True)

    @staticmethod
    def _attention_title(data: DashboardData) -> str:
        title = "Требует внимания"
        if data.problems:
            title += f" · {len(data.problems)}"
        return brand.frame_title(title)

    @staticmethod
    def _attention_mode(
        data: DashboardData, *, online: bool, language: str = "ru"
    ) -> str:
        return i18n.tr_text(language, f"{_mode(online)} · пропущено {data.skipped}")

    def _publish_counts(self, data: DashboardData) -> None:
        """Share the counters with the top bar of every screen."""
        setattr(
            self.app, "health_counts", (data.ok, data.warn, data.fail, data.skipped)
        )

    def _apply(
        self,
        data: DashboardData,
        *,
        online: bool,
        banner: brand.BannerInfo | None = None,
    ) -> None:
        if banner is not None:
            self._installed_version = banner.installed_version
            self._installed_commit = banner.installed_commit
            self._capabilities = banner.capabilities
            self._banner = banner
        self._data, self._online = data, online
        self._publish_counts(data)
        self._repaint()
        self.query_one(
            "#dashboard-attention-scroll"
        ).border_title = self._attention_title(data)
        self._badges = _section_badges(data)
        self._render_menu()
        self.query_one(TopBar).refresh_status()
        self.translate_now()

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        self.workers.cancel_node(self)

    def action_checks_offline(self) -> None:
        self._refresh_dashboard(online=False)

    def action_checks_online(self) -> None:
        self._refresh_dashboard(online=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dashboard-offline":
            self.action_checks_offline()
        elif event.button.id == "dashboard-online":
            self.action_checks_online()

    async def action_update_harness(self) -> None:
        from .update_harness import UpdateHarnessScreen
        from ..app import HarnessConsoleApp

        if isinstance(self.app, HarnessConsoleApp) and self.app.commands_running:
            self.notify("Дождитесь окончания команды", severity="warning")
            return
        screen = UpdateHarnessScreen(self.repo, command_runner=self._command_runner)
        await self.app.push_screen(screen, lambda _result: self._refresh_dashboard())

    async def action_open_section(self, index: int) -> None:
        name = SECTIONS[index]
        menu = self.query_one("#section-menu", ListView)
        menu.index = index
        menu.focus()
        await self._open_section(name)

    def action_open_project(self) -> None:
        from .project_setup import ProjectSetupScreen
        from ..app import HarnessConsoleApp

        if isinstance(self.app, HarnessConsoleApp) and self.app.commands_running:
            self.notify("Дождитесь окончания команды", severity="warning")
            return

        def selected(repo: Path | None) -> None:
            if repo is not None and isinstance(self.app, HarnessConsoleApp):
                self.app.activate_project(repo)

        self.app.push_screen(
            ProjectSetupScreen(self.repo, command_runner=self._command_runner),
            selected,
        )

    def _refresh_dashboard(self, *, online: bool = False) -> None:
        attention = self.query_one("#dashboard-attention", Static)
        summary = self.query_one("#dashboard-summary", Static)
        running = (
            "онлайн-проверки выполняются…" if online else "офлайн-проверки выполняются…"
        )
        attention.update(f"⏳ {running}")
        summary.update(running)

        def work() -> None:
            try:
                data = self._collect_dashboard(self.repo, online=online)
                banner = self._collect_banner(self.repo)
            except Exception as exc:
                # An unhandled worker error would exit the whole TUI; show it in the screen instead.
                text = f"проверки не выполнены: {escape(repr(exc))}"

                def show_error() -> None:
                    attention.update(f"[{_color('fail')}]{text}[/]")
                    summary.update(text)

                call_from_worker(self, show_error)
                return
            call_from_worker(
                self, lambda: self._apply(data, online=online, banner=banner)
            )

        self.run_worker(work, thread=True, exclusive=True, group="dashboard-health")

    async def _open_section(self, name: str) -> None:
        if name == "Open project":
            self.action_open_project()
            return
        if name == "Update Harness":
            await self.action_update_harness()
            return
        open_screen = self._sections.get(name)
        if open_screen is None:
            return
        if name == CONFIGURATION_SECTION:
            self.app.push_screen(
                open_screen(), lambda _result: self._refresh_dashboard()
            )
        else:
            self.app.push_screen(open_screen())

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        await self._open_section(event.item.name or "")

    def _diagnostics(self) -> Screen[None]:
        from .diagnostics import DiagnosticsScreen

        return DiagnosticsScreen(self.repo)

    def _harness(self) -> Screen[None]:
        from .harness import HarnessScreen

        return HarnessScreen(self.repo, command_runner=self._command_runner)

    def _orchestration(self) -> Screen[None]:
        from .orchestration import OrchestrationScreen

        return OrchestrationScreen(self.repo, command_runner=self._command_runner)

    def _reports(self) -> Screen[None]:
        from .reports import ReportsScreen

        return ReportsScreen(self.repo)

    def _repo_map(self) -> Screen[None]:
        from .repo_map import RepoMapScreen

        return RepoMapScreen(self.repo, command_runner=self._command_runner)

    def _configuration(self) -> Screen[None]:
        from .configuration import ConfigurationScreen

        return ConfigurationScreen(self.repo)

    def _help(self) -> Screen[None]:
        from .help import HelpScreen

        return HelpScreen()
