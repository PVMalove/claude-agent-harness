"""Guided configuration editing; only explicit confirmation applies a validated draft."""

from __future__ import annotations

import copy
import inspect
import json
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Callable, cast

from rich.json import JSON
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)

from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore, StaleConfiguration
from harness.runtime_adapters import AdapterStatus
from .. import brand, i18n
from ..shell import SHELL_CSS, ConsoleShell, SectionCard, WrappedValue
from ..status import Status
from ..translate import Translated
from ..logtext import colorize_diff
from ..widgets import (
    ActionButton,
    CheckBox,
    ComposedPage,
    ReasonNote,
    RoundRadioButton,
    StatusLine,
)
from ..advisor import PROVIDERS, Advice, AdvisorUnavailable, advise, availability
from ..configuration import (
    ConfigurationDraft,
    RuntimeAdapterPreview,
    preview_runtime_adapters,
    apply_runtime_adapters,
)
from ..configuration_steps import (
    skeleton,
    CHOICE_PAGE_SIZE,
    ConfigurationStep,
    step_for_problem,
    steps_for,
)
from .commands import call_from_worker
from .home_exit import leave_for_section

_PROVIDER_KEYS = tuple(PROVIDERS)
_NO_ADVISOR = "Без ИИ — обычные подсказки"
_UNCHECKED = "доступность ещё не проверена"
_TARGETS = {
    "configure-project": ("project.json",),
    "configure-orchestration": ("orchestration.json",),
    "configure-both": ("project.json", "orchestration.json"),
}


# The three states of a runtime adapter: its symbol and text come from the console's status words.
_ADAPTER_STATUS = {
    "current": Status("accepted", "актуален", "up to date"),
    "outdated": Status("warning", "требует обновления", "needs an update"),
    "missing": Status("not_started", "не установлен", "not installed"),
}
# The start page's buttons: id -> (label, label on a narrow terminal).
_START_LABELS = {
    "configure-project": ("project.json", "project.json"),
    "configure-orchestration": ("orchestration.json", "orchestration.json"),
    "configure-both": ("Оба файла", "Оба"),
    "save-current-version": ("Сохранить версию", "Сохранить версию"),
    "open-versions": ("Версии / восстановить", "Версии"),
    "preview-adapters": ("Просмотр установки и обновления", "Просмотр обновления"),
}


class Phase(StrEnum):
    CHOOSE = "choose"
    EDITING = "editing"
    REVIEW = "review"


def _short(value: object, limit: int = 48) -> str:
    """One-line preview of a JSON value for the section overview."""
    if isinstance(value, dict):
        return (
            "{…} "
            + ", ".join(list(map(str, value))[:4])
            + (", …" if len(value) > 4 else "")
        )
    if isinstance(value, list):
        text = "[" + ", ".join(_short(item, 16) for item in value) + "]"
    else:
        text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ConfirmConfiguration(Translated, ModalScreen[bool]):
    """A confirmation: the first line of `text` is the bold title, the rest is the plan. A disabled
    `Подтвердить` says why (`reason`) beside the buttons."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = brand.shell_css("""
    ConfirmConfiguration { align: center middle; }
    ConfirmConfiguration > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%; padding: 0 1;
        border: round $primary; border-title-color: $primary;
    }
    ConfirmConfiguration #confirm-title { height: auto; text-style: bold; margin-bottom: 1; }
    ConfirmConfiguration #confirm-text { height: auto; max-height: 20; border: round $border-subtle; padding: 0 1; }
    ConfirmConfiguration #confirm-text Static { height: auto; }
    ConfirmConfiguration .actions { height: auto; }
    ConfirmConfiguration .reason-note { height: auto; color: $text-muted; }
    ConfirmConfiguration Button { min-width: 8; width: auto; border: round $component-border; }
    ConfirmConfiguration Button:hover, ConfirmConfiguration Button:focus { border: round $primary; color: $primary; }
    """)

    def __init__(self, text: str, *, enabled: bool = True, reason: str = "") -> None:
        super().__init__()
        self.text = text
        self.enabled = enabled
        self.reason = reason

    def compose(self) -> ComposeResult:
        title, _, plan = self.text.partition("\n")
        with Vertical() as dialog:
            dialog.border_title = brand.frame_title("Подтверждение")
            yield Static(title, id="confirm-title", markup=False)
            if plan:
                with VerticalScroll(id="confirm-text"):
                    yield Static(plan, markup=False)
            with Horizontal(classes="actions"):
                confirm = Button("Подтвердить", id="confirm", disabled=not self.enabled)
                yield confirm
                yield Button("Отмена", id="cancel")
            yield ReasonNote(
                lambda: [(confirm, self.reason)]
                if confirm.disabled and self.reason
                else [],
                id="confirm-reason",
            )

    def sync_language(self) -> None:
        for note in self.query(ReasonNote):
            note.relabel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfigurationScreen(ConsoleShell):
    """The configuration wizard on the shared console frame; the page below the frame is rebuilt
    on every phase change, the frame itself (menu, breadcrumbs, footer) stays."""

    SECTION = "Configuration"
    CRUMBS = "Главная › Настройка конфигурации"
    NARROW_FOOTER_KEYS = ("escape", "enter", "f2")
    AUTO_FOCUS = "#configure-project"
    # Textual's Screen binds Tab with no footer hint; the footer names it here.
    BINDINGS = [Binding("tab", "app.focus_next", "Фокус")]
    CSS = SHELL_CSS + brand.shell_css("""
    ConfigurationScreen #configuration-page { width: 100%; height: 1fr; padding: 0 2; }
    ConfigurationScreen #configuration-scroll { width: 100%; height: 1fr; overflow-x: hidden; }
    ConfigurationScreen #configuration-title { height: 1; text-style: bold; }
    ConfigurationScreen #configuration-subtitle { color: $text-muted; margin-bottom: 1; }
    ConfigurationScreen #configuration-row { width: 100%; height: auto; layout: vertical; }
    ConfigurationScreen.-spacious #configuration-row { layout: horizontal; }
    ConfigurationScreen #configuration-row > SectionCard { width: 1fr; }
    ConfigurationScreen.-spacious #configuration-row > SectionCard:first-child { margin-right: 1; }
    ConfigurationScreen .store-path { color: $text-muted; }
    ConfigurationScreen .reason-note { height: auto; color: $text-muted; }
    ConfigurationScreen #adapter-status { height: auto; }
    ConfigurationScreen.-spacious #adapter-status { layout: grid; grid-size: 2; grid-gutter: 0 1; }
    ConfigurationScreen .adapter-block { height: auto; border: round $border-subtle; padding: 0 1; }
    ConfigurationScreen.-narrow .adapter-block { border: none; padding: 0; }
    ConfigurationScreen .adapter-head { height: auto; }
    ConfigurationScreen .adapter-name { width: 1fr; text-style: bold; }
    ConfigurationScreen .adapter-state { width: auto; }
    ConfigurationScreen .adapter-path, ConfigurationScreen .adapter-problem { color: $text-muted; }
    ConfigurationScreen .configuration-choice { height: auto; }
    ConfigurationScreen .actions { height: auto; }
    ConfigurationScreen Static { height: auto; }
    ConfigurationScreen RadioSet { height: auto; }
    ConfigurationScreen #configuration-mode {
        layout: horizontal; height: 1; border: none; padding: 0; background: transparent;
    }
    ConfigurationScreen #configuration-mode > RadioButton {
        width: auto; height: 1; margin-right: 3; border: none; padding: 0;
        background: transparent;
    }
    ConfigurationScreen #configuration-mode > RadioButton.-selected {
        background: transparent;
    }
    ConfigurationScreen #configuration-mode:focus > RadioButton.-selected {
        color: $primary; background: transparent;
    }
    ConfigurationScreen #configuration-mode:focus > RadioButton.-selected > .toggle--label {
        color: $primary; text-style: underline; background: transparent;
    }
    ConfigurationScreen #field-value { height: 9; min-height: 3; }
    ConfigurationScreen .recommendation-title { color: $text-muted; }
    ConfigurationScreen .recommendation-json { height: auto; max-height: 12; }
    ConfigurationScreen .section-rows { height: auto; max-height: 12; }
    ConfigurationScreen #menu-items { height: auto; max-height: 8; }
    ConfigurationScreen .hint { color: $text-muted; }
    ConfigurationScreen .row-ok { color: $success; }
    ConfigurationScreen .row-warn { color: $warning; }
    ConfigurationScreen .row-muted { color: $text-muted; }
    ConfigurationScreen #config-error { color: $error; }
    """)

    def __init__(
        self, repo: Path, focus: tuple[str, tuple[str, ...]] | None = None
    ) -> None:
        """`focus` is (file, field path): the wizard opens on that file at that field."""
        super().__init__()
        self.repo = repo
        self.focus_target = focus
        self.phase = Phase.CHOOSE
        self.draft: ConfigurationDraft | None = None
        self.steps: list[ConfigurationStep] = []
        self.index = 0
        self.choice_page = 0
        self.text_mode = False
        self.message = ""
        self.message_kind = "error"
        self._narrow = False
        self.busy = False
        self.stale = False
        self.advice: Advice | None = None
        self.advisor_cancel = Event()
        self.advisor_pending = False
        self.check_cancel = Event()
        self.provider_status: dict[str, str] = {}
        self._actions: dict[str, Callable[[], object]] = {
            "next-step": lambda: self._move(1),
            "previous-step": lambda: self._move(-1),
            "review-config": lambda: self._move(0, review=True),
            "back-to-steps": lambda: self._move(0, review=False),
            "choice-next-page": self._next_choice_page,
            "use-recommendation": self._use_recommendation,
            "use-advisor": self._use_advice,
            "apply-config": self._confirm_apply,
            "save-version": self._request_save_version,
            "save-current-version": self._request_save_current,
            "reload-external": self._reload_external,
            "open-versions": self._open_versions,
            "ask-advisor": self._ask_advisor,
            "check-advisor": self._check_advisor,
            "menu-add": self._menu_add,
            "menu-remove": self._menu_remove,
            "menu-open": self._menu_open,
            "preview-adapters": self._preview_adapters,
        }

    def compose_strip(self) -> ComposeResult:
        yield Static(
            "Проект: " + self.repo.name, id="configuration-project", markup=False
        )
        yield Static(
            "Изменения применяются только после просмотра и подтверждения",
            classes="wide-only",
        )

    def compose_content(self) -> ComposeResult:
        yield ComposedPage(self._build_page, id="configuration-page")

    def _build_page(self) -> ComposeResult:
        # The start page hugs its content; the long step and review forms fill the page so
        # their action row stays at the bottom.
        self.set_class(self.phase is Phase.CHOOSE, "-choose")
        with VerticalScroll(id="configuration-scroll"):
            if self.phase is Phase.CHOOSE:
                yield from self._compose_choose()
            elif self.phase is Phase.EDITING:
                yield from self._compose_field()
            else:
                yield from self._compose_review()
        with SectionCard(title="Действия", id="configuration-actions") as actions:
            # The start screen has no actions: this card appears there only with a message.
            actions.display = self.phase is not Phase.CHOOSE or bool(self.message)
            error = Static(self._message_text(), id="config-error", markup=False)
            error.display = bool(self.message)
            yield error
            reasons: list[tuple[Button, str]] = []
            with Horizontal(classes="actions"):
                if self.phase is Phase.EDITING:
                    previous = Button(
                        "Предыдущий шаг", id="previous-step", disabled=self.index == 0
                    )
                    if previous.disabled:
                        reasons.append((previous, "это первый шаг"))
                    yield previous
                    yield Button("Далее", id="next-step")
                    yield Button("Просмотр", id="review-config")
                elif self.phase is Phase.REVIEW:
                    assert self.draft is not None
                    invalid = bool(self.draft.problems())
                    yield Button("К шагам", id="back-to-steps")
                    apply = Button(
                        "Применить", id="apply-config", disabled=invalid or self.stale
                    )
                    save = Button(
                        "Сохранить версию", id="save-version", disabled=invalid
                    )
                    why = []
                    if invalid:
                        why.append("в черновике есть ошибки: исправьте их на шагах")
                    if self.stale:
                        why.append(
                            "файлы изменены снаружи: сначала загрузите внешние изменения"
                        )
                    if why:
                        reasons.append((apply, "; ".join(why)))
                    if invalid:
                        reasons.append(
                            (save, "в черновике есть ошибки: исправьте их на шагах")
                        )
                    yield apply
                    yield save
                    if self.stale:
                        yield Button(
                            "Загрузить внешние изменения", id="reload-external"
                        )
                    yield Button("Версии и откат", id="open-versions")
            yield ReasonNote(lambda: reasons, id="actions-reason")

    async def _rebuild_page(self) -> None:
        """Rebuild the page below the frame: the menu, breadcrumbs and footer stay as they are."""
        page = self.query_one("#configuration-page", ComposedPage)
        await page.recompose()
        self.translate_now()
        self.call_after_refresh(self._focus_page)

    def _focus_page(self) -> None:
        page = self.query_one("#configuration-page", ComposedPage)
        for widget in self.focus_chain:
            if page in widget.ancestors:
                widget.focus()
                return

    def _remember(self, text: str, kind: str = "error") -> None:
        self.message = text
        self.message_kind = kind

    def _message_text(self) -> Text | str:
        """The message as a status: its symbol, its text in the console language, its colour."""
        if not self.message:
            return ""
        status = Status(
            self.message_kind, self.message, i18n.tr_text("en", self.message)
        )
        return Text(status.render(self._language()), style=status.color)

    def _set_message(self, text: str, kind: str = "error") -> None:
        """Show a message in the actions card, which the start screen otherwise hides."""
        self._remember(text, kind)
        error = self.query_one("#config-error", Static)
        error.update(self._message_text())
        error.display = bool(text)
        self.query_one("#configuration-actions").display = (
            self.phase is not Phase.CHOOSE or bool(text)
        )

    def _is_narrow(self) -> bool:
        return self.app.size.width < brand.NARROW_WIDTH

    def _start_button(self, identifier: str) -> ActionButton:
        wide, narrow = _START_LABELS[identifier]
        return ActionButton(narrow if self._is_narrow() else wide, id=identifier)

    def _store_line(self) -> WrappedValue:
        return WrappedValue(
            self._store_text(), id="configuration-store-path", classes="store-path"
        )

    def _store_text(self) -> str:
        prefix = i18n.tr(self._language(), "Хранилище: ")
        return prefix + str(ConfigurationStore(self.repo).root)

    def _compose_choose(self) -> ComposeResult:
        yield Static("Настройка конфигурации", id="configuration-title")
        yield Static(
            "Файлы project.json и orchestration.json, сохранённые версии и адаптеры runtime.",
            id="configuration-subtitle",
            classes="wide-only",
        )
        with Horizontal(id="configuration-row"):
            with SectionCard(
                title="Редактирование конфигурации",
                id="configuration-editing",
                classes="configuration-choice",
            ):
                yield Static("Выберите, какие файлы настроить.", classes="wide-only")
                with Horizontal(classes="actions"):
                    yield self._start_button("configure-project")
                    yield self._start_button("configure-orchestration")
                    yield self._start_button("configure-both")
                yield Static(
                    "Изменения применяются после просмотра и подтверждения.",
                    classes="wide-only",
                )
            with SectionCard(
                title="Сохранённые версии",
                id="configuration-versions",
                classes="configuration-choice",
            ):
                yield Static(
                    "Сохранить текущий набор без редактирования.", classes="wide-only"
                )
                yield Input(
                    placeholder="Описание версии (необязательно)",
                    id="version-description",
                )
                yield self._store_line()
                with Horizontal(classes="actions"):
                    yield self._start_button("save-current-version")
                    yield self._start_button("open-versions")
        yield from self._compose_adapters()

    def _compose_adapters(self) -> ComposeResult:
        with SectionCard(
            title="Адаптеры runtime",
            note="внешний транспорт оркестрации (transport: external)",
            id="configuration-adapters",
            classes="configuration-choice",
        ):
            try:
                preview = preview_runtime_adapters(self.repo)
            except (ValueError, OSError) as exc:
                message = str(exc)
                yield StatusLine(
                    Status("error", message, i18n.tr_text("en", message)),
                    id="adapter-error",
                )
                return
            with Vertical(id="adapter-status"):
                for item in preview.adapters:
                    yield from self._compose_adapter(item)
            yield Static(
                "Установка и обновление требуют просмотра и подтверждения. "
                "Консоль не включает force и не меняет transport.",
                classes="wide-only",
            )
            yield self._start_button("preview-adapters")

    def _compose_adapter(self, item: AdapterStatus) -> ComposeResult:
        with Vertical(classes="adapter-block"):
            with Horizontal(classes="adapter-head"):
                yield Static(
                    Path(item["path"]).stem,
                    classes="adapter-name no-i18n",
                    markup=False,
                )
                yield StatusLine(
                    _ADAPTER_STATUS[item["status"]], classes="adapter-state"
                )
            yield WrappedValue(item["path"], classes="adapter-path wide-only")
            if item["problem"]:
                yield Static(
                    f"{brand.STATUS_ICONS['warn']} {item['problem']}",
                    classes="adapter-problem no-i18n",
                    markup=False,
                )

    def _preview_adapters(self) -> None:
        try:
            preview = preview_runtime_adapters(self.repo)
        except (ValueError, OSError) as exc:
            self._set_message(str(exc))
            return
        self.app.push_screen(
            ConfirmConfiguration(
                "Обновить адаптеры runtime?\n" + preview.text,
                enabled=not preview.blocked,
                reason=preview.reason,
            ),
            lambda confirmed: self._confirmed_adapters(confirmed, preview),
        )

    def _confirmed_adapters(
        self, confirmed: bool | None, preview: RuntimeAdapterPreview
    ) -> None:
        if confirmed:
            self._operation(
                lambda: apply_runtime_adapters(self.repo, preview, confirmed=True),
                reload=True,
            )

    def _compose_field(self) -> ComposeResult:
        assert self.draft is not None
        field = self.steps[self.index]
        groups = list(dict.fromkeys((step.file, step.path[0]) for step in self.steps))
        group = groups.index((field.file, field.path[0])) + 1
        with SectionCard(title="Шаг", id="configuration-step"):
            yield Static(
                f"Группа {group} / {len(groups)}: {field.path[0]}\nШаг {self.index + 1} / {len(self.steps)}: {field.file} → {'.'.join(field.path)}",
                markup=False,
            )
            yield Select(
                [
                    (f"{s.file}: {'.'.join(s.path)}", i)
                    for i, s in enumerate(self.steps)
                ],
                value=self.index,
                allow_blank=False,
                id="step-jump",
            )
        with SectionCard(title="Параметр", id="configuration-field"):
            yield from self._compose_parameter(field)
        with SectionCard(title="ИИ-подсказки", id="configuration-advisor") as advisor:
            advisor.display = not (
                field.is_section
                or field.is_map
                or (field.has_menu and not self.text_mode)
            )
            yield from self._compose_advisor()

    def _compose_parameter(self, field: ConfigurationStep) -> ComposeResult:
        assert self.draft is not None
        yield Static(field.description, markup=False)
        if field.is_section:
            yield from self._compose_section(field)
            return
        if field.has_menu:
            yield from self._compose_mode_switch()
            if not self.text_mode:
                yield from self._compose_menu(field)
                return
        recommendation, why = field.recommendation(self.draft)
        if isinstance(recommendation, (dict, list)) and recommendation:
            if recommendation != field.value(self.draft):
                # A whole object is unreadable on one line; show it as indented JSON.
                yield Static("Рекомендация (★):", classes="recommendation-title")
                with VerticalScroll(classes="recommendation-json"):
                    yield Static(
                        JSON.from_data(recommendation, indent=2, ensure_ascii=False),
                        classes="no-i18n",
                    )
            yield Static("Почему: " + why, markup=False)
        else:
            yield Static(
                "Рекомендация (★): "
                + json.dumps(recommendation, ensure_ascii=False)
                + "\nПочему: "
                + why,
                markup=False,
            )
        yield CheckBox(
            "Использовать параметр (снять — оставить его отсутствующим)",
            value=field.present(self.draft) or field.required,
            disabled=field.required,
            id="include-field",
        )
        value = field.value(self.draft)
        if field.kind == "boolean":
            yield CheckBox(
                "Включено — да; выключено — нет",
                value=value is True,
                id="field-boolean",
            )
        else:
            if field.choices:
                yield from self._compose_choices(field, value, recommendation)
            text = (
                value
                if isinstance(value, str)
                and field.kind == "string"
                and not field.choices
                else json.dumps(value, ensure_ascii=False, indent=2)
            )
            yield Static(
                "Введите значение"
                if field.kind == "string" and not field.choices
                else "Добавьте или удалите записи в JSON; поля каждой записи настраиваются на следующих шагах"
                if field.is_map
                else "Значение в JSON; объект/список можно редактировать целиком"
            )
            yield TextArea(str(text or ""), id="field-value", soft_wrap=True)
        with Horizontal(classes="actions"):
            yield Button("Принять рекомендацию", id="use-recommendation")

    def _compose_section(self, field: ConfigurationStep) -> ComposeResult:
        """A block of fields: what is set so far; the fields follow on the next steps."""
        assert self.draft is not None
        yield CheckBox(
            "Использовать блок (снять — убрать его целиком)",
            value=field.present(self.draft) or field.required,
            disabled=field.required,
            id="include-field",
        )
        yield from self._compose_rows(self._section_rows(field), "Поля блока:")
        yield Static(
            "Поля блока настраиваются на следующих шагах — «Далее».",
            classes="hint",
        )

    def _compose_rows(self, rows: list[tuple[str, str]], title: str) -> ComposeResult:
        yield Static(title, classes="recommendation-title")
        with VerticalScroll(classes="section-rows"):
            for text, style in rows:
                yield Static(text, classes=style, markup=False)

    def _section_rows(self, field: ConfigurationStep) -> list[tuple[str, str]]:
        assert self.draft is not None
        value = field.value(self.draft)
        current = value if isinstance(value, dict) else {}
        required = field.required_names
        rows = []
        for name in field.child_names:
            if name in current:
                rows.append((f"✓ {name} = {_short(current[name])}", "row-ok"))
            elif name in required:
                rows.append((f"! {name} — не задан (обязательный)", "row-warn"))
            else:
                rows.append((f"· {name} — не задан", "row-muted"))
        return rows

    def _compose_mode_switch(self) -> ComposeResult:
        """Two ways to edit: through menu items or as JSON text."""
        with RadioSet(id="configuration-mode", compact=True):
            yield RoundRadioButton(
                "Через меню", value=not self.text_mode, id="mode-menu", compact=True
            )
            yield RoundRadioButton(
                "Текстом (JSON)", value=self.text_mode, id="mode-text", compact=True
            )

    def _compose_menu(self, field: ConfigurationStep) -> ComposeResult:
        assert self.draft is not None
        yield CheckBox(
            "Использовать параметр (снять — оставить его отсутствующим)",
            value=field.present(self.draft) or field.required,
            disabled=field.required,
            id="include-field",
        )
        value = field.value(self.draft)
        if field.is_map:
            entries = value if isinstance(value, dict) else {}
            rows = [
                (str(name), f"{name}  {_short(item)}") for name, item in entries.items()
            ]
            yield Static("Записи:", classes="recommendation-title")
            empty = "Записей пока нет"
        else:
            items = value if isinstance(value, list) else []
            rows = [(str(i), _short(item)) for i, item in enumerate(items)]
            yield Static("Элементы:", classes="recommendation-title")
            empty = "Список пока пуст"
        if rows:
            yield ListView(
                *[
                    ListItem(Label(text, markup=False), name=name)
                    for name, text in rows
                ],
                id="menu-items",
                initial_index=0,
            )
        else:
            yield Static(empty, classes="row-muted")
        reasons: list[tuple[Button, str]] = []
        with Horizontal(classes="actions"):
            if field.is_map:
                open_entry = Button(
                    "Настроить запись", id="menu-open", disabled=not rows
                )
                remove = Button("Удалить запись", id="menu-remove", disabled=not rows)
                if not rows:
                    reasons += [
                        (open_entry, "записей пока нет"),
                        (remove, "записей пока нет"),
                    ]
                yield open_entry
                yield remove
            else:
                remove = Button("Удалить элемент", id="menu-remove", disabled=not rows)
                if not rows:
                    reasons.append((remove, "список пока пуст"))
                yield remove
        yield ReasonNote(lambda: reasons, id="menu-reason")
        yield Input(
            placeholder="Имя новой записи" if field.is_map else "Новый элемент",
            id="menu-new",
        )
        if field.is_map and rows:
            yield Static("Копировать настройки из:", classes="hint")
            yield Select(
                [(name, name) for name, _ in rows],
                value=rows[0][0],
                allow_blank=False,
                id="menu-template",
            )
        with Horizontal(classes="actions"):
            yield Button(
                "Добавить запись" if field.is_map else "Добавить элемент",
                id="menu-add",
            )

    async def _set_mode(self, text: bool) -> None:
        if self._save_field():
            self.text_mode = text
            await self._rebuild_page()

    def _selected_menu_name(self) -> str | None:
        menu = (
            self.query("#menu-items").first(ListView)
            if self.query("#menu-items")
            else None
        )
        item = menu.highlighted_child if menu is not None else None
        return item.name if item is not None else None

    async def _menu_open(self) -> None:
        field = self.steps[self.index]
        name = self._selected_menu_name()
        target = (*field.path, name)
        index = next(
            (
                i
                for i, s in enumerate(self.steps)
                if s.file == field.file and s.path == target
            ),
            None,
        )
        if name is None or index is None or not self._save_field():
            return
        self.index = index
        self._set_page()
        await self._rebuild_page()

    async def _menu_remove(self) -> None:
        assert self.draft is not None
        field = self.steps[self.index]
        name = self._selected_menu_name()
        if name is None:
            return
        if field.is_map:
            self.draft.remove_value(field.file, (*field.path, name))
        else:
            items = field.value(self.draft)
            if isinstance(items, list) and 0 <= int(name) < len(items):
                del items[int(name)]
                self.draft.set_value(field.file, field.path, items)
        self._set_message("")
        self._refresh_steps(field)
        self._set_page()
        await self._rebuild_page()

    async def _menu_add(self) -> None:
        assert self.draft is not None
        field = self.steps[self.index]
        text = self.query_one("#menu-new", Input).value.strip()
        if not text:
            self._set_message(
                "Введите имя записи" if field.is_map else "Введите элемент"
            )
            return
        current = field.value(self.draft)
        if field.is_map:
            entries = dict(current) if isinstance(current, dict) else {}
            if text in entries:
                self._set_message("Запись с таким именем уже есть")
                return
            templates = self.query("#menu-template")
            source = templates.first(Select).value if templates else None
            entries[text] = (
                copy.deepcopy(entries[source])
                if isinstance(source, str) and source in entries
                else skeleton(field.entry_schema, field.root)
            )
            new: object = entries
        else:
            new = [*(current if isinstance(current, list) else []), text]
            try:
                field.validate(new)
            except ValueError as exc:
                self._set_message(str(exc))
                return
        self.draft.set_value(field.file, field.path, new)
        self._set_message("")
        self._refresh_steps(field)
        if field.is_map:
            # The new entry opens at once: its own fields are what is left to fill in.
            target = (*field.path, text)
            self.index = next(
                (
                    i
                    for i, s in enumerate(self.steps)
                    if s.file == field.file and s.path == target
                ),
                self.index,
            )
        self._set_page()
        await self._rebuild_page()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "menu-items" and self.steps[self.index].is_map:
            await self._menu_open()

    def _compose_choices(
        self, field: ConfigurationStep, value: object, recommended: object
    ) -> ComposeResult:
        def label(choice: object) -> Text:
            mark = "★ " if choice == recommended else ""
            return Text(mark + str(choice) + " — " + field.effect(choice))

        options = field.choice_page(self.choice_page)
        if field.kind == "array":
            for offset, choice in enumerate(options):
                yield CheckBox(
                    label(choice),
                    value=isinstance(value, list) and choice in value,
                    id=f"array-choice-{offset}",
                )
        else:
            with RadioSet(id="field-choices"):
                for offset, choice in enumerate(options):
                    yield RadioButton(
                        label(choice), value=choice == value, id=f"choice-{offset}"
                    )
        if field.pages > 1:
            yield Button(
                f"More choices (page {self.choice_page + 1} / {field.pages}, per page {CHOICE_PAGE_SIZE})",
                id="choice-next-page",
            )

    def _compose_advisor(self) -> ComposeResult:
        yield Static(
            "ИИ-подсказки используют существующую авторизацию CLI; запрос запускается только по кнопке, проверка CLI модель не вызывает."
        )
        with RadioSet(id="advisor-provider"):
            yield RadioButton(_NO_ADVISOR, value=True)
            for key, provider in PROVIDERS.items():
                status = self.provider_status.get(key, _UNCHECKED)
                yield RadioButton(
                    provider.label + " — " + status,
                    disabled=key in self.provider_status
                    and not status.startswith("Готов"),
                )
        accept = Button("Принять совет ИИ", id="use-advisor", disabled=True)
        with Horizontal(classes="actions"):
            yield Button("Проверить CLI", id="check-advisor")
            yield Button("Спросить ИИ", id="ask-advisor")
            yield accept
        yield ReasonNote(
            lambda: [(accept, "сначала получите совет: «Спросить ИИ»")]
            if accept.disabled
            else [],
            id="advisor-reason",
        )
        yield Static("", id="advisor-output", markup=False)

    def _compose_review(self) -> ComposeResult:
        assert self.draft is not None
        problems = self.draft.problems()
        with SectionCard(title="Проверка", id="configuration-check"):
            yield Static(
                "\n".join(problems)
                if problems
                else "Контракт конфигурации соблюдён; окружение ещё нужно проверить.",
                markup=False,
            )
            targets = {
                index: problem
                for problem in problems
                if (index := step_for_problem(self.steps, problem)) is not None
            }
            if targets:
                yield Select(
                    [
                        (f"{'.'.join(self.steps[i].path)}: {p[:60]}", i)
                        for i, p in targets.items()
                    ],
                    prompt="Перейти к шагу с ошибкой",
                    id="problem-jump",
                )
        with SectionCard(title="Различия", id="configuration-changes"):
            yield Static(
                colorize_diff(self.draft.diff() or "Изменений нет"),
                id="configuration-diff",
                markup=False,
            )
        with SectionCard(title="Итоговые файлы", id="configuration-json"):
            for name, data in self.draft.documents.items():
                yield Static(
                    name + "\n" + json.dumps(data, ensure_ascii=False, indent=2),
                    markup=False,
                )
        with SectionCard(title="Версия", id="configuration-version"):
            yield self._store_line()
            yield Input(
                placeholder="Описание версии (необязательно)",
                id="version-description",
            )

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < brand.NARROW_WIDTH
        if narrow != self._narrow:
            self._narrow = narrow
            self._relabel_buttons()

    def _relabel_buttons(self) -> None:
        """The start page's buttons carry a shorter label on a narrow terminal."""
        if self.phase is not Phase.CHOOSE:
            return
        language = self._language()
        for identifier, labels in _START_LABELS.items():
            for button in self.query(f"#{identifier}").results(Button):
                button.label = i18n.tr(language, labels[self._narrow])

    def _refresh_notes(self) -> None:
        for note in self.query(ReasonNote):
            note.relabel()

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        self._relabel_buttons()
        for line in self.query(StatusLine):
            line.relabel()
        for error in self.query("#config-error").results(Static):
            error.update(self._message_text())
        for store in self.query(".store-path").results(WrappedValue):
            store.set_value(self._store_text())
        self._refresh_notes()

    def on_mount(self) -> None:
        self._narrow = self._is_narrow()
        if self.focus_target is not None:
            name, path = self.focus_target
            self.call_later(self._start, (name,), path)

    async def _start(self, names: tuple[str, ...], focus: tuple[str, ...] = ()) -> None:
        try:
            if not (self.repo / ".harness/project.schema.json").is_file():
                raise ValueError(
                    "Сначала установите pvmalove-suite или backend-orchestration через меню подключения проекта"
                )
            ConfigurationStore(self.repo).recover()
            self.draft = ConfigurationDraft.load(self.repo, names)
            self.steps = steps_for(self.draft)
            # Opening on a given field takes one build of the page, not two.
            self.index = next(
                (
                    i
                    for i, step in enumerate(self.steps)
                    if focus and step.path[: len(focus)] == focus
                ),
                0,
            )
            self.phase = Phase.EDITING
            self._remember("")
            self._set_page()
        except (ValueError, OSError) as exc:
            self._remember(str(exc))
        await self._rebuild_page()
        if self.phase is Phase.EDITING and not self.provider_status:
            # Availability is probed without a model request; the result labels the options.
            self._check_advisor(announce=False)

    def _set_page(self) -> None:
        self.choice_page = 0
        if self.draft is not None and self.steps:
            field = self.steps[self.index]
            current = field.value(self.draft)
            if field.kind != "array" and current in field.choices:
                self.choice_page = field.choices.index(current) // CHOICE_PAGE_SIZE

    def _refresh_steps(self, target: ConfigurationStep) -> None:
        assert self.draft is not None
        self.steps = steps_for(self.draft)
        # A removed object's former child resolves to the parent step, not a stale index.
        candidates = [
            (i, step)
            for i, step in enumerate(self.steps)
            if step.file == target.file and target.path[: len(step.path)] == step.path
        ]
        self.index = (
            max(candidates, key=lambda item: len(item[1].path))[0] if candidates else 0
        )

    def _save_field(self) -> bool:
        if self.phase is not Phase.EDITING or self.draft is None:
            return True
        field = self.steps[self.index]
        try:
            if field.is_section or (field.has_menu and not self.text_mode):
                # Fields, entries and items change the draft as they are edited; the checkbox
                # only adds or removes the whole value.
                if not self.query_one("#include-field", Checkbox).value:
                    if field.present(self.draft):
                        self.draft.remove_value(field.file, field.path)
                elif not field.present(self.draft):
                    self.draft.set_value(
                        field.file, field.path, [] if field.kind == "array" else {}
                    )
            elif self.query_one("#include-field", Checkbox).value:
                value = (
                    self.query_one("#field-boolean", Checkbox).value
                    if field.kind == "boolean"
                    else field.parse(self.query_one("#field-value", TextArea).text)
                )
                field.validate(value)
                self.draft.set_value(field.file, field.path, value)
            elif field.present(self.draft):
                self.draft.remove_value(field.file, field.path)
            self._remember("")
            return True
        except ValueError as exc:
            self._set_message(str(exc))
            return False

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "configuration-mode":
            text = event.pressed.id == "mode-text"
            if text != self.text_mode:
                await self._set_mode(text)
                if self.text_mode != text:
                    # Invalid JSON keeps the editor and its selected mode intact.
                    pending = self.query_one("#field-value", TextArea).text
                    await self._rebuild_page()
                    self.query_one("#field-value", TextArea).load_text(pending)
                self.query_one("#configuration-mode", RadioSet).focus()
            return
        if event.radio_set.id == "field-choices" and self.draft is not None:
            choices = self.steps[self.index].choice_page(self.choice_page)
            if 0 <= event.index < len(choices):
                self.query_one("#field-value", TextArea).load_text(
                    json.dumps(choices[event.index], ensure_ascii=False)
                )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        identifier = event.checkbox.id or ""
        if not identifier.startswith("array-choice-") or self.draft is None:
            return
        field = self.steps[self.index]
        area = self.query_one("#field-value", TextArea)
        try:
            values = field.parse(area.text)
            if not isinstance(values, list):
                return
            choice = field.choice_page(self.choice_page)[
                int(identifier.removeprefix("array-choice-"))
            ]
            if event.value and choice not in values:
                values.append(choice)
            elif not event.value and choice in values:
                values.remove(choice)
            area.load_text(json.dumps(values, ensure_ascii=False, indent=2))
        except (ValueError, IndexError):
            self._set_message("Сначала исправьте JSON списка")

    async def on_select_changed(self, event: Select.Changed) -> None:
        if self.advisor_pending:
            self.advisor_cancel.set()
            self.advisor_pending = False
        if event.select.id not in ("step-jump", "problem-jump") or not isinstance(
            event.value, int
        ):
            return
        if event.select.id == "step-jump" and event.value == self.index:
            return
        destination = self.steps[event.value]
        if self._save_field():
            if event.select.id == "problem-jump":
                self.phase = Phase.EDITING
            self._refresh_steps(destination)
            self._set_page()
            await self._rebuild_page()
        elif event.select.id == "step-jump":
            cast(Select[int], event.select).value = self.index

    # The shell's own handler (the menu toggle) stays; this one is async, so it overrides by name.
    async def on_button_pressed(  # type: ignore[override]
        self, event: Button.Pressed
    ) -> None:
        if self.busy:
            return
        key = event.button.id or ""
        if key not in _TARGETS and key not in self._actions:
            return
        if self.advisor_pending and key != "ask-advisor":
            self.advisor_cancel.set()
            self.advisor_pending = False
        if key in _TARGETS:
            await self._start(_TARGETS[key])
        elif key in self._actions:
            result = self._actions[key]()
            if inspect.isawaitable(result):
                await result

    async def _move(self, step: int, review: bool | None = None) -> None:
        if not self._save_field():
            return
        current = self.steps[self.index]
        self._refresh_steps(current)
        if review is False:
            self.phase = Phase.EDITING
        elif review or (step > 0 and self.index + 1 >= len(self.steps)):
            self.phase = Phase.REVIEW
        else:
            self.index = max(0, min(len(self.steps) - 1, self.index + step))
        self._set_page()
        await self._rebuild_page()

    async def _next_choice_page(self) -> None:
        if self._save_field():
            pages = self.steps[self.index].pages
            self.choice_page = (self.choice_page + 1) % pages
            await self._rebuild_page()

    def _fill_field(self, value: object) -> None:
        """Put a suggested value into the editor widgets; the draft changes on Next/Review."""
        field = self.steps[self.index]
        self.query_one("#include-field", Checkbox).value = True
        if field.kind == "boolean":
            self.query_one("#field-boolean", Checkbox).value = bool(value)
            return
        text = (
            value
            if field.kind == "string" and not field.choices
            else json.dumps(value, ensure_ascii=False, indent=2)
        )
        self.query_one("#field-value", TextArea).load_text(str(text))
        if field.kind == "array" and isinstance(value, list):
            # Programmatic ticks must not feed partial selections back into the JSON.
            with self.prevent(Checkbox.Changed):
                for offset, choice in enumerate(field.choice_page(self.choice_page)):
                    self.query_one(f"#array-choice-{offset}", Checkbox).value = (
                        choice in value
                    )

    def _use_recommendation(self) -> None:
        if self.draft is None:
            return
        value, _ = self.steps[self.index].recommendation(self.draft)
        if value is not None:
            self._fill_field(value)

    def _use_advice(self) -> None:
        if self.advice is not None:
            self._fill_field(self.advice.value)

    def _open_versions(self) -> None:
        from .configuration_versions import ConfigurationVersionsScreen

        self.app.push_screen(ConfigurationVersionsScreen(self.repo))

    def _confirm_apply(self) -> None:
        self.app.push_screen(
            ConfirmConfiguration(
                "Перезаписать выбранные JSON?\nЦель: "
                + str(self.repo)
                + "\nТекущий набор будет сохранён отдельной версией."
            ),
            self._confirmed_apply,
        )

    def _description(self) -> str:
        return self.query_one("#version-description", Input).value

    def _request_save_version(self) -> None:
        description = self._description()
        self._operation(lambda: self._save_version(description))

    def _request_save_current(self) -> None:
        description = self._description()
        self._operation(lambda: self._save_current(description))

    def _save_version(self, description: str) -> str:
        assert self.draft is not None
        if self.draft.problems():
            raise ValueError("Исправьте ошибки черновика")
        bundle = {**read_config_bundle(self.repo), **self.draft.encoded()}
        version = ConfigurationStore(self.repo).save(bundle, description)
        return (
            "Версия сохранена без применения: "
            + version.id
            + "\nХранилище: "
            + str(ConfigurationStore(self.repo).root)
        )

    def _save_current(self, description: str) -> str:
        version = ConfigurationStore(self.repo).save(
            read_config_bundle(self.repo), description
        )
        return (
            "Текущая конфигурация сохранена как версия: "
            + version.id
            + "\nХранилище: "
            + str(ConfigurationStore(self.repo).root)
        )

    def _confirmed_apply(self, confirmed: bool | None) -> None:
        if confirmed:
            self._operation(self._apply, reload=True)

    def _apply(self) -> str:
        assert self.draft is not None
        if self.draft.problems():
            raise ValueError("Исправьте ошибки черновика")
        backup = ConfigurationStore(self.repo).apply(
            self.draft.encoded(), expected=self.draft.original
        )
        return "Конфигурация применена. Предыдущая версия: " + backup.id

    async def _reload_external(self) -> None:
        if self.draft is None:
            return
        try:
            conflicts = self.draft.rebase(read_config_bundle(self.repo))
        except (ValueError, OSError) as exc:
            self._remember(str(exc))
        else:
            self.stale = False
            self._remember(
                "Внешние изменения загружены, ваши правки сохранены."
                + (
                    " Изменено и снаружи, оставлены ваши значения: "
                    + ", ".join(conflicts)
                    if conflicts
                    else ""
                ),
                "accepted",
            )
        await self._rebuild_page()

    def _operation(self, run: Callable[[], str], reload: bool = False) -> None:
        self.busy = True
        self._set_message("Выполняется…", "running")

        def work() -> None:
            stale = False
            ok = True
            try:
                message = run()
            except StaleConfiguration as exc:
                message, ok, stale = str(exc), False, True
            except (ValueError, OSError) as exc:
                message, ok = str(exc), False
            call_from_worker(
                self, lambda: self._finished(message, ok and reload, stale, ok)
            )

        self.run_worker(work, thread=True, group="configuration", exclusive=True)

    async def _finished(
        self, message: str, reload: bool, stale: bool, ok: bool = False
    ) -> None:
        self.busy = False
        kind = "accepted" if ok else "error"
        self.stale = stale or (self.stale and not reload)
        self._set_message(message, kind)
        if stale:
            await self._rebuild_page()
        elif reload:
            if self.draft is not None:
                self.draft = ConfigurationDraft.load(
                    self.repo, tuple(self.draft.documents)
                )
            await self._rebuild_page()
            self._set_message(message, kind)

    def _selected_provider(self) -> str | None:
        index = self.query_one("#advisor-provider", RadioSet).pressed_index
        return _PROVIDER_KEYS[index - 1] if 1 <= index <= len(_PROVIDER_KEYS) else None

    def _ask_advisor(self) -> None:
        if self.draft is None or self.advisor_pending:
            return
        provider = self._selected_provider()
        if provider is None:
            self.query_one("#advisor-output", Static).update(
                "ИИ отключён. Обычные подсказки доступны выше."
            )
            return
        if not self._save_field():
            return
        # Own request state prevents late results from updating another field or project.
        cancel = Event()
        self.advisor_cancel = cancel
        self.advisor_pending = True
        self.advice = None
        self.query_one("#use-advisor", Button).disabled = True
        self._refresh_notes()
        self.query_one("#advisor-output", Static).update(
            "Запрашивается рекомендация через текущую авторизацию CLI…"
        )
        draft, field = self.draft, self.steps[self.index]

        def work() -> None:
            try:
                advice = advise(provider, draft, field, cancel)
                message = _advice_text(advice)
            except AdvisorUnavailable as exc:
                advice, message = None, str(exc)
            call_from_worker(
                self, lambda: self._advisor_finished(cancel, advice, message)
            )

        self.run_worker(work, thread=True, group="advisor", exclusive=True)

    def _check_advisor(self, announce: bool = True) -> None:
        # A model-free probe: it survives navigation and only ends with the screen.
        self.check_cancel.set()
        cancel = self.check_cancel = Event()
        if announce:
            self.query_one("#advisor-output", Static).update(
                "Проверяется наличие, версия и текущая авторизация CLI…"
            )

        def work() -> None:
            statuses = {key: availability(key, cancel) for key in PROVIDERS}
            call_from_worker(self, lambda: self._providers_finished(cancel, statuses))

        self.run_worker(work, thread=True, group="advisor-check", exclusive=True)

    def _providers_finished(self, cancel: Event, statuses: dict[str, str]) -> None:
        if cancel is not self.check_cancel or cancel.is_set():
            return
        self.provider_status = statuses
        if self.phase is not Phase.EDITING:
            return
        buttons = list(self.query_one("#advisor-provider", RadioSet).query(RadioButton))
        for index, (key, provider) in enumerate(PROVIDERS.items(), 1):
            buttons[index].label = Text(provider.label + " — " + statuses[key])
            buttons[index].disabled = not statuses[key].startswith("Готов")
            if buttons[index].disabled and buttons[index].value:
                buttons[0].value = True
        self.query_one("#advisor-output", Static).update(
            "Готовность CLI проверена; запрос к модели ещё не выполнялся."
        )

    def _advisor_finished(
        self, cancel: Event, advice: Advice | None, message: str
    ) -> None:
        if (
            cancel is not self.advisor_cancel
            or cancel.is_set()
            or self.phase is not Phase.EDITING
        ):
            return
        self.advisor_pending = False
        self.advice = advice
        self.query_one("#advisor-output", Static).update(message)
        self.query_one("#use-advisor", Button).disabled = advice is None
        self._refresh_notes()

    # Every way out of the wizard - Esc, a number key, the menu, a breadcrumb, also from the
    # versions screen above it - goes through one guard: it waits for a running write and asks
    # before an unapplied draft is dropped. A breadcrumb needs no guard of its own: the shell sends
    # it to open_section, which has it.
    async def action_back(self) -> None:
        self.leave(None)

    async def open_section(self, index: int) -> None:
        self.leave(index)

    def leave(self, index: int | None, above: Screen[None] | None = None) -> None:
        """Every way out of the wizard, also from the versions screen `above` it: wait for a running
        write, ask once before an unapplied draft is dropped, then close and open section `index`."""
        self.advisor_cancel.set()
        if self.busy:
            self.notify(
                i18n.tr(self._language(), "Дождитесь окончания записи"),
                severity="warning",
            )
            return
        if self.draft is not None and self.draft.changed():
            self.app.push_screen(
                ConfirmConfiguration(
                    "Закрыть мастер и отбросить неприменённый черновик?"
                ),
                lambda yes: self._close(index, above) if yes else None,
            )
        else:
            self._close(index, above)

    def _close(self, index: int | None, above: Screen[None] | None) -> None:
        if above is not None:
            # Only the active screen can be dismissed: the versions screen goes first.
            above.dismiss(None)
            self.app.call_later(self._close, index, None)
            return
        leave_for_section(self, index)

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def on_unmount(self) -> None:
        self.advisor_cancel.set()
        self.check_cancel.set()
        self.workers.cancel_node(self)


def _advice_text(advice: Advice) -> str:
    lines = [
        "Предложено: " + json.dumps(advice.value, ensure_ascii=False),
        "Почему: " + advice.why,
        "Влияние: " + advice.impact,
    ]
    lines.extend(f"• {option}: {tradeoff}" for option, tradeoff in advice.comparison)
    return "\n".join(lines)
