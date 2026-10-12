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
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
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
from .. import brand
from ..chrome import TopBar
from ..translate import Translated
from ..logtext import colorize_diff
from ..widgets import CheckBox, RoundRadioButton
from ..advisor import PROVIDERS, Advice, AdvisorUnavailable, advise, availability
from ..configuration import ConfigurationDraft
from ..configuration_steps import (
    skeleton,
    CHOICE_PAGE_SIZE,
    ConfigurationStep,
    step_for_problem,
    steps_for,
)
from .commands import call_from_worker

_PROVIDER_KEYS = tuple(PROVIDERS)
_NO_ADVISOR = "Без ИИ — обычные подсказки"
_UNCHECKED = "доступность ещё не проверена"
_TARGETS = {
    "configure-project": ("project.json",),
    "configure-orchestration": ("orchestration.json",),
    "configure-both": ("project.json", "orchestration.json"),
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
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    ConfirmConfiguration { align: center middle; }
    ConfirmConfiguration > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%; padding: 0 1;
        border: round $primary; border-title-color: $primary;
    }
    ConfirmConfiguration #confirm-text { height: auto; max-height: 20; }
    ConfirmConfiguration #confirm-text Static { height: auto; }
    ConfirmConfiguration .actions { height: auto; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical() as dialog:
            dialog.border_title = brand.frame_title("Подтверждение")
            with VerticalScroll(id="confirm-text"):
                yield Static(self.text, markup=False)
            with Horizontal(classes="actions"):
                yield Button("Подтвердить", id="confirm")
                yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfigurationScreen(Translated, Screen[None]):
    BINDINGS = [Binding("escape", "cancel", "Назад")]
    DEFAULT_CSS = """
    ConfigurationScreen #configuration-scroll { width: 100%; height: 1fr; overflow-x: hidden; }
    ConfigurationScreen.-choose #configuration-scroll { height: auto; }
    ConfigurationScreen #configuration-project { color: $text-muted; }
    ConfigurationScreen .store-path { color: $text-muted; }
    ConfigurationScreen .configuration-choice { height: auto; }
    ConfigurationScreen Vertical.frame { height: auto; overflow: hidden hidden; }
    ConfigurationScreen #configuration-actions { height: auto; }
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
    """

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
        }

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Настройка")
        # The start screen hugs its content; the long step and review forms fill the screen so
        # their action row stays at the bottom.
        self.set_class(self.phase is Phase.CHOOSE, "-choose")
        with VerticalScroll(id="configuration-scroll"):
            with Vertical(id="configuration-target", classes="frame") as target:
                target.border_title = brand.frame_title("Цель")
                yield Static(
                    "Проект: " + brand.display_path(self.repo)
                    if self.phase is Phase.CHOOSE
                    else "Цель: " + str(self.repo),
                    id="configuration-project",
                    markup=False,
                )
            if self.phase is Phase.CHOOSE:
                yield from self._compose_choose()
            elif self.phase is Phase.EDITING:
                yield from self._compose_field()
            else:
                yield from self._compose_review()
        with Vertical(id="configuration-actions", classes="frame") as actions:
            actions.border_title = brand.frame_title("Действия")
            # The start screen has no actions: this frame appears there only with a message.
            actions.display = self.phase is not Phase.CHOOSE or bool(self.message)
            error = Static(self.message, id="config-error", markup=False)
            error.display = bool(self.message)
            yield error
            with Horizontal(classes="actions"):
                if self.phase is Phase.EDITING:
                    yield Button(
                        "Предыдущий шаг", id="previous-step", disabled=self.index == 0
                    )
                    yield Button("Далее", id="next-step")
                    yield Button("Просмотр", id="review-config")
                elif self.phase is Phase.REVIEW:
                    assert self.draft is not None
                    invalid = bool(self.draft.problems())
                    yield Button("К шагам", id="back-to-steps")
                    yield Button(
                        "Применить", id="apply-config", disabled=invalid or self.stale
                    )
                    yield Button(
                        "Сохранить версию", id="save-version", disabled=invalid
                    )
                    if self.stale:
                        yield Button(
                            "Загрузить внешние изменения", id="reload-external"
                        )
                    yield Button("Версии и откат", id="open-versions")
        yield Footer()

    def _set_message(self, text: str) -> None:
        """Show a message in the actions frame, which the start screen otherwise hides."""
        error = self.query_one("#config-error", Static)
        error.update(text)
        error.display = bool(text)
        self.query_one("#configuration-actions").display = (
            self.phase is not Phase.CHOOSE or bool(text)
        )

    def _compose_choose(self) -> ComposeResult:
        with Vertical(
            id="configuration-editing", classes="frame configuration-choice"
        ) as editing:
            editing.border_title = brand.frame_title("Редактирование конфигурации")
            yield Static("Выберите, какие файлы настроить.")
            with Horizontal(classes="actions"):
                yield Button("project.json", id="configure-project")
                yield Button("orchestration.json", id="configure-orchestration")
                yield Button("Both files", id="configure-both")
            yield Static("Изменения применяются после просмотра и подтверждения.")
        with Vertical(
            id="configuration-versions", classes="frame configuration-choice"
        ) as versions:
            versions.border_title = brand.frame_title("Сохранённые версии")
            yield Static("Сохранить текущий набор без редактирования.")
            yield Static(
                "Хранилище: " + str(ConfigurationStore(self.repo).root),
                id="configuration-store-path",
                classes="store-path",
                markup=False,
            )
            yield Input(
                placeholder="Описание версии (необязательно)", id="version-description"
            )
            with Horizontal(classes="actions"):
                yield Button("Save version", id="save-current-version")
                yield Button("Versions / restore", id="open-versions")

    def _compose_field(self) -> ComposeResult:
        assert self.draft is not None
        field = self.steps[self.index]
        groups = list(dict.fromkeys((step.file, step.path[0]) for step in self.steps))
        group = groups.index((field.file, field.path[0])) + 1
        with Vertical(id="configuration-step", classes="frame") as step:
            step.border_title = brand.frame_title("Шаг")
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
        with Vertical(id="configuration-field", classes="frame") as parameter:
            parameter.border_title = brand.frame_title("Параметр")
            yield from self._compose_parameter(field)
        with Vertical(id="configuration-advisor", classes="frame") as advisor:
            advisor.border_title = brand.frame_title("ИИ-подсказки")
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
        with Horizontal(classes="actions"):
            if field.is_map:
                yield Button("Настроить запись", id="menu-open", disabled=not rows)
                yield Button("Удалить запись", id="menu-remove", disabled=not rows)
            else:
                yield Button("Удалить элемент", id="menu-remove", disabled=not rows)
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
            await self.recompose()

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
        await self.recompose()

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
        await self.recompose()

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
        await self.recompose()

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
        with Horizontal(classes="actions"):
            yield Button("Проверить CLI", id="check-advisor")
            yield Button("Спросить ИИ", id="ask-advisor")
            yield Button("Принять совет ИИ", id="use-advisor", disabled=True)
        yield Static("", id="advisor-output", markup=False)

    def _compose_review(self) -> ComposeResult:
        assert self.draft is not None
        problems = self.draft.problems()
        with Vertical(id="configuration-check", classes="frame") as check:
            check.border_title = brand.frame_title("Проверка")
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
        with Vertical(id="configuration-changes", classes="frame") as changes:
            changes.border_title = brand.frame_title("Различия")
            yield Static(
                colorize_diff(self.draft.diff() or "Изменений нет"),
                id="configuration-diff",
                markup=False,
            )
        with Vertical(id="configuration-json", classes="frame") as documents:
            documents.border_title = brand.frame_title("Итоговые файлы")
            for name, data in self.draft.documents.items():
                yield Static(
                    name + "\n" + json.dumps(data, ensure_ascii=False, indent=2),
                    markup=False,
                )
        with Vertical(id="configuration-version", classes="frame") as version:
            version.border_title = brand.frame_title("Версия")
            yield Static(
                "Хранилище: " + str(ConfigurationStore(self.repo).root),
                id="configuration-store-path",
                classes="store-path",
                markup=False,
            )
            yield Input(
                placeholder="Описание версии (необязательно)",
                id="version-description",
            )

    def on_mount(self) -> None:
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
            self.message = ""
            self._set_page()
        except (ValueError, OSError) as exc:
            self.message = str(exc)
        await self.recompose()
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
            self.message = ""
            return True
        except ValueError as exc:
            self.message = str(exc)
            self._set_message(self.message)
            return False

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "configuration-mode":
            text = event.pressed.id == "mode-text"
            if text != self.text_mode:
                await self._set_mode(text)
                if self.text_mode != text:
                    # Invalid JSON keeps the editor and its selected mode intact.
                    pending = self.query_one("#field-value", TextArea).text
                    await self.recompose()
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
            await self.recompose()
        elif event.select.id == "step-jump":
            cast(Select[int], event.select).value = self.index

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        key = event.button.id or ""
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
        await self.recompose()

    async def _next_choice_page(self) -> None:
        if self._save_field():
            pages = self.steps[self.index].pages
            self.choice_page = (self.choice_page + 1) % pages
            await self.recompose()

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
            self.message = str(exc)
        else:
            self.stale = False
            self.message = "Внешние изменения загружены, ваши правки сохранены." + (
                " Изменено и снаружи, оставлены ваши значения: " + ", ".join(conflicts)
                if conflicts
                else ""
            )
        await self.recompose()

    def _operation(self, run: Callable[[], str], reload: bool = False) -> None:
        self.busy = True
        self._set_message("Выполняется…")

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
                self, lambda: self._finished(message, ok and reload, stale)
            )

        self.run_worker(work, thread=True, group="configuration", exclusive=True)

    async def _finished(self, message: str, reload: bool, stale: bool) -> None:
        self.busy = False
        self.message = message
        self.stale = stale or (self.stale and not reload)
        self._set_message(message)
        if stale:
            await self.recompose()
        elif reload and self.draft is not None:
            self.draft = ConfigurationDraft.load(self.repo, tuple(self.draft.documents))
            await self.recompose()
            self._set_message(message)

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

    def action_cancel(self) -> None:
        self.advisor_cancel.set()
        if self.busy:
            self.notify("Дождитесь окончания записи", severity="warning")
            return
        if self.draft is not None and self.draft.changed():
            self.app.push_screen(
                ConfirmConfiguration(
                    "Закрыть мастер и отбросить неприменённый черновик?"
                ),
                self._discard,
            )
        else:
            self.dismiss(None)

    def _discard(self, yes: bool | None) -> None:
        if yes:
            self.dismiss(None)

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
