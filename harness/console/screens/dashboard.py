"""The console's home screen: what needs attention first, short cards, the full report on demand
and a numbered section menu - Diagnostics, Harness, Orchestration, Reports, Repo Map, Help."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, ListItem, ListView, Static

from .. import brand, i18n
from ..chrome import TopBar
from ..session_card import elapsed_text, session_row
from ..shell import (
    CONFIGURATION_SECTION,
    HOME_SECTION,
    SECTIONS,
    SHELL_CSS,
    ConsoleShell,
    ShellIds,
    ElidedPath,
    Fold,
    JumpLink,
    SectionCard,
    check_route,
    fact_table,
    repo_map_tier_name,
    section_badges,
)
from .. import reports
from .. import data as console_data
from ..data import DashboardData
from ..repo_map import RepoMapView, cached_map, head_commit
from ..repo_map_settings import MemoryStatus, memory_status
from ..runner import CommandRunner, capturing_runner
from .commands import call_from_worker

DRIFT_LABELS = {"clean": "нет", "drift": "есть", "missing": "не установлен"}
DRIFT_LABELS_EN = {"clean": "none", "drift": "yes", "missing": "not installed"}


class _CollectDashboard(Protocol):
    def __call__(self, repo: Path, /, *, online: bool = False) -> DashboardData: ...


@dataclass(frozen=True)
class HomeContext:
    """What the context cards show beside the health facts: the Repo Map cached for HEAD and the
    state of project memory. Either is None when the console cannot read it."""

    repo_map: RepoMapView | None = None
    memory: MemoryStatus | None = None


def collect_context(repo: Path) -> HomeContext:
    """Read the Repo Map cached for the project's HEAD and the memory status; builds nothing."""
    head = head_commit(repo)
    return HomeContext(
        repo_map=cached_map(repo, head) if head else None, memory=memory_status(repo)
    )


def _status_label(status: str) -> str:
    """A status as its symbol plus its word, so it never rests on colour alone: `✖ fail`."""
    return f"{brand.STATUS_ICONS.get(status, '·')} {status}"


def _counter(name: str, value: int, color: str) -> str:
    """A health counter with its status symbol, coloured only when it is non-zero so a clean
    report stays calm."""
    text = f"{brand.STATUS_ICONS.get(name, '·')} {name}={value}"
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
        memory = f"[{color}]{_status_label(check.status)}[/] — {escape(check.message)}"
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
            f"  [{colors.get(status, brand.PALETTE['muted'])}]{_status_label(status):<6}[/] "
            f"{escape(check_id)} — {escape(message)}"
            for status, check_id, message in data.problems
        )
    return "\n".join(lines)


def _color(status: str) -> str:
    return brand.STATUS_COLORS[status]


def _mode(online: bool) -> str:
    return "online" if online else "offline"


def _tokens(language: str, count: int) -> str:
    """`38 400 токенов` / `38,400 tokens`: thousands grouped as each language writes them."""
    _count, noun = i18n.count_noun(
        language, count, ("токен", "токена", "токенов"), ("token", "tokens")
    ).split(" ", 1)
    grouped = f"{count:,}"
    return f"{grouped.replace(',', ' ') if language == 'ru' else grouped} {noun}"


def _value(text: str, status: str | None = None, language: str = "ru") -> Text:
    """A fact's value in `language`; a status value carries its symbol and colour."""
    text = i18n.tr_text(language, text)
    if status is None:
        return Text(text)
    return Text(f"{brand.STATUS_ICONS[status]} {text}", style=_color(status))


def home_facts(
    data: DashboardData,
    banner: brand.BannerInfo,
    context: HomeContext,
    project: str,
    language: str = "ru",
) -> list[tuple[str, str, list[tuple[str, Text]]]]:
    """The context cards of the home screen, `(id, title, [(label, value)])`, filled only from facts
    the console reads: health checks, the ledger, the cached Repo Map and the memory policy. A fact
    the console does not have is `—`, never an example."""

    def plain(text: str) -> Text:
        return _value(text, language=language)

    missing = Text("—")
    if banner.installed_version or banner.installed_commit:
        installed = plain(
            " · ".join(
                part
                for part in (banner.installed_version, banner.installed_commit)
                if part
            )
        )
    elif data.drift_state == "missing":
        installed = plain("не установлен")
    else:
        installed = missing
    drift_status = {"clean": "ok", "drift": "warn"}.get(data.drift_state, "skipped")
    drift_labels = DRIFT_LABELS if language == "ru" else DRIFT_LABELS_EN
    drift = Text(
        f"{brand.STATUS_ICONS[drift_status]} "
        f"{drift_labels.get(data.drift_state, i18n.tr_text(language, data.drift_state))}",
        style=_color(drift_status),
    )
    batches = (
        plain(str(data.active_batches))
        if data.active_batches is not None
        else plain("не подключено")
    )
    tier = repo_map_tier_name(data.repo_map_tier)
    view = context.repo_map
    if view is None:
        repo_map = [
            ("Уровень", plain(tier) if tier else plain("не установлен")),
            ("Актуальность", _value("нет карты для HEAD", "skipped", language)),
        ]
    else:
        repo_map = [
            ("Уровень", plain(tier or view.tier)),
            ("Актуальность", _value("карта для HEAD", "ok", language)),
            ("Файлов", plain(str(len(view.files)))),
            ("Связей", plain(str(len(view.edges)))),
            ("Источник", plain(view.origin)),
            ("Объём", plain(f"≈{_tokens(language, view.estimated_tokens)}")),
        ]
    memory = context.memory
    check = data.memory_index
    state = (
        _value(check.message, check.status, language) if check is not None else missing
    )
    if memory is None:
        memory_rows = [("Состояние", state)]
    else:
        indexed = (
            plain(
                i18n.count_noun(
                    language,
                    memory.indexed,
                    ("документ", "документа", "документов"),
                    ("document", "documents"),
                )
            )
            if memory.indexed is not None
            else missing
        )
        limit = (
            plain(f"{memory.top_k} · {_tokens(language, memory.max_tokens)}")
            if memory.top_k is not None and memory.max_tokens is not None
            else missing
        )
        memory_rows = [
            ("Состояние", state),
            ("Индекс", indexed),
            (
                "Источники",
                plain(" · ".join(memory.source_types))
                if memory.source_types
                else missing,
            ),
            ("Лимит поиска", limit),
        ]
    return [
        (
            "card-project",
            "Контекст проекта",
            [("Проект", plain(project)), ("Ветка", Text(banner.branch or "—"))],
        ),
        (
            "card-harness",
            "Harness",
            [
                ("Установлен", installed),
                (
                    "Модули",
                    plain(" · ".join(banner.capabilities))
                    if banner.capabilities
                    else missing,
                ),
                ("Дрейф", drift),
            ],
        ),
        ("card-orchestration", "Оркестрация", [("Активных batch", batches)]),
        ("card-repo-map", "Repo Map", repo_map),
        ("card-memory", "Память проекта", memory_rows),
    ]


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


class DashboardScreen(ConsoleShell):
    """Home screen: attention list and cards, the full report on demand, a numbered section menu."""

    # The section menu owns the keyboard on arrival.
    AUTO_FOCUS = "#section-menu"
    SECTION = HOME_SECTION
    CRUMBS = "Главная"
    HAS_CONTEXT = True
    # Home is the bottom of the screen stack: Esc has nowhere to go.
    CAN_GO_BACK = False
    NARROW_FOOTER_KEYS = ("p", "f1", "f2")
    # Beside the content from 100 columns; narrower, one folded card under it.
    CONTEXT_SIDE_MIN = brand.NARROW_WIDTH
    CONTEXT_TITLE = "Контекст проекта"
    CONTEXT_SUMMARY = "ветка, Harness, Repo Map, память"
    IDS = ShellIds(
        navigation="dashboard-sections",
        menu="section-menu",
        content="dashboard-main",
        breadcrumbs="dashboard-breadcrumbs",
        toggle="dashboard-menu",
        body="dashboard-body",
        context="dashboard-context",
    )
    BINDINGS = [
        Binding("r", "checks_offline", "Офлайн"),
        Binding("o", "checks_online", "Онлайн"),
        Binding("p", "open_project", "Проект"),
    ]
    DEFAULT_CSS = brand.shell_css("""
    DashboardScreen { overflow: hidden hidden; }
    DashboardScreen #dashboard-overview { width: 1fr; height: 1fr; padding: 1 3; overflow-y: auto; }
    DashboardScreen #dashboard-title { height: 2; text-style: bold; }
    DashboardScreen #dashboard-subtitle { height: 2; color: $text-muted; }
    DashboardScreen #dashboard-attention-card { margin-bottom: 1; }
    DashboardScreen #dashboard-attention, DashboardScreen #dashboard-problems { height: auto; }
    DashboardScreen .problem-row { height: auto; }
    DashboardScreen .problem-symbol { width: 2; height: 1; }
    DashboardScreen .problem-text { width: 1fr; height: auto; }
    DashboardScreen .problem-title { height: 1; color: $foreground; text-wrap: nowrap; text-overflow: ellipsis; }
    DashboardScreen .problem-caption { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    DashboardScreen .problem-row JumpLink { margin-left: 2; }
    DashboardScreen #dashboard-check { height: 1; margin-bottom: 1; }
    DashboardScreen #dashboard-check-label { width: auto; padding-right: 2; color: $text-muted; }
    DashboardScreen #dashboard-attention-mode { width: 1fr; color: $text-muted; padding-left: 2; }
    DashboardScreen #home-sessions-card { margin-bottom: 1; }
    DashboardScreen #home-sessions { height: auto; border: none; }
    DashboardScreen #home-sessions > ListItem { height: 1; padding: 0 1 0 0; border-left: solid transparent; }
    DashboardScreen #home-sessions > ListItem.-highlight { background: $row-highlight; text-style: none; color: $foreground; border-left: solid $selection-edge; }
    DashboardScreen #home-sessions Static { height: 1; }
    DashboardScreen #home-sessions-hint, DashboardScreen #home-empty { height: auto; color: $text-muted; }
    DashboardScreen #dashboard-details > Contents { overflow-y: auto; max-height: 12; }
    DashboardScreen #dashboard-summary, DashboardScreen #brand-info { height: auto; }
    DashboardScreen #brand { height: auto; }
    DashboardScreen #brand-mark { width: 15; }
    DashboardScreen .context-panel .section-card { margin-top: 1; }
    DashboardScreen .context-panel .context-fold { margin-top: 0; }
    DashboardScreen.-narrow #dashboard-overview { height: auto; padding: 0 2; }
    DashboardScreen.-narrow #dashboard-title { height: 1; }
    DashboardScreen.-narrow #dashboard-subtitle { display: none; }
    DashboardScreen.-narrow .problem-caption, DashboardScreen.-narrow #dashboard-check-label, DashboardScreen.-narrow #home-sessions-hint { display: none; }
    DashboardScreen.-narrow #dashboard-attention-card, DashboardScreen.-narrow #dashboard-check, DashboardScreen.-narrow #home-sessions-card { margin-bottom: 0; }
    DashboardScreen.-narrow .context-fold .section-card { margin-top: 0; }
    """)
    # The shell's rules plus this screen's own, which must outrank the app's generic widget rules.
    CSS = SHELL_CSS + brand.shell_css("""
    .shell #dashboard-context { padding: 0 1; }
    .shell.-spacious #dashboard-context { width: 42; }
    .shell.-fold-context #dashboard-context { padding: 0 2; border-top: none; background: $background; scrollbar-gutter: auto; }
    .shell.-fold-context #dashboard-body > .shell-content { min-height: 0; height: auto; }
    .shell #home-sessions, .shell #home-sessions:focus { border: none; }
    .shell #home-sessions > ListItem.-highlight { background: $row-highlight; color: $foreground; text-style: none; border-left: solid $selection-edge; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        collect_dashboard: _CollectDashboard = console_data.collect_dashboard,
        collect_banner: Callable[[Path], brand.BannerInfo] = _default_banner,
        command_runner: CommandRunner = capturing_runner,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        collect_context: Callable[[Path], HomeContext] = collect_context,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._now = now
        self._collect_dashboard = collect_dashboard
        self._collect_banner = collect_banner
        self._collect_context = collect_context
        self._command_runner = command_runner
        self._home_context = HomeContext()
        self._language = "ru"
        self._data: DashboardData | None = None
        self._online = False
        self._banner: brand.BannerInfo | None = None
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
        self._load()
        yield from super().compose()

    def _load(self) -> None:
        banner = self._collect_banner(self.repo)
        self._home_context = self._collect_context(self.repo)
        data = self._collect_dashboard(self.repo)
        self._publish_counts(data)
        self._language = getattr(self.app, "language", "ru")
        self._data, self._online, self._banner = data, False, banner

    def menu_item_id(self, name: str) -> str | None:
        return {"Open project": "open-project", "Update Harness": "update-harness"}.get(
            name
        )

    def compose_strip(self) -> ComposeResult:
        yield ElidedPath(str(self.repo), id="dashboard-path")
        yield Button(
            "Открыть другой проект →",
            id="dashboard-open-project",
            compact=True,
            classes="wide-only",
        )
        yield Button(
            "Проект →",
            id="dashboard-open-project-short",
            compact=True,
            classes="narrow-only",
        )

    def compose_content(self) -> ComposeResult:
        data, banner = self._data, self._banner
        assert data is not None and banner is not None
        with Vertical(id="dashboard-overview"):
            yield Static("Рабочее пространство", id="dashboard-title")
            yield Static(
                "Продолжите работу в выбранной сессии или откройте другой проект.",
                id="dashboard-subtitle",
            )
            with SectionCard(
                title="Требует внимания",
                note=str(len(data.problems)),
                id="dashboard-attention-card",
                classes="-alert" if data.problems else "",
            ):
                status = Static(
                    self._attention_status(data),
                    id="dashboard-attention",
                    classes="no-i18n",
                )
                status.display = not data.problems
                yield status
                yield Vertical(*self._problem_rows(data), id="dashboard-problems")
            with Horizontal(id="dashboard-check"):
                yield Static("ПРОВЕРКА", id="dashboard-check-label")
                with Horizontal(classes="segment"):
                    yield Button(
                        i18n.ui(self._language, "offline"),
                        id="dashboard-offline",
                        compact=True,
                        classes="-on",
                    )
                    yield Static("│", classes="segment-sep no-i18n")
                    yield Button(
                        i18n.ui(self._language, "online"),
                        id="dashboard-online",
                        compact=True,
                    )
                yield Static(
                    self._attention_mode(data, online=False, language=self._language),
                    id="dashboard-attention-mode",
                    classes="no-i18n",
                )
            stages = self._home_sessions()
            with SectionCard(
                title="Рабочие сессии", note=str(len(stages)), id="home-sessions-card"
            ):
                yield ListView(
                    *(
                        ListItem(
                            Static(self._session_row(stage), classes="no-i18n"),
                            name=stage.dispatch_id,
                        )
                        for stage in stages
                    ),
                    id="home-sessions",
                )
                if stages:
                    yield Static(
                        "↑↓ выбор · Enter — открыть наблюдение. Выбор сессии не запускает новый процесс.",
                        id="home-sessions-hint",
                    )
                else:
                    yield Static(
                        "Активных сессий нет. Откройте «Оркестрация», чтобы выбрать задачу.",
                        id="home-empty",
                    )
            with Fold(
                title="Подробный технический отчёт",
                summary=self._checks_summary(data),
                collapsed=True,
                id="dashboard-details",
            ):
                with Horizontal(id="brand"):
                    yield Static(_render_mark(), id="brand-mark")
                    yield Static(
                        _render_banner(banner, self._language),
                        id="brand-info",
                        classes="no-i18n",
                    )
                yield Static(
                    _render_summary(data, language=self._language),
                    id="dashboard-summary",
                    classes="no-i18n",
                )

    def compose_context(self) -> ComposeResult:
        for card_id, title, rows in self._facts():
            yield SectionCard(
                Static(
                    fact_table(rows, self._language),
                    id=f"{card_id}-facts",
                    classes="no-i18n",
                ),
                title=title,
                id=card_id,
            )

    def _facts(self) -> list[tuple[str, str, list[tuple[str, Text]]]]:
        data, banner = self._data, self._banner
        assert data is not None and banner is not None
        return home_facts(
            data, banner, self._home_context, self.repo.name, self._language
        )

    def _session_row(self, stage: reports.StageEntry) -> Table:
        return session_row(
            ticket=stage.ticket,
            role=stage.role,
            dispatch_id=stage.dispatch_id,
            state=stage.state,
            language=self._language,
            elapsed=elapsed_text(
                reports.timestamp(stage.at), self._now(), self._language
            ),
        )

    def _home_sessions(self) -> list[reports.StageEntry]:
        """Every unfinished session, the most recently recorded first."""
        view = reports.load_ledger_view(self.repo)
        return sorted(
            (stage for stage in view.stages if not stage.finished),
            key=lambda stage: stage.at,
            reverse=True,
        )

    def _checks_summary(self, data: DashboardData) -> str:
        total = data.ok + data.warn + data.fail + data.skipped
        return i18n.checks_count(self._language, total, data.skipped)

    def _attention_status(self, data: DashboardData) -> str:
        """The line above the problems: empty while there are problems, else that there are none."""
        if data.problems:
            return ""
        return (
            f"[{_color('ok')}]{brand.STATUS_ICONS['ok']} "
            f"{i18n.tr_text(self._language, 'Проблем не найдено')}[/]"
        )

    def _problem_rows(self, data: DashboardData) -> list[Widget]:
        """Every failed and warning check: its symbol, readable title, what and where, and a jump
        to the section that deals with it. Only the symbol carries the status colour."""
        rows: list[Widget] = []
        for status, check_id, message in data.problems:
            route = check_route(check_id)
            rows.append(
                Horizontal(
                    Static(
                        Text(brand.STATUS_ICONS.get(status, "·"), style=_color(status)),
                        classes="problem-symbol no-i18n",
                    ),
                    Vertical(
                        Static(
                            i18n.tr(self._language, route.title),
                            classes="problem-title no-i18n",
                            markup=False,
                        ),
                        Static(
                            f"{check_id} · {message}",
                            classes="problem-caption no-i18n",
                            markup=False,
                        ),
                        classes="problem-text",
                    ),
                    JumpLink(route.section),
                    classes="problem-row",
                )
            )
        return rows

    def _render_attention(self, data: DashboardData) -> None:
        card = self.query_one("#dashboard-attention-card", SectionCard)
        card.set_class(bool(data.problems), "-alert")
        card.set_note(str(len(data.problems)))
        status = self.query_one("#dashboard-attention", Static)
        status.update(self._attention_status(data))
        status.display = not data.problems
        problems = self.query_one("#dashboard-problems", Vertical)
        problems.remove_children()
        problems.mount_all(self._problem_rows(data))

    def sync_language(self) -> None:
        """Re-label the section menu after the console language changed."""
        language = getattr(self.app, "language", "ru")
        if language == self._language:
            return
        self._language = language
        self._render_menu()
        self._repaint()
        stages = {stage.dispatch_id: stage for stage in self._home_sessions()}
        for item in self.query("#home-sessions > ListItem").results(ListItem):
            if item.name in stages:
                item.query_one(Static).update(self._session_row(stages[item.name]))

    def _repaint(self) -> None:
        """Re-render the data-driven texts in the current language from the stored data."""
        data = getattr(self, "_data", None)
        if data is None or not self.is_mounted:
            return
        language = self._language
        self._render_attention(data)
        self.query_one("#dashboard-attention-mode", Static).update(
            self._attention_mode(data, online=self._online, language=language)
        )
        self.query_one("#dashboard-offline", Button).set_class(not self._online, "-on")
        self.query_one("#dashboard-online", Button).set_class(self._online, "-on")
        self.query_one("#dashboard-details", Fold).set_summary(
            self._checks_summary(data)
        )
        for card_id, _title, rows in self._facts():
            self.query_one(f"#{card_id}-facts", Static).update(
                fact_table(rows, language)
            )
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
        ConsoleShell.sync_language(self)
        for button_id, key in (
            ("dashboard-offline", "offline"),
            ("dashboard-online", "online"),
        ):
            button = self.query_one(f"#{button_id}", Button)
            button.label = i18n.ui(self._language, key)
            button.refresh(layout=True)

    @staticmethod
    def _attention_mode(
        data: DashboardData, *, online: bool, language: str = "ru"
    ) -> str:
        return i18n.tr_text(language, f"{_mode(online)} · пропущено {data.skipped}")

    def _publish_counts(self, data: DashboardData) -> None:
        """Share the counters with the top bar and the menu badges with every shell screen."""
        setattr(
            self.app, "health_counts", (data.ok, data.warn, data.fail, data.skipped)
        )
        setattr(self.app, "section_badges", section_badges(data))

    def _apply(
        self,
        data: DashboardData,
        *,
        online: bool,
        banner: brand.BannerInfo | None = None,
        context: HomeContext | None = None,
    ) -> None:
        if banner is not None:
            self._banner = banner
        if context is not None:
            self._home_context = context
        self._data, self._online = data, online
        self._publish_counts(data)
        self._repaint()
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dashboard-offline":
            self.action_checks_offline()
        elif event.button.id == "dashboard-online":
            self.action_checks_online()
        elif event.button.id in (
            "dashboard-open-project",
            "dashboard-open-project-short",
        ):
            self.action_open_project()

    async def action_update_harness(self) -> None:
        from .update_harness import UpdateHarnessScreen
        from ..app import HarnessConsoleApp

        if isinstance(self.app, HarnessConsoleApp) and self.app.commands_running:
            self.notify("Дождитесь окончания команды", severity="warning")
            return
        screen = UpdateHarnessScreen(self.repo, command_runner=self._command_runner)
        await self.app.push_screen(screen, lambda _result: self._refresh_dashboard())

    async def open_section(self, index: int) -> None:
        await self.action_open_section(index)

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
        attention.display = True
        summary.update(running)

        def work() -> None:
            try:
                data = self._collect_dashboard(self.repo, online=online)
                banner = self._collect_banner(self.repo)
                context = self._collect_context(self.repo)
            except Exception as exc:
                # An unhandled worker error would exit the whole TUI; show it in the screen instead.
                text = f"проверки не выполнены: {escape(repr(exc))}"

                def show_error() -> None:
                    attention.update(f"[{_color('fail')}]{text}[/]")
                    attention.display = True
                    summary.update(text)

                call_from_worker(self, show_error)
                return
            call_from_worker(
                self,
                lambda: self._apply(
                    data, online=online, banner=banner, context=context
                ),
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
        if event.list_view.id == "home-sessions" and event.item.name:
            from .worker_observation import WorkerObservationScreen

            self.app.push_screen(WorkerObservationScreen(self.repo, event.item.name))

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
