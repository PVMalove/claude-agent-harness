"""Guided configuration editing; only explicit confirmation applies a validated draft."""

from __future__ import annotations

import inspect
import json
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Callable, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)

from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore, StaleConfiguration
from ..advisor import PROVIDERS, Advice, AdvisorUnavailable, advise, availability
from ..configuration import ConfigurationDraft
from ..configuration_steps import (
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


class ConfirmConfiguration(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    ConfirmConfiguration { align: center middle; }
    ConfirmConfiguration > Vertical { width: 80%; height: auto; max-height: 80%; padding: 1 2; border: round $primary; }
    ConfirmConfiguration .actions { height: auto; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.text, markup=False)
            with Horizontal(classes="actions"):
                yield Button("Подтвердить", id="confirm")
                yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfigurationScreen(Screen[None]):
    BINDINGS = [Binding("escape", "cancel", "Назад")]
    DEFAULT_CSS = """
    ConfigurationScreen #configuration-scroll { height: 1fr; margin: 0 1; }
    ConfigurationScreen .actions { height: auto; }
    ConfigurationScreen Static { height: auto; }
    ConfigurationScreen RadioSet { height: auto; }
    ConfigurationScreen #field-value { height: 9; min-height: 3; }
    ConfigurationScreen #config-error { color: $error; }
    """

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.phase = Phase.CHOOSE
        self.draft: ConfigurationDraft | None = None
        self.steps: list[ConfigurationStep] = []
        self.index = 0
        self.choice_page = 0
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
            "cancel-config": self.action_cancel,
            "ask-advisor": self._ask_advisor,
            "check-advisor": self._check_advisor,
        }

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="configuration-scroll"):
            yield Static(
                "Настройка конфигурации\nЦель: " + str(self.repo), markup=False
            )
            if self.phase is Phase.CHOOSE:
                yield from self._compose_choose()
            elif self.phase is Phase.EDITING:
                yield from self._compose_field()
            else:
                yield from self._compose_review()
        yield Static(self.message, id="config-error", markup=False)
        with Horizontal(classes="actions"):
            if self.phase is Phase.EDITING:
                yield Button("Назад", id="previous-step", disabled=self.index == 0)
                yield Button("Далее", id="next-step")
                yield Button("Просмотр итога", id="review-config")
            elif self.phase is Phase.REVIEW:
                assert self.draft is not None
                invalid = bool(self.draft.problems())
                yield Button("К шагам", id="back-to-steps")
                yield Button(
                    "Применить", id="apply-config", disabled=invalid or self.stale
                )
                yield Button("Сохранить версию", id="save-version", disabled=invalid)
                if self.stale:
                    yield Button("Загрузить внешние изменения", id="reload-external")
                yield Button("Версии / откат", id="open-versions")
            yield Button("Закрыть / отменить", id="cancel-config")
        yield Footer()

    def _compose_choose(self) -> ComposeResult:
        yield Static(
            "Рабочие JSON изменяются только после итогового просмотра и подтверждения."
        )
        with Horizontal(classes="actions"):
            yield Button("project.json", id="configure-project")
            yield Button("orchestration.json", id="configure-orchestration")
            yield Button("Оба файла", id="configure-both")
        yield Static(
            "Версии: сохранить текущий набор как есть, выбрать версию, сравнить и восстановить."
        )
        yield Input(
            placeholder="Описание версии (необязательно)", id="version-description"
        )
        with Horizontal(classes="actions"):
            yield Button("Сохранить версию", id="save-current-version")
            yield Button("Выбрать версию / восстановить", id="open-versions")

    def _compose_field(self) -> ComposeResult:
        assert self.draft is not None
        field = self.steps[self.index]
        groups = list(dict.fromkeys((step.file, step.path[0]) for step in self.steps))
        group = groups.index((field.file, field.path[0])) + 1
        yield Static(
            f"Группа {group} / {len(groups)}: {field.path[0]}\nШаг {self.index + 1} / {len(self.steps)}: {field.file} → {'.'.join(field.path)}",
            markup=False,
        )
        yield Select(
            [(f"{s.file}: {'.'.join(s.path)}", i) for i, s in enumerate(self.steps)],
            value=self.index,
            allow_blank=False,
            id="step-jump",
        )
        yield Static(field.description, markup=False)
        recommendation, why = field.recommendation(self.draft)
        yield Static(
            "Рекомендация (★): "
            + json.dumps(recommendation, ensure_ascii=False)
            + "\nПочему: "
            + why,
            markup=False,
        )
        yield Checkbox(
            "Использовать параметр (снять — оставить его отсутствующим)",
            value=field.present(self.draft) or field.required,
            disabled=field.required,
            id="include-field",
        )
        value = field.value(self.draft)
        if field.kind == "boolean":
            yield Checkbox(
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
                else "Значение в JSON; объект/список можно редактировать целиком"
            )
            yield TextArea(str(text or ""), id="field-value", soft_wrap=True)
        with Horizontal(classes="actions"):
            yield Button("Взять рекомендацию", id="use-recommendation")
        yield from self._compose_advisor()

    def _compose_choices(
        self, field: ConfigurationStep, value: object, recommended: object
    ) -> ComposeResult:
        def label(choice: object) -> Text:
            mark = "★ " if choice == recommended else ""
            return Text(mark + str(choice) + " — " + field.effect(choice))

        options = field.choice_page(self.choice_page)
        if field.kind == "array":
            for offset, choice in enumerate(options):
                yield Checkbox(
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
                f"Другие варианты (страница {self.choice_page + 1} / {field.pages}, по {CHOICE_PAGE_SIZE})",
                id="choice-next-page",
            )

    def _compose_advisor(self) -> ComposeResult:
        yield Static(
            "ИИ-подсказки используют существующую авторизацию CLI; запрос запускается только по кнопке."
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
        yield Button("Проверить CLI (без запроса к модели)", id="check-advisor")
        yield Button("Подсказать с ИИ", id="ask-advisor")
        yield Button("Взять ИИ-рекомендацию", id="use-advisor", disabled=True)
        yield Static("", id="advisor-output", markup=False)

    def _compose_review(self) -> ComposeResult:
        assert self.draft is not None
        problems = self.draft.problems()
        yield Static(
            "Проверка: "
            + (
                "\n".join(problems)
                if problems
                else "контракт конфигурации соблюдён; окружение ещё нужно проверить"
            ),
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
        yield Static(
            self.draft.diff() or "Изменений нет", id="configuration-diff", markup=False
        )
        for name, data in self.draft.documents.items():
            yield Static(
                name + "\n" + json.dumps(data, ensure_ascii=False, indent=2),
                markup=False,
            )
        yield Input(
            placeholder="Описание версии (необязательно)", id="version-description"
        )

    async def _start(self, names: tuple[str, ...]) -> None:
        try:
            if not (self.repo / ".harness/project.schema.json").is_file():
                raise ValueError(
                    "Сначала установите pvmalove-suite или backend-orchestration через меню подключения проекта"
                )
            ConfigurationStore(self.repo).recover()
            self.draft = ConfigurationDraft.load(self.repo, names)
            self.steps = steps_for(self.draft)
            self.index = 0
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
            if self.query_one("#include-field", Checkbox).value:
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
            self.query_one("#config-error", Static).update(self.message)
            return False

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
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
            self.query_one("#config-error", Static).update(
                "Сначала исправьте JSON списка"
            )

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
        return "Версия сохранена без применения: " + version.id

    def _save_current(self, description: str) -> str:
        version = ConfigurationStore(self.repo).save(
            read_config_bundle(self.repo), description
        )
        return "Текущая конфигурация сохранена как версия: " + version.id

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
        self.query_one("#config-error", Static).update("Выполняется…")

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
        self.query_one("#config-error", Static).update(message)
        if stale:
            await self.recompose()
        elif reload and self.draft is not None:
            self.draft = ConfigurationDraft.load(self.repo, tuple(self.draft.documents))
            await self.recompose()
            self.query_one("#config-error", Static).update(message)

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
