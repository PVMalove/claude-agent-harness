"""Guided configuration editing; only explicit confirmation applies a validated draft."""

from __future__ import annotations

import json
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

from harness.configuration import ConfigurationStore, read_config_bundle
from ..advisor import Advice, AdvisorUnavailable, advise, availability
from ..configuration import ConfigurationDraft
from ..configuration_steps import ConfigurationStep, steps_for
from .commands import call_from_worker


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
        self.phase = "choose"
        self.draft: ConfigurationDraft | None = None
        self.steps: list[ConfigurationStep] = []
        self.index = 0
        self.choice_page = 0
        self.message = ""
        self.busy = False
        self.advice: Advice | None = None
        self.advisor_cancel = Event()
        self.advisor_pending = False
        self.provider_status: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="configuration-scroll"):
            yield Static(
                "Настройка конфигурации\nЦель: " + str(self.repo), markup=False
            )
            if self.phase == "choose":
                yield Static(
                    "Рабочие JSON изменяются только после итогового просмотра и подтверждения."
                )
                with Horizontal(classes="actions"):
                    yield Button("project.json", id="configure-project")
                    yield Button("orchestration.json", id="configure-orchestration")
                    yield Button("Оба файла", id="configure-both")
                    yield Button("Версии", id="open-versions")
            elif self.phase == "editing":
                yield from self._compose_field()
            else:
                yield from self._compose_review()
        yield Static(self.message, id="config-error", markup=False)
        with Horizontal(classes="actions"):
            if self.phase == "editing":
                yield Button("Назад", id="previous-step", disabled=self.index == 0)
                yield Button("Далее", id="next-step")
                yield Button("Просмотр итога", id="review-config")
            elif self.phase == "review":
                assert self.draft is not None
                invalid = bool(self.draft.problems())
                yield Button("К шагам", id="back-to-steps")
                yield Button("Применить", id="apply-config", disabled=invalid)
                yield Button("Сохранить версию", id="save-version", disabled=invalid)
                yield Button("Версии / откат", id="open-versions")
            yield Button("Закрыть / отменить", id="cancel-config")
        yield Footer()

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
            "Рекомендация: "
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
                if field.kind == "array":
                    for offset, choice in enumerate(
                        field.choice_page(self.choice_page)
                    ):
                        yield Checkbox(
                            Text(str(choice) + " — " + field.effect(choice)),
                            value=isinstance(value, list) and choice in value,
                            id=f"array-choice-{offset}",
                        )
                else:
                    with RadioSet(id="field-choices"):
                        for offset, choice in enumerate(
                            field.choice_page(self.choice_page)
                        ):
                            yield RadioButton(
                                Text(str(choice) + " — " + field.effect(choice)),
                                value=choice == value,
                                id=f"choice-{offset}",
                            )
                if len(field.choices) > 4:
                    yield Button("Другие варианты (группы по 4)", id="choice-next-page")
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
        yield Static(
            "ИИ-подсказки используют существующую авторизацию CLI; запрос запускается только по кнопке."
        )
        with RadioSet(id="advisor-provider"):
            yield RadioButton("Без ИИ — обычные подсказки", value=True)
            for provider, label in (
                ("codex", "Codex CLI"),
                ("claude", "Claude Code CLI"),
            ):
                status = self.provider_status.get(
                    provider, "доступность ещё не проверена"
                )
                yield RadioButton(
                    label + " — " + status,
                    disabled=provider in self.provider_status
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
        yield Static(self.draft.diff(), id="configuration-diff", markup=False)
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
            self.phase = "editing"
            self.message = ""
            self._set_page()
        except (ValueError, OSError) as exc:
            self.message = str(exc)
        await self.recompose()

    def _set_page(self) -> None:
        self.choice_page = 0
        if self.draft is not None and self.steps:
            field = self.steps[self.index]
            current = field.value(self.draft)
            if field.kind != "array" and current in field.choices:
                self.choice_page = field.choices.index(current) // 4

    def _refresh_steps(self, file: str, path: tuple[str, ...]) -> None:
        assert self.draft is not None
        self.steps = steps_for(self.draft)
        # A removed object's former child resolves to the parent step, not a stale index.
        candidates = [
            (i, step)
            for i, step in enumerate(self.steps)
            if step.file == file and path[: len(step.path)] == step.path
        ]
        self.index = (
            max(candidates, key=lambda item: len(item[1].path))[0] if candidates else 0
        )

    def _save_field(self) -> bool:
        if self.phase != "editing" or self.draft is None:
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
                if field.path == ("branch_pattern",):
                    import re

                    try:
                        re.compile(str(value))
                    except re.error:
                        raise ValueError(
                            "Некорректное регулярное выражение имён веток"
                        ) from None
                self.draft.set_value(field.file, field.path, value)
            elif field.present(self.draft):
                target = self.draft.documents[field.file]
                for key in field.path[:-1]:
                    target = target[key]
                target.pop(field.path[-1], None)
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
        if (
            event.select.id != "step-jump"
            or not isinstance(event.value, int)
            or event.value == self.index
        ):
            return
        destination = self.steps[event.value]
        if self._save_field():
            self._refresh_steps(destination.file, destination.path)
            self._set_page()
            await self.recompose()
        else:
            cast(Select[int], event.select).value = self.index

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        key = event.button.id
        if self.advisor_pending and key != "ask-advisor":
            self.advisor_cancel.set()
            self.advisor_pending = False
        names = {
            "configure-project": ("project.json",),
            "configure-orchestration": ("orchestration.json",),
            "configure-both": ("project.json", "orchestration.json"),
        }
        if key in names:
            await self._start(names[key])
        elif key in ("next-step", "previous-step", "review-config", "back-to-steps"):
            if not self._save_field():
                return
            current = self.steps[self.index]
            self._refresh_steps(current.file, current.path)
            if key == "back-to-steps":
                self.phase = "editing"
            elif key == "review-config" or (
                key == "next-step" and self.index + 1 >= len(self.steps)
            ):
                self.phase = "review"
            else:
                self.index = max(
                    0,
                    min(
                        len(self.steps) - 1,
                        self.index + (1 if key == "next-step" else -1),
                    ),
                )
            self._set_page()
            await self.recompose()
        elif key == "choice-next-page":
            if self._save_field():
                self.choice_page = (self.choice_page + 1) % (
                    (len(self.steps[self.index].choices) + 3) // 4
                )
                await self.recompose()
        elif key == "use-recommendation" and self.draft is not None:
            value, _ = self.steps[self.index].recommendation(self.draft)
            field = self.steps[self.index]
            if value is not None:
                self.query_one("#include-field", Checkbox).value = True
                if field.kind == "boolean":
                    self.query_one("#field-boolean", Checkbox).value = bool(value)
                else:
                    text = (
                        value
                        if field.kind == "string" and not field.choices
                        else json.dumps(value, ensure_ascii=False, indent=2)
                    )
                    self.query_one("#field-value", TextArea).load_text(str(text))
        elif key == "apply-config":
            self.app.push_screen(
                ConfirmConfiguration(
                    "Перезаписать выбранные JSON?\nЦель: "
                    + str(self.repo)
                    + "\nТекущий набор будет сохранён отдельной версией."
                ),
                self._confirmed_apply,
            )
        elif key == "save-version":
            description = self.query_one("#version-description", Input).value
            self._operation(lambda: self._save_version(description))
        elif key == "open-versions":
            from .configuration_versions import ConfigurationVersionsScreen

            self.app.push_screen(ConfigurationVersionsScreen(self.repo))
        elif key == "cancel-config":
            self.action_cancel()
        elif key == "ask-advisor":
            self._ask_advisor()
        elif key == "check-advisor":
            self._check_advisor()
        elif key == "use-advisor" and self.advice is not None:
            field = self.steps[self.index]
            self.query_one("#include-field", Checkbox).value = True
            if field.kind == "boolean":
                self.query_one("#field-boolean", Checkbox).value = bool(
                    self.advice.value
                )
            else:
                value = self.advice.value
                text = (
                    value
                    if field.kind == "string" and not field.choices
                    else json.dumps(value, ensure_ascii=False, indent=2)
                )
                self.query_one("#field-value", TextArea).load_text(str(text))

    def _save_version(self, description: str) -> str:
        assert self.draft is not None
        if self.draft.problems():
            raise ValueError("Исправьте ошибки черновика")
        bundle = {**read_config_bundle(self.repo), **self.draft.encoded()}
        version = ConfigurationStore(self.repo).save(bundle, description)
        return "Версия сохранена без применения: " + version.id

    def _confirmed_apply(self, confirmed: bool | None) -> None:
        if confirmed:
            self._operation(self._apply)

    def _apply(self) -> str:
        assert self.draft is not None
        if self.draft.problems():
            raise ValueError("Исправьте ошибки черновика")
        backup = ConfigurationStore(self.repo).apply(
            self.draft.encoded(), expected=self.draft.original
        )
        return "Конфигурация применена. Предыдущая версия: " + backup.id

    def _operation(self, run: Callable[[], str]) -> None:
        self.busy = True
        self.query_one("#config-error", Static).update("Выполняется…")

        def work() -> None:
            try:
                message = run()
            except (ValueError, OSError) as exc:
                message = str(exc)
            call_from_worker(self, lambda: self._finished(message))

        self.run_worker(work, thread=True, group="configuration", exclusive=True)

    async def _finished(self, message: str) -> None:
        self.busy = False
        self.message = message
        self.query_one("#config-error", Static).update(message)
        if message.startswith("Конфигурация применена") and self.draft is not None:
            self.draft = ConfigurationDraft.load(self.repo, tuple(self.draft.documents))
            await self.recompose()
            self.query_one("#config-error", Static).update(message)

    def _ask_advisor(self) -> None:
        if self.draft is None or self.advisor_pending:
            return
        index = self.query_one("#advisor-provider", RadioSet).pressed_index
        if index not in (1, 2):
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
                advice = advise(
                    "codex" if index == 1 else "claude", draft, field, cancel
                )
                message = (
                    "Предложено: "
                    + json.dumps(advice.value, ensure_ascii=False)
                    + "\nПочему: "
                    + advice.why
                    + "\nВлияние: "
                    + advice.impact
                )
            except AdvisorUnavailable as exc:
                advice, message = None, str(exc)
            call_from_worker(
                self, lambda: self._advisor_finished(cancel, advice, message)
            )

        self.run_worker(work, thread=True, group="advisor", exclusive=True)

    def _check_advisor(self) -> None:
        if self.advisor_pending:
            return
        cancel = Event()
        self.advisor_cancel = cancel
        self.advisor_pending = True
        self.query_one("#advisor-output", Static).update(
            "Проверяется наличие, версия и текущая авторизация CLI…"
        )

        def work() -> None:
            statuses = {
                provider: availability(provider, cancel)
                for provider in ("codex", "claude")
            }
            call_from_worker(self, lambda: self._providers_finished(cancel, statuses))

        self.run_worker(work, thread=True, group="advisor", exclusive=True)

    def _providers_finished(self, cancel: Event, statuses: dict[str, str]) -> None:
        if (
            cancel is not self.advisor_cancel
            or cancel.is_set()
            or self.phase != "editing"
        ):
            return
        self.advisor_pending = False
        self.provider_status = statuses
        buttons = list(self.query_one("#advisor-provider", RadioSet).query(RadioButton))
        for index, (provider, label) in enumerate(
            (("codex", "Codex CLI"), ("claude", "Claude Code CLI")), 1
        ):
            buttons[index].label = Text(label + " — " + statuses[provider])
            buttons[index].disabled = not statuses[provider].startswith("Готов")
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
            or self.phase != "editing"
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
        if self.draft is not None and self.draft.diff() != "Изменений нет":
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
        self.workers.cancel_node(self)
