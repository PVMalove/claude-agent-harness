"""Раздел Harness: команды харнесса и обслуживания леджера из `harness.console.catalog.HARNESS_COMMANDS`,
доступные для запуска в текущем репозитории, каждая с отображением эквивалента CLI. Обратимые команды
без параметров запускаются немедленно; любые другие команды открывают модальный экран `CommandFormScreen`,
запрашивающий параметры, поясняющий причины подтверждения для необратимых действий и требующий ввода
контрольного слова (например, RESET). Отмена формы ничего не выполняет.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Mapping, Sequence
from collections import Counter

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Collapsible, Input, ListItem, ListView, Static

from .. import brand, i18n
from .. import data as console_data
from .. import reports as console_reports
from .. import stats as console_stats
from ..catalog import (
    HARNESS_COMMANDS,
    CONFIRMATION_REASONS,
    REVERSIBILITY_LABELS,
    Reversibility,
    CatalogEntry,
    process_argv,
)
from harness.cleanup import plan_cleanup, plan_digest, worktree_facts
from ..maintenance import (
    MaintenanceView,
    load_maintenance,
    summarize,
    summary_text,
    reason_text,
    format_size,
    path_size,
)
from ..runner import CommandRunner, HarnessRun, capturing_runner
from ..shell import SHELL_CSS, ConsoleShell, Fold, SectionCard
from .commands import (
    CommandForm,
    CommandMenuScreen,
    call_from_worker,
    short_repo,
    harness_runs,
    render_result,
)

_OTHER_GROUP = "Прочее"
_GROUPS = {
    **dict.fromkeys(
        (
            "init",
            "update",
            "update-force-managed",
            "update-force-seed",
            "adopt",
        ),
        "Установка и обновление",
    ),
    **dict.fromkeys(
        ("diff", "health", "health-fix", "health-online-fix", "verify"),
        "Проверки",
    ),
    **dict.fromkeys(("registry", "lock-project-skills", "list"), "Скиллы"),
    **dict.fromkeys(("repo-map", "parser-bundle"), "Repo Map"),
    **dict.fromkeys(
        (
            "cleanup",
            "cleanup-hard",
            "cleanup-apply",
            "cleanup-hard-apply",
            "uninstall",
            "uninstall-apply",
            "worktree-remove",
        ),
        "Очистка и удаление",
    ),
    **dict.fromkeys(("ledger-migrate", "ledger-clean", "ledger-reset"), "Ledger"),
}
_GROUP_ORDER = list(dict.fromkeys(_GROUPS.values())) + [_OTHER_GROUP]


def _group_rank(key: str) -> int:
    return _GROUP_ORDER.index(_GROUPS.get(key, _OTHER_GROUP))


_HIDDEN_COMMANDS = {
    "init",
    "update",
    "update-force-managed",
    "update-force-seed",
    "adopt",
}


class HarnessCommandList(ListView):
    """Mouse selection is read-only; Enter is a separate, explicit activation."""

    BINDINGS = [
        Binding("enter", "select_cursor", "Запустить"),
        Binding("up", "cursor_up", "Выбор", key_display="↑↓"),
        Binding("down", "cursor_down", show=False),
    ]

    class Activate(Message):
        def __init__(self, item: ListItem) -> None:
            super().__init__()
            self.item = item

    def action_select_cursor(self) -> None:
        if self.highlighted_child is not None:
            self.post_message(self.Activate(self.highlighted_child))


class CommandFormScreen(CommandForm):
    """Модальная форма параметров для команд раздела Harness."""

    BINDINGS = [Binding("f2", "app.toggle_language", "RU/EN", show=False)]

    def __init__(
        self,
        entry: CatalogEntry,
        repo: Path,
        *,
        values: Mapping[str, str] | None = None,
    ) -> None:
        """Инициализирует форму параметров команды каталога для репозитория."""
        super().__init__()
        self.entry = entry
        self.repo = repo
        self._values = dict(values or {})
        self._facts_required = entry.key in (
            "cleanup-apply",
            "cleanup-hard-apply",
            "worktree-remove",
        )
        self._facts_ready = not self._facts_required
        self._facts_error = ""
        self._facts_epoch = 0
        self._approved_digest = ""
        self._facts_text: Callable[[str], str] = lambda language: ""

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов формы ввода параметров команды."""
        entry = self.entry
        with Vertical(id="command-form"):
            with VerticalScroll(id="command-form-body"):
                yield from self.form_header(
                    entry.title,
                    short_repo(entry.cli_line(self.repo), self.repo).replace(
                        "$REPO", "…"
                    ),
                    entry.reversibility,
                    warning_cli=True,
                )
                if self._facts_required:
                    yield Static(
                        "…",
                        id="maintenance-confirmation",
                        classes="no-i18n",
                        markup=False,
                    )
                for name in entry.inputs:
                    yield Static(name, id=f"label-{name}", markup=False)
                    yield Input(
                        value=self._values.get(name, ""),
                        placeholder=name,
                        id=f"input-{name}",
                    )
                if entry.typed_confirmation is not None:
                    yield Static(
                        f"Введите {entry.typed_confirmation}",
                        id="label-confirmation",
                        markup=False,
                    )
                    yield Input(
                        placeholder=f"Введите {entry.typed_confirmation}",
                        id="typed-confirmation",
                    )
            # Outside the scrolling body: Run and the error stay visible however long the form is.
            yield from self.form_buttons(entry.reversibility)

    CSS = """
    CommandFormScreen #command-form { max-height: 95%; }
    CommandFormScreen #command-form-body { max-height: 65vh; }
    CommandFormScreen #form-cli { color: $warning; text-style: none; }
    CommandFormScreen #form-error { height: auto; min-height: 1; }
    """

    def on_mount(self) -> None:
        self._validate()
        if self._facts_required:
            self._refresh_facts()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "input-worktree" and self.is_mounted:
            self._refresh_facts()
        self._validate()

    def _refresh_facts(self) -> None:
        self._facts_epoch += 1
        epoch = self._facts_epoch
        self._facts_ready = False
        self._facts_error = ""
        value = (
            self.query_one("#input-worktree", Input).value.strip()
            if self.entry.key == "worktree-remove"
            else ""
        )
        self._validate()

        def load() -> None:
            digest, error = "", ""
            render: Callable[[str], str]
            try:
                if self.entry.key == "worktree-remove":
                    path = Path(value).expanduser()
                    path = (path if path.is_absolute() else self.repo / path).resolve()
                    fact = next(
                        (
                            item
                            for item in worktree_facts(self.repo)
                            if item.path == path
                        ),
                        None,
                    )
                    error = fact.reason if fact else "worktree is not registered"
                    try:
                        size_label = format_size(path_size(path).bytes) if fact else "?"
                    except OSError:
                        size_label = "?"

                    def render(language: str) -> str:
                        dirty = (
                            str(fact.dirty) if fact and fact.dirty is not None else "?"
                        )
                        return (
                            f"{path}\n{reason_text(error, language)} · dirty: {dirty} · {size_label}\n"
                            + (
                                "Ветка и PR останутся."
                                if language == "ru"
                                else "The branch and PR are kept."
                            )
                        )
                else:
                    summary = summarize(
                        plan_cleanup(
                            self.repo,
                            "hard"
                            if self.entry.key == "cleanup-hard-apply"
                            else "soft",
                        )
                    )
                    digest = plan_digest(summary.plan)

                    def render(language: str) -> str:
                        return summary_text(summary, language)
            except Exception as exc:
                error = str(exc)

                def render(language: str) -> str:
                    return error

            def apply() -> None:
                if epoch != self._facts_epoch or not self.is_mounted:
                    return
                self._facts_ready = True
                self._facts_error = error
                self._approved_digest = digest
                self._facts_text = render
                if self.entry.key == "worktree-remove":
                    self.query_one("#form-cli", Static).update(
                        "$ " + self.entry.cli_line(self.repo, {"worktree": value})
                    )
                self.query_one("#maintenance-confirmation", Static).update(
                    render(getattr(self.app, "language", "ru"))
                )
                self._validate()

            call_from_worker(self, apply)

        self.run_worker(load, thread=True, group="maintenance-confirmation")

    def on_unmount(self) -> None:
        self._facts_epoch += 1
        self.workers.cancel_node(self)

    def _validation(self) -> tuple[dict[str, str], str]:
        values = {
            name: self.query_one(f"#input-{name}", Input).value.strip()
            for name in self.entry.inputs
        }
        missing = [name for name, value in values.items() if not value]
        word = self.entry.typed_confirmation
        if missing:
            return values, f"Заполните: {', '.join(missing)}"
        if (
            word is not None
            and self.query_one("#typed-confirmation", Input).value != word
        ):
            return values, f"Для запуска введите {word}"
        if not self._facts_ready:
            return values, "…"
        if self._facts_error:
            return values, reason_text(
                self._facts_error, getattr(self.app, "language", "ru")
            )
        if self._approved_digest:
            values["__plan_digest"] = self._approved_digest
        return values, ""

    def _validate(self) -> None:
        _, error = self._validation()
        self.query_one("#run", Button).disabled = bool(error)
        self._error(i18n.tr_text(getattr(self.app, "language", "ru"), error))

    def sync_language(self) -> None:
        language = getattr(self.app, "language", "ru")
        self.query_one("#command-form-body > Static", Static).update(
            Text(i18n.tr(language, self.entry.title), style="bold")
        )
        if self.entry.needs_confirmation:
            self.query_one("#confirmation-reason", Static).update(
                Text(
                    i18n.tr_text(
                        language, CONFIRMATION_REASONS[self.entry.reversibility]
                    ),
                    style=brand.PALETTE["warning"],
                )
            )
        if self._facts_required:
            self.query_one("#maintenance-confirmation", Static).update(
                self._facts_text(language)
            )
        self._validate()

    def _submit(self) -> None:
        values, error = self._validation()
        if error:
            self._validate()
        else:
            self.dismiss(values)


class HarnessScreen(ConsoleShell, CommandMenuScreen):
    """Экран раздела Harness на общей оболочке консоли: меню доступных команд харнесса и
    обслуживания леджера."""

    CRUMB = "Harness"
    CANCEL_OWN_WORKERS_ONLY = True
    CRUMBS = "Главная › Harness"
    SECTION = "Harness"
    NARROW_FOOTER_KEYS = ("escape", "enter", "y")

    BINDINGS = [Binding("y", "copy_cli", "Копировать CLI")]
    CSS = SHELL_CSS + brand.shell_css("""
    HarnessScreen #harness-page { height: 1fr; padding: 0 2; }
    HarnessScreen #harness-title { height: 1; text-style: bold; }
    HarnessScreen #harness-subtitle { height: 1; color: $text-muted; }
    HarnessScreen .section-card { border: round $component-border; padding: 0 1; height: auto; margin: 0; }
    HarnessScreen #harness-state, HarnessScreen #harness-usage { height: auto; }
    HarnessScreen #harness-usage { color: $text-muted; }
    HarnessScreen #maintenance-fold { height: 12; margin: 0; padding: 0 1; }
    HarnessScreen #maintenance-fold.-collapsed { height: 1; border: none; }
    HarnessScreen #maintenance-fold > Contents { height: 1fr; padding: 0; }
    HarnessScreen #maintenance-scroll { height: 1fr; }
    HarnessScreen #maintenance-summary { height: auto; }
    HarnessScreen #maintenance-actions, HarnessScreen .worktree-row { height: 1; }
    HarnessScreen #maintenance-worktrees { height: auto; }
    HarnessScreen .worktree-label { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    HarnessScreen #maintenance-fold Button { height: 1; min-height: 1; min-width: 0; border: none; padding: 0 1; }
    HarnessScreen.-narrow #maintenance-fold { height: 7; }
    HarnessScreen.-narrow #maintenance-fold.-collapsed { height: 1; }
    HarnessScreen #command-body { height: 1fr; min-height: 3; }
    HarnessScreen #command-list { width: 56%; height: 1fr; min-height: 2; }
    HarnessScreen #command-filters { height: 1; }
    HarnessScreen #command-filters Button { min-width: 0; padding: 0 1; }
    HarnessScreen #command-filters .-on { background: $selection-background; color: $selection-foreground; }
    HarnessScreen #command-menu { height: 1fr; min-height: 2; margin: 0; padding: 0; border: round $component-border; }
    HarnessScreen #command-menu > ListItem { height: 1; padding: 0; border-left: solid transparent; text-style: none; }
    HarnessScreen #command-menu > ListItem.-highlight { background: $selection-background; border-left: solid $primary; outline: round $selection-outline; text-style: none; }
    HarnessScreen #command-menu Static { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    HarnessScreen #command-menu Horizontal { height: 1; }
    HarnessScreen .command-label { width: 1fr; }
    HarnessScreen .command-risk { width: auto; padding-left: 1; }
    HarnessScreen .group-title { color: $text-muted; text-style: bold; }
    HarnessScreen #detail-fold { width: 1fr; height: 1fr; }
    HarnessScreen #detail-fold > CollapsibleTitle { display: none; }
    HarnessScreen #detail-fold > Contents { height: 1fr; padding: 0; }
    HarnessScreen #command-detail-scroll { height: 1fr; padding: 0 1; }
    HarnessScreen #command-detail { height: auto; }
    HarnessScreen #command-risk { height: auto; padding: 0 1; border: round $warning; background: transparent; color: $warning; }
    HarnessScreen #command-risk.-safe { border: round $success; color: $success; }
    HarnessScreen #command-risk.-deletes { border: round $error; color: $error; }
    HarnessScreen #command-cli { height: auto; border: round $component-border; padding: 0 1; }
    HarnessScreen #copy-hint { height: 1; color: $text-muted; }
    HarnessScreen #command-actions { height: auto; }
    HarnessScreen #command-actions Button { margin-right: 1; }
    HarnessScreen #command-run { background: $primary; color: $background; border: round $primary; }
    HarnessScreen #command-run:hover, HarnessScreen #command-run:focus { border: round $foreground; }
    HarnessScreen #run-status { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    HarnessScreen #run-feedback { height: auto; max-height: 1; color: $warning; text-wrap: nowrap; text-overflow: ellipsis; }
    HarnessScreen #output-fold { height: 10; max-height: 35%; }
    HarnessScreen #output-fold.-collapsed { height: 1; border: none; padding: 0 1; }
    HarnessScreen #output-fold > Contents { height: 1fr; padding: 0; }
    HarnessScreen #command-output-scroll { height: 1fr; }
    HarnessScreen #command-output { height: auto; }
    HarnessScreen.-narrow #harness-page { padding: 0 1; }
    HarnessScreen.-narrow #harness-subtitle { display: none; }
    HarnessScreen.-narrow #command-body { layout: vertical; }
    HarnessScreen.-narrow #command-list { width: 1fr; height: 1fr; }
    HarnessScreen.-narrow #detail-fold { width: 1fr; height: 7; }
    HarnessScreen.-narrow #detail-fold.-collapsed { height: 1; border: none; padding: 0 1; }
    HarnessScreen.-narrow #detail-fold > CollapsibleTitle { display: block; }
    HarnessScreen.-narrow #command-risk { padding: 0; border: none; }
    HarnessScreen.-narrow #command-actions Button { height: 1; border: none; min-height: 1; }
    HarnessScreen.-narrow #output-fold { height: 5; }
    HarnessScreen.-narrow #output-fold.-collapsed { height: 1; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CatalogEntry] = HARNESS_COMMANDS,
        load_drift: Callable[[Path], str] = console_data.drift_state,
        load_view: Callable[
            [Path], console_reports.LedgerView
        ] = console_reports.load_ledger_view,
    ) -> None:
        """Инициализирует экран раздела Harness со списком доступных команд."""
        super().__init__(repo, command_runner=command_runner)
        self._load_view = load_view
        self._load_drift = load_drift
        self._drift = "loading"
        self._drift_epoch = 0
        self._filter = "all"
        self._expanded_group = "Проверки"
        self._selected_key: str | None = None
        self._detail_ready: str | None = None
        self._rebuilding = False
        self._menu_lock = asyncio.Lock()
        self._last_narrow: bool | None = None
        self._state = console_stats.harness_state(repo)
        self._view = load_view(repo)
        self._owned_run: HarnessRun | None = None
        self._rendered_run: tuple[int, str, str, int] | None = None
        self._maintenance: MaintenanceView | None = None
        self._maintenance_error = ""
        self._maintenance_epoch = 0
        self._tree_paths: dict[str, Path] = {}
        self._refresh_after_run = 0
        self._receiving = True
        # A command whose script is absent here (e.g. verify outside the harness repository)
        # would only ever fail with "can't open file", so it is not offered.
        self._entries = {
            entry.key: entry
            for entry in catalog
            if entry.key not in _HIDDEN_COMMANDS and entry.available(repo)
        }
        if self._entries and not any(
            self.menu_group(key) == self._expanded_group for key in self._entries
        ):
            self._expanded_group = self.menu_group(next(iter(self._entries)))

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        bindings = super().active_bindings
        order = (
            "escape",
            "up",
            "down",
            "enter",
            "y",
            "f2",
            self.app.COMMAND_PALETTE_BINDING,
        )
        return {key: bindings[key] for key in order if key in bindings}

    @property
    def narrow(self) -> bool:
        return self.app.size.width < brand.NARROW_WIDTH

    def _t(self, text: str) -> str:
        return i18n.tr_text(self._language, text)

    def compose_strip(self) -> ComposeResult:
        yield Static(f"Проект: {self.repo.name}", id="harness-project", markup=False)
        yield Static(
            "Недоступные в этом проекте команды не показываются",
            id="availability-hint",
            classes="wide-only",
        )

    def compose_content(self) -> ComposeResult:
        self._language = getattr(self.app, "language", "ru")
        with Vertical(id="harness-page"):
            yield Static("Harness", id="harness-title")
            yield Static(
                "Команды харнесса и обслуживание. У каждой команды виден CLI.",
                id="harness-subtitle",
            )
            with SectionCard(title="Состояние"):
                yield Static("", id="harness-state", classes="no-i18n", markup=False)
                yield Static("", id="harness-usage", classes="no-i18n", markup=False)
            with Fold(
                title="Обслуживание", id="maintenance-fold", collapsed=self.narrow
            ):
                with VerticalScroll(id="maintenance-scroll"):
                    yield Static(
                        "…", id="maintenance-summary", classes="no-i18n", markup=False
                    )
                    with Horizontal(id="maintenance-actions"):
                        yield Button("План", id="maintenance-plan", compact=True)
                        yield Button(
                            "Очистить…",
                            id="maintenance-clean",
                            compact=True,
                            disabled=True,
                        )
                        yield Button("↻", id="maintenance-refresh", compact=True)
                    with Vertical(id="maintenance-worktrees"):
                        yield Static("Worktree: …", markup=False)
            with Horizontal(id="command-body"):
                with Vertical(id="command-list"):
                    with Horizontal(id="command-filters"):
                        yield Button(
                            "Все", id="filter-all", compact=True, classes="-on"
                        )
                        yield Button(
                            "✔ обратимые", id="filter-reversible", compact=True
                        )
                        yield Button(
                            "⚠ с подтверждением", id="filter-confirmation", compact=True
                        )
                    children = self._menu_children()
                    first = next((i for i, item in enumerate(children) if item.name), 0)
                    yield HarnessCommandList(
                        *children, id="command-menu", initial_index=first
                    )
                with Fold(
                    title="Описание выбранной команды",
                    id="detail-fold",
                    collapsed=self.narrow,
                ):
                    with VerticalScroll(id="command-detail-scroll"):
                        yield Static(
                            "", id="command-detail", classes="no-i18n", markup=False
                        )
                        yield Static(
                            "", id="command-risk", classes="no-i18n", markup=False
                        )
                        yield Static(
                            "", id="command-cli", classes="no-i18n", markup=False
                        )
                        yield Static("y — копировать CLI", id="copy-hint")
                    with Horizontal(id="command-actions"):
                        yield Button("Запустить", id="command-run", variant="primary")
                        yield Button("Копировать CLI", id="command-copy")
            yield Static("", id="run-feedback", markup=False, classes="no-i18n")
            yield Static("", id="run-status", markup=False, classes="no-i18n")
            with Fold(
                title="Вывод", summary="пока пусто", collapsed=True, id="output-fold"
            ):
                with VerticalScroll(id="command-output-scroll"):
                    yield Static(
                        "", id="command-output", markup=False, classes="no-i18n"
                    )
                yield Button("Повторить", id="command-retry", compact=True)

    def _filtered_entries(self) -> list[CatalogEntry]:
        return [
            entry
            for entry in sorted(
                self._entries.values(), key=lambda entry: _group_rank(entry.key)
            )
            if self._filter == "all"
            or (self._filter == "confirmation") == entry.needs_confirmation
        ]

    def menu_items(self) -> Sequence[tuple[str, str]]:
        return [
            (entry.key, i18n.tr(self._language, entry.title))
            for entry in self._filtered_entries()
        ]

    def menu_group(self, key: str) -> str:
        return _GROUPS.get(key, _OTHER_GROUP)

    def _risk_color(self, entry: CatalogEntry) -> str:
        return brand.STATUS_COLORS[
            "ok"
            if not entry.needs_confirmation
            else "fail"
            if entry.reversibility is Reversibility.DELETES_LOCAL_DATA
            else "warn"
        ]

    def _risk_label(self, entry: CatalogEntry) -> str:
        label = self._t(REVERSIBILITY_LABELS[entry.reversibility])
        return label[0] if self.narrow else label

    def _menu_children(self) -> list[ListItem]:
        entries = self._filtered_entries()
        children: list[ListItem] = []
        self._headings = {}
        for group in _GROUP_ORDER:
            members = [
                entry for entry in entries if self.menu_group(entry.key) == group
            ]
            if not members:
                continue
            heading_id = f"group-{_GROUP_ORDER.index(group)}"
            self._headings[heading_id] = group
            opened = group == self._expanded_group
            counts = Counter(
                REVERSIBILITY_LABELS[entry.reversibility][0] for entry in members
            )
            summary = " ".join(f"{symbol} {count}" for symbol, count in counts.items())
            heading = f"{'▾' if opened else '▸'} {i18n.group_title(self._language, group)} · {len(members)}"
            if not opened:
                heading += "   " + summary
            children.append(
                ListItem(
                    Static(heading, classes="group-title no-i18n", markup=False),
                    id=heading_id,
                )
            )
            if opened:
                for entry in members:
                    children.append(
                        ListItem(
                            Horizontal(
                                Static(
                                    i18n.tr(self._language, entry.title),
                                    classes="command-label no-i18n",
                                    markup=False,
                                ),
                                Static(
                                    Text(
                                        self._risk_label(entry),
                                        style=self._risk_color(entry),
                                    ),
                                    classes="command-risk no-i18n",
                                    markup=False,
                                ),
                            ),
                            name=entry.key,
                        )
                    )
        if not entries:
            children.append(
                ListItem(
                    Static(self._t("Нет доступных команд"), markup=False), disabled=True
                )
            )
        return children

    async def _rebuild_menu(self, selected: str | None = None) -> None:
        async with self._menu_lock:
            await self._replace_menu(selected)

    async def _replace_menu(self, selected: str | None) -> None:
        self._rebuilding = True
        menu = self.query_one("#command-menu", ListView)
        await menu.clear()
        await menu.extend(self._menu_children())
        rows = list(menu.children)
        menu.index = next(
            (
                i
                for i, row in enumerate(rows)
                if row.name == selected and selected is not None
            ),
            next((i for i, row in enumerate(rows) if row.name), 0),
        )
        self._rebuilding = False
        item = menu.highlighted_child
        self._select_item(item)
        self._paint_menu_title()

    def select_command(self, key: str) -> None:
        """Reveal a command for direct navigation, keeping admission separate from selection."""
        if key not in {entry.key for entry in self._filtered_entries()}:
            return
        group = self.menu_group(key)
        if group != self._expanded_group:
            self._expanded_group = group
            self.run_worker(self._rebuild_menu(key), group="harness-menu")
        else:
            menu = self.query_one("#command-menu", ListView)
            menu.index = next(
                i for i, row in enumerate(menu.children) if row.name == key
            )

    def _paint_menu_title(self) -> None:
        self.query_one("#command-menu").border_title = brand.section_title(
            i18n.ui(self._language, "commands"), str(len(self._filtered_entries()))
        )

    def _paint_state(self) -> None:
        fields = console_stats.harness_summary_fields(
            self._state, console_data.harness_version()
        )
        text = Text()
        if self.narrow:
            if self._state.installed:
                fields = [
                    field
                    for field in fields
                    if field[0] in ("Версия", "Состав", "Оркестрация")
                ]
            else:
                fields = [("", "○ Харнесс не установлен: нет читаемого lock")]
        for index, (label, value) in enumerate(fields):
            if index:
                text.append(" · ")
            text.append(
                (self._t(label) + " ") if label else "", style=brand.PALETTE["muted"]
            )
            text.append(self._t(value), style="bold")
        drift_labels = {
            "loading": "○ дрейф: проверяется…",
            "clean": "✔ дрейфа нет",
            "drift": "⚠ дрейф есть",
            "missing": "○ дрейф: нет lock",
        }
        drift = drift_labels.get(self._drift, "○ дрейф неизвестен")
        text.append(
            " · " + self._t(drift),
            style=brand.STATUS_COLORS["ok" if self._drift == "clean" else "warn"],
        )
        self.query_one("#harness-state", Static).update(text)
        self.query_one("#harness-state").tooltip = self._t(
            console_stats.harness_state_text(
                self._state, console_data.harness_version()
            )
        )
        usage = console_stats.usage_text(self._view)
        if self.narrow:
            if self._view.unavailable:
                usage = "○ Ledger: " + self._t(self._view.unavailable)
            else:
                stats = console_stats.pipeline_stats(self._view)
                usage = self._t(
                    f"Пайплайн: запусков {stats.batches} · тикетов {stats.tickets} · диспатчей {sum(stats.dispatches_by_role.values())} · отчётов {sum(stats.reports_by_outcome.values())}"
                )
        elif self._view.unavailable:
            usage = "○ " + usage
        badges = Text(self._t(usage))
        if not self._view.unavailable:
            for match in re.finditer(r"\b\d+\b", badges.plain):
                badges.stylize(
                    f"bold on {brand.PALETTE['panel']}", match.start(), match.end()
                )
        self.query_one("#harness-usage", Static).update(badges)

    def _load_maintenance(self) -> None:
        self._maintenance_epoch += 1
        epoch = self._maintenance_epoch
        self._maintenance = None
        self._maintenance_error = ""
        self._paint_maintenance_summary()

        def load() -> None:
            try:
                view, error = load_maintenance(self.repo), ""
            except Exception as exc:
                view, error = None, str(exc)

            def apply() -> None:
                if epoch != self._maintenance_epoch or not self.is_mounted:
                    return
                self._maintenance, self._maintenance_error = view, error
                self._paint_maintenance_summary()
                self.run_worker(
                    self._paint_worktrees(), group="maintenance-paint", exclusive=True
                )

            call_from_worker(self, apply)

        self.run_worker(load, thread=True, group="maintenance-load")

    def _paint_maintenance_summary(self) -> None:
        if not self.query("#maintenance-summary"):
            return
        language = self._language
        view = self._maintenance
        if self._maintenance_error:
            text = (
                "Ошибка плана очистки: " if language == "ru" else "Cleanup plan error: "
            ) + self._maintenance_error
        elif view is None:
            text = (
                "Статистика считается…"
                if language == "ru"
                else "Calculating statistics…"
            )
        else:
            # Full paths and skip reasons stay in the confirmation and tooltip.
            summary = view.soft
            text = "\n".join(
                line
                for line in summary_text(summary, language).splitlines()
                if not line.startswith("○ ")
            )
            extra = max(0, view.hard.total.bytes - summary.total.bytes)
            text += (
                "\nЖёсткий режим: +" if language == "ru" else "\nHard mode: +"
            ) + format_size(extra)
            self.query_one("#maintenance-summary").tooltip = summary_text(
                summary, language
            )
        heading = "Временные файлы" if language == "ru" else "Temporary files"
        self.query_one("#maintenance-summary", Static).update(heading + "\n" + text)
        self.query_one("#maintenance-clean", Button).disabled = (
            view is None or not view.soft.plan["remove"] or harness_runs(self).busy
        )
        self.query_one("#maintenance-plan", Button).disabled = view is None
        self.query_one("#maintenance-fold", Fold).title = (
            "Обслуживание" if language == "ru" else "Maintenance"
        )
        self.query_one("#maintenance-plan", Button).label = (
            "План" if language == "ru" else "Plan"
        )
        self.query_one("#maintenance-clean", Button).label = (
            "Очистить…" if language == "ru" else "Clean…"
        )

    async def _paint_worktrees(self) -> None:
        container = self.query_one("#maintenance-worktrees", Vertical)
        await container.remove_children()
        self._tree_paths.clear()
        view = self._maintenance
        if view is None:
            await container.mount(Static("Worktree: …", markup=False))
        elif view.worktree_error:
            await container.mount(
                Static(reason_text(view.worktree_error, self._language), markup=False)
            )
        elif not view.worktrees:
            await container.mount(Static("Worktree: 0", markup=False))
        else:
            available = sum(fact.removable for fact in view.worktrees)
            await container.mount(
                Static(
                    f"Worktree: {len(view.worktrees)} · "
                    + ("можно удалить" if self._language == "ru" else "can remove")
                    + f": {available}",
                    markup=False,
                )
            )
            for index, fact in enumerate(view.worktrees):
                self._tree_paths[f"tree-remove-{index}"] = fact.path
                size = view.tree_sizes.get(fact.path)
                label = f"{fact.path.name} · dirty: {fact.dirty if fact.dirty is not None else '?'} · {format_size(size.bytes) if size else '?'} · {reason_text(fact.reason, self._language)}"
                row = Horizontal(
                    Static(label, classes="worktree-label", markup=False),
                    Button(
                        "Удалить…" if self._language == "ru" else "Remove…",
                        id=f"tree-remove-{index}",
                        compact=True,
                    ),
                    classes="worktree-row",
                )
                row.tooltip = (
                    str(fact.path) + "\n" + reason_text(fact.reason, self._language)
                )
                await container.mount(row)

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        language = getattr(self.app, "language", "ru")
        changed = language != self._language
        self._language = language
        self._paint_state()
        self._paint_menu_title()
        self._paint_maintenance_summary()
        if changed:
            self.run_worker(self._paint_worktrees(), group="maintenance-paint")
        if changed:
            self.run_worker(
                self._rebuild_menu(self._selected_key),
                group="harness-menu",
            )
        self._paint_detail()
        self._sync_run()

    def menu_detail(self, key: str) -> Text:
        entry = self._entries[key]
        text = Text()
        text.append(i18n.tr(self._language, entry.title), style="bold")
        if entry.inputs:
            text.append("\n" + self._t("Параметры:") + " " + ", ".join(entry.inputs))
        return text

    @on(ListView.Highlighted, "#command-menu")
    def _show_detail(self, event: ListView.Highlighted) -> None:
        if not self._rebuilding:
            self._select_item(event.item)

    def _select_item(self, item: ListItem | None) -> None:
        if item is not None and item.name in self._entries:
            if self._selected_key != item.name:
                self._detail_ready = None
                if self.narrow:
                    self.query_one("#detail-fold", Fold).collapsed = True
            self._selected_key = item.name
            self._paint_detail()
        elif not self._filtered_entries():
            self._selected_key = None
            self._paint_detail()

    def _paint_detail(self) -> None:
        key = self._selected_key
        if key is None:
            for widget_id in ("command-detail", "command-risk", "command-cli"):
                self.query_one(f"#{widget_id}", Static).update("")
            self.query_one("#command-run", Button).disabled = True
            return
        entry = self._entries[key]
        self.query_one("#command-run", Button).disabled = False
        self.query_one("#command-detail", Static).update(self.menu_detail(key))
        risk = REVERSIBILITY_LABELS[entry.reversibility]
        reason = ""
        if entry.needs_confirmation:
            reason = (
                CONFIRMATION_REASONS[entry.reversibility]
                + "\nПеред запуском откроется форма подтверждения."
            )
            if entry.typed_confirmation:
                reason += f"\nПотребуется ввести {entry.typed_confirmation}"
        risk_widget = self.query_one("#command-risk", Static)
        risk_widget.set_class(not entry.needs_confirmation, "-safe")
        risk_widget.set_class(
            entry.reversibility is Reversibility.DELETES_LOCAL_DATA, "-deletes"
        )
        risk_widget.update(
            Text(
                self._t(risk + ("\n" + reason if reason else "")),
                style=self._risk_color(entry),
            )
        )
        self.query_one("#command-cli", Static).update("$ " + entry.cli_line(self.repo))
        self.query_one("#detail-fold", Fold).set_summary(risk)
        self._update_enter_hint()

    def _update_enter_hint(self) -> None:
        menu = self.query_one("#command-menu", HarnessCommandList)
        description = (
            "Описание"
            if self.narrow and self._detail_ready != self._selected_key
            else "Запустить"
        )
        menu._bindings.key_to_bindings["enter"] = [
            Binding("enter", "select_cursor", i18n.tr(self._language, description))
        ]
        menu._bindings.key_to_bindings["up"] = [
            Binding("up", "cursor_up", self._t("Выбор"), key_display="↑↓")
        ]
        self._bindings.key_to_bindings["y"] = [
            Binding(
                "y", "copy_cli", "CLI" if self.narrow else self._t("Копировать CLI")
            )
        ]
        self.refresh_bindings()

    def on_resize(self, event: events.Resize) -> None:
        if (
            self.is_mounted
            and self.query("#detail-fold")
            and self._last_narrow != self.narrow
        ):
            self._last_narrow = self.narrow
            self.query_one("#detail-fold", Fold).collapsed = (
                self.narrow and self._detail_ready is None
            )
            self.run_worker(
                self._rebuild_menu(self._selected_key),
                group="harness-menu",
            )

    @on(Collapsible.Expanded, "#detail-fold")
    def _expanded_detail(self) -> None:
        self._detail_ready = self._selected_key
        self._update_enter_hint()
        self.call_after_refresh(self._scroll_selection)

    def _scroll_selection(self) -> None:
        item = self.query_one("#command-menu", ListView).highlighted_child
        if item is not None:
            item.scroll_visible(animate=False, immediate=True)

    @on(Collapsible.Collapsed, "#detail-fold")
    def _collapsed_detail(self) -> None:
        self._detail_ready = None
        self._update_enter_hint()

    def action_copy_cli(self) -> None:
        if self._selected_key is not None:
            self.app.copy_to_clipboard(self.menu_cli_line(self._selected_key))
            self.notify(self._t("Команда скопирована"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id or ""
        if key.startswith("tree-remove-"):
            event.stop()
            path = self._tree_paths.get(key)
            if path is not None:
                self.open_command(
                    "worktree-remove",
                    {"worktree": str(path)},
                )
        elif key == "maintenance-refresh":
            event.stop()
            self._load_maintenance()
        elif key in ("maintenance-plan", "maintenance-clean"):
            event.stop()
            self.open_command(
                "cleanup" if key == "maintenance-plan" else "cleanup-apply"
            )
        elif key.startswith("filter-"):
            event.stop()
            self._filter = key.removeprefix("filter-")
            for button in self.query("#command-filters Button"):
                button.set_class(button.id == key, "-on")
            entries = self._filtered_entries()
            selected = (
                self._selected_key
                if any(entry.key == self._selected_key for entry in entries)
                else entries[0].key
                if entries
                else None
            )
            if selected:
                self._expanded_group = self.menu_group(selected)
            self.run_worker(self._rebuild_menu(selected), group="harness-menu")
        elif key == "command-run" and self._selected_key:
            event.stop()
            self.open_command(self._selected_key)
        elif key == "command-copy":
            event.stop()
            self.action_copy_cli()
        elif key == "command-retry":
            event.stop()
            run = harness_runs(self).current
            if run is not None:
                self.open_command(run.key)

    def menu_cli_line(self, key: str) -> str:
        return self._entries[key].cli_line(self.repo)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == self.IDS.menu:
            await ConsoleShell.on_list_view_selected(self, event)
        elif event.list_view.id == "command-menu":
            event.stop()
            if event.item.id in self._headings:
                self._expanded_group = self._headings[event.item.id]
                await self._rebuild_menu(self._selected_key)

    async def on_harness_command_list_activate(
        self, event: HarnessCommandList.Activate
    ) -> None:
        event.stop()
        if event.item.id in self._headings:
            self._expanded_group = self._headings[event.item.id]
            await self._rebuild_menu(self._selected_key)
        elif event.item.name is not None:
            if self._refuse_busy():
                return
            if self.narrow and (
                self._detail_ready != event.item.name
                or self.query_one("#detail-fold", Fold).collapsed
            ):
                self._detail_ready = event.item.name
                self.query_one("#detail-fold", Fold).collapsed = False
                self._update_enter_hint()
                return
            self.open_command(event.item.name)

    def open_command(self, key: str, values: Mapping[str, str] | None = None) -> None:
        """Open a catalog command through its usual parameter and confirmation flow."""
        if self._refuse_busy():
            return
        entry = self._entries.get(key)
        if entry is None or not entry.available(self.repo):
            message = self._t("Команда недоступна для выбранного проекта")
            self.query_one("#run-feedback", Static).update(message)
            self.query_one("#run-feedback").display = True
            self.notify(message, severity="warning")
            return
        if not entry.needs_confirmation and not entry.inputs:
            self._run(entry, {})
            return

        def on_form_closed(values: "dict[str, str] | None") -> None:
            """Коллбэк закрытия модальной формы параметров с запуском команды при подтверждении."""
            if values is not None and self.is_mounted and self.app.screen is self:
                self._run(entry, values)

        self.app.push_screen(
            CommandFormScreen(entry, self.repo, values=values), on_form_closed
        )

    def _run(self, entry: CatalogEntry, values: Mapping[str, str]) -> None:
        """Формирует аргументы и запускает процесс выбранной команды каталога."""
        argv = process_argv(
            entry.cli_argv(self.repo, values),
            self.repo,
            dev_environment=entry.dev_environment,
        )
        if "__plan_digest" in values:
            argv.extend(["--plan-digest", values["__plan_digest"]])
        state = harness_runs(self)
        run = state.reserve(entry.key, entry.cli_line(self.repo, values), argv, values)
        if run is None:
            self._refuse_busy()
            return
        self._owned_run = run
        self.query_one("#run-feedback", Static).update("")
        self.query_one("#run-feedback").display = False
        if self.narrow:
            self.query_one("#detail-fold", Fold).collapsed = True
        self._sync_run()
        self.run_worker(
            lambda: state.execute(run, self._command_runner, self.repo),
            thread=True,
            group="harness-command",
        )

    def _refuse_busy(self) -> bool:
        if not harness_runs(self).busy:
            return False
        message = i18n.tr(self._language, "Дождитесь окончания команды Harness")
        self.query_one("#run-feedback", Static).update(message)
        self.query_one("#run-feedback").display = True
        self.notify(message, severity="warning")
        return True

    def on_mount(self) -> None:
        self._paint_state()
        self._paint_menu_title()
        self._load_maintenance()
        self.query_one("#command-retry").display = False
        self.query_one("#run-status").display = False
        self.query_one("#run-feedback").display = False
        self.set_interval(0.1, self._sync_run)
        self._sync_run()
        self._drift_epoch += 1
        epoch = self._drift_epoch

        def load() -> None:
            try:
                value = self._load_drift(self.repo)
            except Exception:
                value = "unknown"

            def apply() -> None:
                if epoch == self._drift_epoch:
                    self._drift = value
                    self._paint_state()

            call_from_worker(self, apply)

        self.run_worker(load, thread=True, group="harness-drift")

    def on_screen_resume(self) -> None:
        self._receiving = True
        self.sync_language()
        self._sync_run()

    def on_screen_suspend(self) -> None:
        self._receiving = False
        if self._owned_run is not None:
            harness_runs(self).abandon_pending(self._owned_run)

    def on_unmount(self) -> None:
        self._drift_epoch += 1
        self._maintenance_epoch += 1
        self._receiving = False
        if self._owned_run is not None:
            harness_runs(self).abandon_pending(self._owned_run)
        # Cancel only this screen's workers, never other application workers.
        self.workers.cancel_node(self)

    def _sync_run(self) -> None:
        if not self.is_mounted or not self._receiving or self.app.screen is not self:
            return
        run = harness_runs(self).current
        if run is None:
            return
        signature = (run.token, run.phase, self._language, run.revision)
        if self._rendered_run == signature:
            return
        scroll = self.query_one("#command-output-scroll", VerticalScroll)
        follow = (
            self._rendered_run is None
            or self._rendered_run[0] != run.token
            or scroll.scroll_y >= scroll.max_scroll_y - 1
        )
        self._rendered_run = signature
        if run.phase in ("pending", "running"):
            status = "▶ " + i18n.tr(self._language, "Выполняется…")
            output = f"$ {run.cli_line}\n{run.output}"
        elif run.phase == "cancelled":
            return
        else:
            status = (
                ("✖ " + i18n.tr_text(self._language, run.error))
                if run.error
                else f"{'✔' if run.returncode == 0 else '✖'} exit {run.returncode}"
            )
            output = (
                f"$ {run.cli_line}\n{run.output}{run.error}"
                if run.error
                else render_result(
                    run.cli_line,
                    CompletedProcess(
                        run.argv, run.returncode or 0, run.stdout, run.stderr
                    ),
                    full=True,
                )
            )
        color = brand.STATUS_COLORS[
            "warn"
            if run.phase in ("pending", "running")
            else "ok"
            if run.returncode == 0 and not run.error
            else "fail"
        ]
        self.query_one("#run-status").display = True
        self.query_one("#run-status", Static).update(Text(status, style=color))
        fold = self.query_one("#output-fold", Fold)
        fold.set_summary(status)
        fold.collapsed = False
        self.query_one("#command-retry").display = run.phase == "completed" and (
            bool(run.error) or run.returncode != 0
        )
        self._show(output)
        if follow:
            self.call_after_refresh(self._follow_output, run.token)
        if run.phase == "completed" and run.token != self._refresh_after_run:
            self._refresh_after_run = run.token
            self._load_maintenance()

    def _follow_output(self, token: int) -> None:
        run = harness_runs(self).current
        if (
            self.is_mounted
            and self._receiving
            and self.app.screen is self
            and run is not None
            and run.token == token
        ):
            self.query_one("#command-output-scroll", VerticalScroll).scroll_end(
                animate=False, immediate=True
            )
