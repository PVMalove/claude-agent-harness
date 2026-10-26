"""Select, inspect and install a target project through the existing CLI."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content, ContentText
from textual.geometry import Size
from textual.message import Message
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from .. import brand, i18n
from ..shell import SHELL_CSS, ConsoleShell, Fold, SectionCard
from ..status import Status
from .. import data
from ...health.model import Report
from ..catalog import process_argv
from ..project_target import (
    ProjectTarget,
    capability_details,
    capability_options,
    capability_summary,
    capability_title,
    inspect_target,
    installation_blockers,
    installation_entry,
)
from ..runner import CommandRunner
from ..translate import retranslate
from .commands import call_from_worker, render_result
from .diagnostics import render_report, styled_log
from .configuration import ConfirmConfiguration
from .harness import CommandFormScreen

DEFAULT_CAPABILITY = "pvmalove-suite"
_STAGES = (
    "Выбор проекта",
    "Предварительные проверки",
    "Установка / внедрение",
    "Настройка",
    "Итоговая диагностика",
)
_TARGET_HINT = "Укажите путь к проекту и нажмите «Проверить путь»."
_BUSY_NOTE = "Дождитесь окончания операции"
_PLAN_HINT = "План появится после проверки пути."
_OUTPUT_HINT = "Результат проверок и установки появится здесь."
_ACTION_WORDS = {
    "update": "обновление",
    "init": "новая установка",
    "adopt": "внедрение",
}
_AFTER_INSPECT = (
    "activate-project",
    "install-project",
    "setup-offline",
    "setup-online",
    "setup-fix",
    "setup-config",
)
# A button's label on a wide terminal and on a narrow one (the narrow mockup shortens three).
_LABELS = {
    "browse-project": ("Обзор папок", "Обзор"),
    "inspect-project": ("Проверить путь", "Проверить"),
    "activate-project": ("Открыть проект →", "Открыть →"),
}
_CLOSE_BROWSER = ("Закрыть обзор", "Закрыть")
_KEY_LABEL_WIDTH = 11


class ChecksSummary(NamedTuple):
    """What a health report says in numbers: checked, of all, needing a fix, not checked."""

    checked: int
    total: int
    failing: int
    skipped: int


def summarize_report(report: Report) -> ChecksSummary:
    """A skipped check is not a success: it is counted apart from the checked ones."""
    counts = report.summary()
    total = len(report.checks)
    return ChecksSummary(
        total - counts["skipped"],
        total,
        counts["warn"] + counts["fail"],
        counts["skipped"],
    )


class _DirectoryOnlyTree(DirectoryTree):
    """Folders only. A click or Space opens and closes a folder, Right opens it, Enter chooses it:
    only Enter puts the folder into the path."""

    BINDINGS = [
        Binding("right", "expand", "Раскрыть", show=False),
        Binding("enter", "choose", "Подставить путь", show=False),
    ]
    ICON_NODE = "▸ "
    ICON_NODE_EXPANDED = "▾ "

    class Chosen(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(self, path: Path, *, id: str) -> None:
        super().__init__(path, id=id)
        self.guide_depth = 2

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]

    def action_expand(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_choose(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self.post_message(self.Chosen(node.data.path))


class _OptionButton(RadioButton):
    """A radio button whose label keeps its second line: the name, then its description."""

    def _make_label(self, label: ContentText) -> Content:
        return Content.from_text(label)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._label.plain.split("\n"))


class _Stepper(Static):
    """The five stages of the wizard on one line: done `✔`, current `▶`, to come `○`. A terminal
    too narrow for all of them shows the current stage alone, with the counter."""

    def __init__(self, *, id: str) -> None:
        super().__init__("", id=id, classes="no-i18n", markup=False)
        self.stage = 0

    def set_stage(self, stage: int) -> None:
        self.stage = stage
        self.paint()

    def on_resize(self) -> None:
        self.paint()

    def paint(self) -> None:
        language = getattr(self.app, "language", "ru")
        names = [i18n.tr_text(language, name) for name in _STAGES]
        counter = i18n.tr(language, "Шаг {} из {}").format(self.stage + 1, len(names))
        palette = brand.PALETTE
        full = Text()
        for index, name in enumerate(names):
            if index:
                full.append(" ─ ", style=palette["muted"])
            if index < self.stage:
                full.append("✔ ", style=palette["success"])
                full.append(name, style=palette["foreground"])
            elif index == self.stage:
                full.append("▶ ", style=palette["primary"])
                full.append(name, style="bold " + palette["foreground"])
            else:
                full.append("○ " + name, style=palette["muted"])
        width = self.content_size.width
        shown = full
        if width and cell_len(full.plain) + 2 + cell_len(counter) > width:
            shown = Text()
            shown.append("▶ ", style=palette["primary"])
            shown.append(names[self.stage], style="bold " + palette["foreground"])
        room = max(2, width - cell_len(shown.plain) - cell_len(counter))
        shown.append(" " * room + counter, style=palette["muted"])
        self.update(shown)


class ProjectSetupScreen(ConsoleShell[Path | None]):
    """Open project on the shared shell; it is dismissed with the chosen project or with None."""

    CRUMBS = "Главная › Открыть проект"
    SECTION = "Open project"
    NARROW_FOOTER_KEYS = ("escape", "f1", "f2")
    AUTO_FOCUS = "#project-path"
    # Hidden from the footer (the mockup's footer holds Esc, F1 and F2): the plan and the `git init`
    # hint name this key.
    BINDINGS = [Binding("y", "copy_command", "Копировать команду", show=False)]
    CSS = SHELL_CSS + brand.shell_css("""
    ProjectSetupScreen #setup-scroll { width: 100%; height: 1fr; overflow-x: hidden; padding: 0 2; }
    ProjectSetupScreen #setup-title { height: 1; text-style: bold; }
    ProjectSetupScreen #setup-subtitle { height: auto; color: $text-muted; }
    ProjectSetupScreen #setup-stage { height: auto; border: round $border-subtle; background: $surface; padding: 0 1; }
    ProjectSetupScreen #setup-columns { width: 100%; height: auto; layout: vertical; }
    ProjectSetupScreen.-spacious #setup-columns { layout: horizontal; }
    ProjectSetupScreen #setup-left, ProjectSetupScreen #setup-right { width: 1fr; height: auto; }
    ProjectSetupScreen.-spacious #setup-left { margin-right: 1; }
    ProjectSetupScreen Static { height: auto; }
    ProjectSetupScreen .field-label, ProjectSetupScreen .hint { color: $text-muted; }
    ProjectSetupScreen .actions { height: auto; align-vertical: middle; }
    ProjectSetupScreen .actions Button { margin-right: 1; }
    ProjectSetupScreen Button:disabled { border: dashed $border-subtle; text-opacity: 45%; }
    ProjectSetupScreen #activate-project { color: $primary; border: round $primary; }
    ProjectSetupScreen #install-project { background: $primary; color: $background; border: round $primary; text-style: bold; }
    ProjectSetupScreen #activate-project:disabled, ProjectSetupScreen #install-project:disabled { background: $surface; color: $text-muted; border: dashed $border-subtle; }
    ProjectSetupScreen #project-path { width: 1fr; }
    ProjectSetupScreen #target-status { margin-top: 1; }
    ProjectSetupScreen #browser-panel { height: auto; margin-top: 1; }
    ProjectSetupScreen #directory-browser { height: 12; background: $background; border: round $border-subtle; padding: 0 1; }
    ProjectSetupScreen #directory-browser > .tree--guides, ProjectSetupScreen #directory-browser > .tree--guides-hover, ProjectSetupScreen #directory-browser > .tree--guides-selected { color: $background; }
    ProjectSetupScreen #directory-browser > .tree--cursor { background: $row-highlight; color: $foreground; }
    ProjectSetupScreen #directory-browser:focus > .tree--cursor { background: $selection-background; color: $selection-foreground; text-style: none; }
    ProjectSetupScreen #capability-frame > CollapsibleTitle, ProjectSetupScreen #output-frame > CollapsibleTitle { display: none; }
    ProjectSetupScreen #capability-frame > Contents, ProjectSetupScreen #output-frame > Contents { padding: 0; }
    ProjectSetupScreen RadioSet { height: auto; border: none; padding: 0; background: transparent; }
    ProjectSetupScreen RadioButton { padding: 0; background: transparent; }
    ProjectSetupScreen #capability-detail { color: $text-muted; }
    ProjectSetupScreen #plan-command { border: round $border-subtle; background: $background; padding: 0 1; }
    ProjectSetupScreen #plan-hint { color: $text-muted; }
    ProjectSetupScreen #install-note { width: auto; height: 3; padding-left: 1; content-align: left middle; color: $text-muted; }
    ProjectSetupScreen #checks-summary { margin-top: 1; }
    ProjectSetupScreen #setup-output-scroll { height: auto; max-height: 18; }
    ProjectSetupScreen #setup-open { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    ProjectSetupScreen #setup-note { width: auto; padding-left: 2; text-wrap: nowrap; }
    ProjectSetupScreen.-narrow ContextStrip { display: none; }
    ProjectSetupScreen.-narrow #setup-scroll { padding: 0 1; }
    ProjectSetupScreen.-narrow #project-frame, ProjectSetupScreen.-narrow #checks-frame { border: none; padding: 0; background: transparent; }
    ProjectSetupScreen.-narrow #capability-frame > CollapsibleTitle, ProjectSetupScreen.-narrow #output-frame > CollapsibleTitle { display: block; }
    ProjectSetupScreen.-narrow #capability-frame > Contents, ProjectSetupScreen.-narrow #output-frame > Contents { padding: 1 0 0 0; }
    ProjectSetupScreen.-narrow #activate-project, ProjectSetupScreen.-narrow #install-project, ProjectSetupScreen.-narrow Button:disabled { border: none; }
    ProjectSetupScreen.-narrow #directory-browser { height: 8; }
    """)

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        super().__init__()
        self.repo = repo
        self.runner = command_runner
        self.target: ProjectTarget | None = None
        self.busy = False
        self.stage = 0
        self.options = _options_by_default()
        self._capability_value = ""
        # What `#target-status` shows: (kind, text, emphasised part of the first line).
        self._target_status: tuple[str, str, str] = ("not_started", _TARGET_HINT, "")
        # The plan whose blockers are being read; an older plan's result is dropped.
        self._plan_epoch = 0
        # A reset outdates the check or fix that is still running: its result is dropped.
        self._run_epoch = 0
        # None while the blockers of the current plan are being read.
        self._blocked: tuple[str, ...] | None = None
        self._plan_unchecked = False
        # What `#setup-output` shows: ("hint" | "running" | "log", text).
        self._output: tuple[str, str] = ("hint", "")
        self._summary: ChecksSummary | None = None
        self._branch: str | None = None
        self._copyable: str | None = None
        self._browsing = False
        self._browser_root = ""
        self._narrow: bool | None = None

    # -- state ------------------------------------------------------------------------------

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _t(self, text: str) -> str:
        return i18n.tr_text(self._language(), text)

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for identifier in ("project-path", "capability", "browse-project"):
            self.query_one("#" + identifier).disabled = value
        # A press during an operation is ignored, so the actions look it: disabled until it ends.
        self.query_one("#inspect-project", Button).disabled = value
        if value:
            for name in _AFTER_INSPECT:
                self.query_one("#" + name, Button).disabled = True
        elif self.target is not None:
            for name in _AFTER_INSPECT:
                self.query_one("#" + name, Button).disabled = False
            self._show_plan()

    def _reset_target(self) -> None:
        """No target: the previous one, its plan, status and result are not available any more."""
        self.target = None
        self._blocked = None
        self._plan_unchecked = False
        self._plan_epoch += 1
        self._run_epoch += 1
        self._copyable = None
        self._summary = None
        for name in _AFTER_INSPECT:
            self.query_one("#" + name, Button).disabled = True
        self._target_status = ("not_started", _TARGET_HINT, "")
        self._output = ("hint", "")
        self.query_one("#plan-frame", SectionCard).set_note("")
        self._set_stage(0)
        self._paint()

    def _set_stage(self, stage: int) -> None:
        self.stage = stage
        self.query_one("#setup-stage", _Stepper).set_stage(stage)

    def _set_output(self, kind: str, text: str) -> None:
        self._output = (kind, text)
        self._paint_output()

    # -- painting ---------------------------------------------------------------------------

    def _paint(self) -> None:
        """Every text that depends on the state or on the language, from the state."""
        self.query_one("#setup-stage", _Stepper).paint()
        self._paint_strip()
        self._paint_status()
        self._paint_options()
        self._paint_plan()
        self._paint_checks()
        self._paint_output()
        self._paint_buttons()
        self._paint_browser()

    def _paint_strip(self) -> None:
        text = f"{self._t('Сейчас открыт:')} {self.repo.name}"
        if self._branch:
            text += f" · ⎇ {self._branch}"
        self.query_one("#setup-open", Static).update(text)

    def _show_target(self, kind: str, text: str, emphasis: str = "") -> None:
        """The target line as a status: its symbol and colour on the first line, the facts below."""
        self._target_status = (kind, text, emphasis)
        self._paint_status()

    def _paint_status(self) -> None:
        kind, text, emphasis = self._target_status
        # Line by line: a known whole line (a status, a hint) has its own English text.
        first, _, rest = "\n".join(
            self._t(line) for line in text.split("\n")
        ).partition("\n")
        status = Status(kind, first, first)
        shown = Text(status.render(self._language()), style=status.color)
        if emphasis:
            word = self._t(emphasis)
            start = shown.plain.find(word)
            if start >= 0:
                shown.stylize("bold", start, start + len(word))
        if rest:
            shown.append("\n" + rest, style=brand.PALETTE["muted"])
        self.query_one("#target-status", Static).update(shown)

    def _show_running(self, text: str) -> None:
        """The output block while a command runs: a running symbol and the text."""
        self._set_output("running", text)

    def _paint_output(self) -> None:
        kind, text = self._output
        output = self.query_one("#setup-output", Static)
        fold = self.query_one("#output-frame", Fold)
        language = self._language()
        if kind == "running":
            status = Status("running", text, i18n.tr_text("en", text))
            output.update(Text(status.render(language), style=status.color))
            fold.set_summary("")
        elif kind == "log":
            output.update(styled_log(text))
            fold.set_summary(
                i18n.count_noun(
                    language,
                    len(text.splitlines()),
                    ("строка", "строки", "строк"),
                    ("line", "lines"),
                )
            )
        else:
            output.update(self._t(_OUTPUT_HINT))
            fold.set_summary("")

    def _paint_options(self) -> None:
        installed = self.target.capabilities if self.target else ()
        buttons = self.query_one("#capability", RadioSet).query(RadioButton)
        muted, primary = brand.PALETTE["muted"], brand.PALETTE["primary"]
        for (name, _), button in zip(self.options, buttons):
            parts: list[str | tuple[str, str]] = [self._t(capability_title(name))]
            if name in installed:
                parts += ["  ", (self._t("установлено"), primary)]
            parts += ["\n    ", (self._t(capability_summary(name)), muted)]
            button.label = Content.assemble(*parts)
        selected = self._capability()
        self.query_one("#capability-detail", Static).update(
            self._t(capability_details(selected))
        )
        self.query_one("#capability-frame", Fold).set_summary(
            self._t(capability_title(selected))
        )

    def _paint_plan(self) -> None:
        plan = self.query_one("#installation-plan", Static)
        shown = self.target is not None
        for selector in ("#plan-command-label", "#plan-command", "#plan-hint"):
            self.query_one(selector).display = shown
        self.query_one("#install-note").display = shown and not self._narrow
        if self.target is None:
            plan.display = True
            plan.update(self._t(_PLAN_HINT))
            return
        target, capability = self.target, self._capability()
        muted = brand.PALETTE["muted"]
        text = Text()
        if not self._narrow:
            for label, value in (
                ("Проект", Text(target.repo.name)),
                (
                    "Состав",
                    Text(f"{self._t(capability_title(capability))} ({capability})"),
                ),
                ("Конфликты", self._conflicts_value(target)),
            ):
                text.append(self._t(label).ljust(_KEY_LABEL_WIDTH), style=muted)
                text.append_text(value)
                text.append("\n")
        notes: list[str] = []
        if target.conflicts:
            notes.append(
                "Force/replace не включены. При конфликте операция остановится."
            )
        if target.capabilities and target.capabilities != (capability,):
            notes.append(
                "Выбранный состав заменит прежний набор управляемых capabilities: "
                + ", ".join(target.capabilities)
                + "."
            )
        if self._blocked:
            notes.append(
                "Установка заблокирована; проверьте собственные/изменённые пути:\n"
                + "\n".join(self._blocked)
            )
        if self._plan_unchecked:
            notes.append(
                "План не удалось проверить; исправьте существующую установку вручную."
            )
        text.append("\n".join(self._t(note) for note in notes))
        text.rstrip()
        plan.display = bool(text.plain)
        plan.update(text)
        self.query_one("#plan-command-label", Static).update(self._t("Команда"))
        self.query_one("#plan-command", Static).update(
            shlex.join(installation_entry(target, capability).cli_argv(target.repo))
        )
        self.query_one("#plan-hint", Static).update(
            self._t("y — копировать · перезаписывает только управляемые файлы Harness")
        )

    def _conflicts_value(self, target: ProjectTarget) -> Text:
        palette = brand.PALETTE
        if self._blocked:
            return Text(
                self._t("✖ заблокировано: {}").format(len(self._blocked)),
                style=palette["error"],
            )
        if target.conflicts:
            return Text("⚠ " + ", ".join(target.conflicts), style=palette["warning"])
        if self._blocked is None and not self._plan_unchecked:
            return Text(self._t("○ проверяется…"), style=palette["muted"])
        return Text(self._t("✔ нет"), style=palette["success"])

    def _paint_checks(self) -> None:
        summary = self.query_one("#checks-summary", Static)
        summary.display = self._summary is not None
        if self._summary is None:
            return
        checked, total, failing, skipped = self._summary
        palette = brand.PALETTE
        items = [
            (f"✔ {self._t('Проверено {} из {}').format(checked, total)}", "success"),
            (f"⚠ {self._t('Требует исправления {}').format(failing)}", "warning"),
            (f"○ {self._t('Не проверено {}').format(skipped)}", "muted"),
        ]
        # On one line when they fit, one under another otherwise: never cut in the middle of one.
        inline = "  ".join(item for item, _ in items)
        room = self.query_one("#checks-frame").content_size.width
        separator = "  " if not room or cell_len(inline) <= room else "\n"
        text = Text()
        for index, (item, color) in enumerate(items):
            if index:
                text.append(separator)
            text.append(item, style=palette[color])
        summary.update(text)

    def _paint_buttons(self) -> None:
        index = 1 if self._narrow else 0
        for identifier, labels in _LABELS.items():
            label = labels[index]
            if identifier == "browse-project" and self._browsing:
                label = _CLOSE_BROWSER[index]
            button = self.query_one("#" + identifier, Button)
            button.label = label
            retranslate(button, self._language())

    def _paint_browser(self) -> None:
        for label in self.query("#browser-label").results(Static):
            label.update(
                Text.assemble(
                    (
                        i18n.tr(self._language(), "Папки") + " · ",
                        brand.PALETTE["muted"],
                    ),
                    self._browser_root,
                )
            )

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        self._paint()
        self._layout(self.app.size.width < brand.NARROW_WIDTH)

    # -- layout -----------------------------------------------------------------------------

    def _layout(self, narrow: bool) -> None:
        """Wide: a card each for the set and the result. Narrow: they fold, and the buttons are
        compact; the reader's own folding is kept between two narrow renderings."""
        switched = narrow != self._narrow
        self._narrow = narrow
        for fold, title, note in (
            (
                self.query_one("#capability-frame", Fold),
                "Состав Harness",
                "что будет установлено",
            ),
            (self.query_one("#output-frame", Fold), "Результат", "последняя операция"),
        ):
            fold.border_title = (
                ""
                if narrow
                else brand.section_title(
                    i18n.tr(self._language(), title), self._t(note)
                )
            )
            if switched:
                fold.collapsed = narrow
        for button in self.query(Button):
            if button.id != self.IDS.toggle:
                button.compact = narrow
        if switched:
            self._paint_buttons()
            self._paint_plan()

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < brand.NARROW_WIDTH
        if narrow != self._narrow:
            self._layout(narrow)
        self._paint_strip()
        self._paint_checks()

    # -- composition ------------------------------------------------------------------------

    def compose_strip(self) -> ComposeResult:
        yield Static("", id="setup-open", classes="no-i18n", markup=False)
        yield Static("Выбор цели меняет проект для всех экранов", id="setup-note")

    def compose_content(self) -> ComposeResult:
        narrow = self.app.size.width < brand.NARROW_WIDTH
        with VerticalScroll(id="setup-scroll"):
            yield Static("Открыть проект", id="setup-title")
            yield Static(
                "Укажите Git-проект. Проверка пути ничего не меняет; установка Harness "
                "выполняется только после вашего подтверждения.",
                id="setup-subtitle",
                classes="wide-only",
            )
            yield _Stepper(id="setup-stage")
            with Horizontal(id="setup-columns"):
                with Vertical(id="setup-left"):
                    with SectionCard(title="Проект", id="project-frame"):
                        yield Static(
                            "Путь к локальному Git-проекту", classes="field-label"
                        )
                        yield Input(
                            value=str(self.repo),
                            placeholder="Путь к локальному Git-проекту",
                            id="project-path",
                        )
                        with Horizontal(classes="actions"):
                            yield Button("Обзор папок", id="browse-project")
                            yield Button("Проверить путь", id="inspect-project")
                            yield Button(
                                "Открыть проект →", id="activate-project", disabled=True
                            )
                        yield Static(
                            "", id="target-status", classes="no-i18n", markup=False
                        )
                    with Fold(
                        title="Состав Harness", id="capability-frame", collapsed=narrow
                    ):
                        with RadioSet(id="capability"):
                            for name, _ in self.options:
                                yield _OptionButton(
                                    name, value=name == DEFAULT_CAPABILITY
                                )
                        yield Static(
                            "", id="capability-detail", classes="no-i18n", markup=False
                        )
                with Vertical(id="setup-right"):
                    with SectionCard(title="План установки", id="plan-frame"):
                        yield Static(
                            "", id="installation-plan", classes="no-i18n", markup=False
                        )
                        yield Static(
                            "Команда",
                            id="plan-command-label",
                            classes="field-label no-i18n",
                        )
                        yield Static(
                            "", id="plan-command", classes="no-i18n", markup=False
                        )
                        yield Static(
                            "", id="plan-hint", classes="no-i18n", markup=False
                        )
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Установить", id="install-project", disabled=True
                            )
                            yield Static("Потребуется подтверждение", id="install-note")
                    with SectionCard(title="Проверки", id="checks-frame"):
                        with Horizontal(classes="actions"):
                            yield Button("Офлайн", id="setup-offline", disabled=True)
                            yield Button("Онлайн", id="setup-online", disabled=True)
                            yield Button("Исправить", id="setup-fix", disabled=True)
                            yield Button("Настройка", id="setup-config", disabled=True)
                        yield Static(
                            "", id="checks-summary", classes="no-i18n", markup=False
                        )
                        yield Static(
                            "Онлайн — отдельная проверка GitHub/GitLab и прав доступа. "
                            "Проверки песочницы анализируют настройки; фактические доступы "
                            "могут остаться непроверенными.",
                            classes="hint wide-only",
                        )
            with Fold(title="Результат", id="output-frame", collapsed=narrow):
                with VerticalScroll(id="setup-output-scroll"):
                    yield Static("", id="setup-output", classes="no-i18n", markup=False)

    def on_mount(self) -> None:
        self._layout(self.app.size.width < brand.NARROW_WIDTH)
        self._set_stage(0)
        self._paint()
        self.run_worker(self._load_branch, thread=True, group="branch")
        self.call_after_refresh(self._show_top)

    def _show_top(self) -> None:
        """The page opens at its title, not scrolled to the focused field's bottom."""
        self.query_one("#setup-scroll").scroll_home(animate=False)

    def _load_branch(self) -> None:
        branch = brand.current_branch(self.repo)
        call_from_worker(self, lambda: self._set_branch(branch))

    def _set_branch(self, branch: str | None) -> None:
        self._branch = branch
        self._paint_strip()

    # -- the wizard -------------------------------------------------------------------------

    def _capability(self) -> str:
        index = self.query_one("#capability", RadioSet).pressed_index
        return self.options[max(0, index)][0]

    def _select_capability(self, name: str) -> None:
        buttons = list(self.query_one("#capability", RadioSet).query(RadioButton))
        for (option, _), button in zip(self.options, buttons):
            if option == name:
                button.value = True

    def _show_plan(self) -> None:
        """Show the plan at once; the blockers come from a worker (they read files and run git),
        and "Установить" stays off until they are known."""
        if self.target is None:
            return
        target, capability = self.target, self._capability()
        self._blocked = None
        self._plan_unchecked = False
        self._copyable = shlex.join(
            installation_entry(target, capability).cli_argv(target.repo)
        )
        self.query_one("#install-project", Button).disabled = True
        self._paint_options()
        self._paint_plan()
        self._plan_epoch += 1
        epoch = self._plan_epoch

        def work() -> None:
            blocked: tuple[str, ...] = ()
            error: Exception | None = None
            try:
                blocked = installation_blockers(target, capability)
            except (ValueError, OSError) as exc:
                error = exc
            call_from_worker(self, lambda: self._planned(epoch, target, blocked, error))

        # Not exclusive: a thread cannot be cancelled, an outdated result is dropped by its epoch.
        self.run_worker(work, thread=True, group="plan")

    def _planned(
        self,
        epoch: int,
        target: ProjectTarget,
        blocked: tuple[str, ...],
        error: Exception | None,
    ) -> None:
        # The target or the capability changed while the blockers were being read: not this plan.
        if epoch != self._plan_epoch or self.target is not target:
            return
        if isinstance(error, ValueError):
            self._failed(str(error))
            return
        self._blocked = blocked
        self._plan_unchecked = error is not None
        self.query_one("#install-project", Button).disabled = (
            self.busy or bool(blocked) or error is not None
        )
        self._paint_plan()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "capability":
            self._show_plan()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-path":
            self._reset_target()

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        # A click on a folder only highlights and opens it; Enter chooses (`_DirectoryOnlyTree.Chosen`).
        event.stop()

    @on(_DirectoryOnlyTree.Chosen)
    def _folder_chosen(self, event: _DirectoryOnlyTree.Chosen) -> None:
        event.stop()
        field = self.query_one("#project-path", Input)
        field.value = str(event.path)
        self._toggle_browser()
        field.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        key = event.button.id
        if key == "browse-project":
            self._toggle_browser()
        elif key == "inspect-project":
            self._inspect(self.query_one("#project-path", Input).value)
        elif key == "activate-project" and self.target:
            self.dismiss(self.target.repo)
        elif key == "install-project" and self.target:
            self._confirm_install(self.target)
        elif key in ("setup-offline", "setup-online") and self.target:
            self._diagnose(self.target.repo, online=key == "setup-online")
        elif key == "setup-fix" and self.target:
            self._confirm_fixes(self.target.repo)
        elif key == "setup-config" and self.target:
            from .configuration import ConfigurationScreen

            self.app.push_screen(
                ConfigurationScreen(self.target.repo), self._configured
            )

    def action_copy_command(self) -> None:
        if self._copyable:
            self.app.copy_to_clipboard(self._copyable)
            self.notify(self._t("Команда скопирована"))

    def _toggle_browser(self) -> None:
        # Mounted on demand: a directory tree starts reading the file system immediately.
        existing = self.query("#browser-panel")
        self._browsing = not existing
        self._paint_buttons()
        if existing:
            existing.remove()
            # An auto-height frame keeps its old height unless its layout is recomputed.
            self.call_after_refresh(self._relayout)
            return
        start = Path(self.query_one("#project-path", Input).value).expanduser()
        root = start if start.is_dir() else Path.home()
        self._browser_root = str(root)
        tree = _DirectoryOnlyTree(root, id="directory-browser")
        panel = Vertical(
            Static("", id="browser-label", classes="no-i18n", markup=False),
            tree,
            Static(
                "↑↓ выбор · → раскрыть · Enter — подставить путь · Esc — закрыть",
                classes="hint",
            ),
            id="browser-panel",
        )
        self.query_one("#project-frame").mount(
            panel, after=self.query_one("#target-status")
        )
        self.call_after_refresh(self._opened_browser, tree)

    def _opened_browser(self, tree: DirectoryTree) -> None:
        self._paint_browser()
        tree.focus()

    def _relayout(self) -> None:
        for identifier in (
            "project-frame",
            "setup-left",
            "setup-columns",
            "setup-scroll",
        ):
            self.query_one("#" + identifier).refresh(layout=True)

    def _confirm_install(self, target: ProjectTarget) -> None:
        # The same confirmation form as the Harness section: command line and reason.
        capability = self._capability()
        entry = installation_entry(target, capability)

        def confirmed(values: dict[str, str] | None) -> None:
            if values is not None:
                self._install(target, capability)

        self.app.push_screen(CommandFormScreen(entry, target.repo), confirmed)

    def _diagnose(self, repo: Path, *, online: bool) -> None:
        self._run(lambda: data.collect_diagnostics(repo, online=online))

    def _confirm_fixes(self, repo: Path) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes:
                # Local fixes only; the report is checked again right after applying them.
                self._run(lambda: data.apply_local_fixes(repo, online=False))

        self.app.push_screen(
            ConfirmConfiguration(
                "Применить локальные исправления health?\nЦель: "
                + str(repo)
                + "\nСетевые действия и внешний трекер не затрагиваются; после исправлений проверка повторится."
            ),
            confirmed,
        )

    def _configured(self, _: None) -> None:
        # Final diagnostics of the configured project, local checks only.
        if self.target is not None:
            self._set_stage(4)
            self._diagnose(self.target.repo, online=False)

    def _inspect(self, value: str) -> None:
        self._set_busy(True)
        self._show_target("running", "Проверяется путь…")

        def work() -> None:
            try:
                target = inspect_target(value)
            except ValueError as exc:
                message = str(exc)
                call_from_worker(self, lambda: self._failed(message))
            else:
                call_from_worker(self, lambda: self._inspected(target))

        self.run_worker(work, thread=True, group="setup", exclusive=True)

    def _install(self, target: ProjectTarget, capability: str) -> None:
        self._set_busy(True)
        self._set_stage(2)
        self._show_running("Устанавливается Harness…")
        entry = installation_entry(target, capability)
        argv = entry.cli_argv(target.repo)
        self._capability_value = capability

        def work() -> None:
            try:
                if installation_blockers(target, capability):
                    raise ValueError("План изменился; заново проверьте пути проекта")
                result = self.runner(process_argv(argv), cwd=target.repo)
                text, success = (
                    render_result(shlex.join(argv), result),
                    result.returncode == 0,
                )
            except ValueError as exc:
                # The plan or the lock changed: an error with a next step, as after a path check.
                message = str(exc)
                call_from_worker(self, lambda: self._failed(message))
                return
            except OSError as exc:
                text, success = str(exc), False
            call_from_worker(self, lambda: self._installed(target, text, success))

        # Its own group: the check that follows the installation must not cancel this worker.
        self.run_worker(work, thread=True, group="setup-install", exclusive=True)

    def _installed(self, target: ProjectTarget, text: str, success: bool) -> None:
        self._finished(text, True)
        if success:
            self._set_stage(3)
            self.notify(
                "Harness установлен. Откройте настройки, затем выполните итоговую диагностику."
            )
        else:
            self._set_stage(1)

    def _failed(self, message: str) -> None:
        self._reset_target()
        self._set_busy(False)
        needs_git = "git init" in message
        self._show_target("error", message, "git init" if needs_git else "")
        if needs_git:
            self._copyable = "git init"

    def _inspected(self, target: ProjectTarget) -> None:
        # A changed path invalidates a result returned by an earlier inspection.
        current = (
            Path(self.query_one("#project-path", Input).value).expanduser().resolve()
        )
        if not current.is_relative_to(target.repo):
            self._failed("Путь изменился; проверьте его ещё раз")
            return
        self._set_busy(False)
        first = self.target is None or self.target.repo != target.repo
        self.target = target
        kind, text, emphasis = _target_summary(target)
        self._show_target(kind, text, emphasis)
        self.query_one("#plan-frame", SectionCard).set_note(
            _ACTION_WORDS[target.action]
        )
        for name in _AFTER_INSPECT:
            self.query_one("#" + name, Button).disabled = False
        if first:
            installed = [
                name for name, _ in self.options if name in target.capabilities
            ]
            # Keep the installed set by default so one click cannot silently downgrade it.
            self._select_capability(installed[-1] if installed else DEFAULT_CAPABILITY)
            self._set_stage(1)
            # Deferred: this callback runs inside the inspection worker's thread call.
            self.call_later(self._diagnose, target.repo, online=False)
        self._show_plan()

    def _run(self, run: Callable[[], Report | str]) -> None:
        self._set_busy(True)
        self._show_running("Выполняется…")
        epoch = self._run_epoch

        def work() -> None:
            result: Report | str
            try:
                result = run()
            except (Exception, SystemExit):
                # `SystemExit`: the CLI helpers the checks use exit the process on a broken file.
                result = "Операция не выполнена; проверьте окружение и доступы"

            def done() -> None:
                # The target was reset meanwhile: the result is not of the screen's target.
                if epoch == self._run_epoch:
                    self._finished(result, False)

            call_from_worker(self, done)

        self.run_worker(work, thread=True, group="setup-run", exclusive=True)

    def _finished(self, result: Report | str, refresh: bool) -> None:
        if isinstance(result, Report):
            self._summary = summarize_report(result)
            self._paint_checks()
            result = render_report(result)
        self._set_output("log", result)
        if refresh and self.target is not None:
            # Checked again in the inspection worker (it runs git), the screen stays busy until then.
            self._inspect(str(self.target.repo))
        else:
            self._set_busy(False)

    def _wait_for_operation(self) -> bool:
        """True (after telling the user) while an operation runs: the screen is not left meanwhile."""
        if self.busy:
            language = getattr(self.app, "language", "ru")
            self.notify(i18n.tr(language, _BUSY_NOTE), severity="warning")
        return self.busy

    async def open_section(self, index: int) -> None:
        if not self._wait_for_operation():
            await super().open_section(index)

    async def action_back(self) -> None:
        # Not `ConsoleShell.action_back` (`pop_screen`): it would drop the push callback, which
        # receives None when the screen is closed without a choice.
        if self.query("#browser-panel"):
            # Esc closes the folder browser first; leaving the screen is the next press.
            self._toggle_browser()
            self.query_one("#browse-project", Button).focus()
        elif not self._wait_for_operation():
            self.dismiss(None)

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)


def _options_by_default() -> tuple[tuple[str, str], ...]:
    """The catalog's capabilities, the default first as in the mockup."""
    options = capability_options()
    first = [option for option in options if option[0] == DEFAULT_CAPABILITY]
    rest = [option for option in options if option[0] != DEFAULT_CAPABILITY]
    return tuple(first + rest)


def _target_summary(target: ProjectTarget) -> tuple[str, str, str]:
    """The status of a checked project: its kind, the text (facts below the first line) and the
    part of the first line to emphasise."""
    word = _ACTION_WORDS[target.action]
    first = f"Git-проект найден · действие: {word}"
    if target.action == "update":
        facts = "harness.lock есть · установлено: " + (
            ", ".join(target.capabilities) or "нет"
        )
    elif target.action == "adopt":
        facts = (
            "Найдено: "
            + ", ".join(target.found)
            + " — конфликты разберите до установки."
        )
    else:
        facts = "Файлов Harness в проекте нет."
    kind = "warning" if target.action == "adopt" else "accepted"
    return kind, f"{first}\n{facts}", word
