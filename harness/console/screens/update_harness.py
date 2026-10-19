"""Update Harness: installed and available versions, the managed-file drift, the update mode and a
confirmed run of `harness update`. The command is the CLI one - the screen only shows what it will
do and asks for the same confirmation as the Harness section."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    RadioSet,
    ListItem,
    ListView,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from .. import brand, i18n
from .. import data as console_data
from .. import stats as console_stats
from ..catalog import (
    CONFIRMATION_REASONS,
    HARNESS_COMMANDS,
    CatalogEntry,
    process_argv,
    seed_update_entry,
)
from ..crumbs import Crumb
from ..shell import SHELL_CSS, ConsoleShell, Fold, SectionCard
from ..status import Status
from ..logtext import colorize_diff, colorize_log
from ..runner import CommandRunner, capturing_runner
from ..widgets import CheckList, ReasonNote, RoundRadioButton
from .commands import call_from_worker, render_result, short_repo
from .harness import CommandFormScreen
from .home_exit import closes_into_home

SEED_MODE = "update-seed-selected"
# (key, radio label): the three ways `harness update` can run. The last one overwrites only the
# seed files ticked in the list, so there is no catalog entry for it until they are known.
MODES = (
    ("update", "Обычное"),
    ("update-force-managed", "С перезаписью управляемых файлов"),
    (SEED_MODE, "С перезаписью выбранных seed-файлов"),
)
# What the selected mode does, shown under the options: (text, status colour or None).
_MODE_CONSEQUENCES = {
    "update": ("Локальные правки сохраняются. Остановится на конфликтах.", None),
    "update-force-managed": (
        "⚠ Заменит и конфликтные файлы версией пакета: ваши правки в них пропадут.",
        "warn",
    ),
    SEED_MODE: ("Только отмеченные seed-файлы; остальное не меняется.", None),
}
# Drift counters of `harness diff --json`: (field, what the update does, the narrow text, colour).
_DRIFT_ROWS = (
    ("package_changed", "обновятся", "обновятся", "ok"),
    ("added", "добавятся", "добавятся", "ok"),
    ("missing", "будут восстановлены", "будут восстановлены", "warn"),
    ("retired", "будут убраны", "будут убраны", "warn"),
    (
        "local_changed",
        "с локальными правками (сохранятся)",
        "ваши правки сохранятся",
        "warn",
    ),
    ("conflict", "в конфликте (нужна перезапись)", "конфликт", "fail"),
)
# Files the diff block lists: (payload field, tag, what happens to the file, status colour).
_DIFF_KINDS = (
    (
        "conflict",
        "конфликт",
        "Изменён и у вас, и в пакете: обычное обновление остановится, перезапись заменит файл версией пакета.",
        "fail",
    ),
    ("package_changed", "обновится", "Файл будет заменён версией из пакета.", "ok"),
    ("added", "добавится", "Новый файл пакета: будет добавлен.", "ok"),
    ("missing", "восстановится", "Файла нет на диске: он будет восстановлен.", "warn"),
    ("retired", "удалится", "Файла больше нет в пакете: он будет удалён.", "warn"),
    (
        "local_changed",
        "ваши правки",
        "Ваши локальные правки: обычное обновление их сохранит.",
        "warn",
    ),
    (
        "seed_differing",
        "seed",
        "Seed-файл отличается от шаблона: обновление его не трогает, пока не выбрана перезапись.",
        "warn",
    ),
)
_LOADING = "проверяется…"
_BUSY_REASON = "обновление выполняется"
_RESULT_STATUS = {
    "empty": Status("not_started", "пока пусто", "empty so far"),
    "running": Status("running", "выполняется", "running"),
    "ok": Status("accepted", "Harness обновлён", "Harness updated"),
    "error": Status("error", "ошибка", "error"),
}


def _count(value: object) -> int:
    """A drift field is a list of paths in `harness diff --json`; a bare number is also accepted."""
    if isinstance(value, list):
        return len(value)
    return value if isinstance(value, int) else 0


def _plural(
    language: str, count: int, ru: tuple[str, str, str], en: tuple[str, str]
) -> str:
    """`count` with its noun: Russian one/few/many forms, English singular/plural."""
    if language == "en":
        return f"{count} {en[0] if count == 1 else en[1]}"
    last, tens = count % 10, count % 100
    if last == 1 and tens != 11:
        form = ru[0]
    elif 2 <= last <= 4 and not 12 <= tens <= 14:
        form = ru[1]
    else:
        form = ru[2]
    return f"{count} {form}"


def _conflicts(count: int, language: str) -> str:
    return _plural(
        language,
        count,
        ("конфликт", "конфликта", "конфликтов"),
        ("conflict", "conflicts"),
    )


def _files(count: int, language: str) -> str:
    return _plural(language, count, ("файл", "файла", "файлов"), ("file", "files"))


def render_versions(
    state: console_stats.HarnessState,
    package_version: str,
    language: str = "ru",
    *,
    narrow: bool = False,
) -> Text:
    """Installed against available version with the update verdict. A narrow terminal keeps the
    versions and the set; the verdict is then the card's note (see `version_verdict`)."""
    colors = brand.STATUS_COLORS
    muted = brand.PALETTE["muted"]

    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    if not state.installed:
        text = Text()
        text.append(
            f"{brand.STATUS_ICONS['fail']} "
            + t("Харнесс не установлен: нет читаемого .harness/harness.lock"),
            style=colors["fail"],
        )
        text.append("\n" + t("Установите его через «Открыть проект»."), style=muted)
        return text
    text = Text()
    text.append(t("Установлено") + "  ", style=muted)
    text.append(f"{state.installed_version}\n", style="bold")
    text.append(t("Доступно") + "  ", style=muted)
    text.append(f"{package_version}", style="bold")
    if not narrow:
        current = state.installed_version == package_version
        icon, color, verdict = (
            (brand.STATUS_ICONS["ok"], colors["ok"], "Версия актуальна")
            if current
            else (brand.STATUS_ICONS["warn"], colors["warn"], "Доступно обновление")
        )
        text.append(f"\n{icon} {t(verdict)}", style=color)
    rows = [("Состав", ", ".join(state.capabilities) or t("нет"))]
    if not narrow:
        rows += [
            ("Скилов", str(state.skills)),
            ("Управляемых файлов", str(state.managed_files)),
            ("Lock обновлён", state.updated_at),
        ]
    for label, value in rows:
        label = t(label)
        text.append("\n" + label + " " * max(2, 20 - cell_len(label)), style=muted)
        text.append(value)
    return text


def version_verdict(
    state: console_stats.HarnessState | None, package_version: str
) -> str:
    """The narrow versions card's note: the verdict as a symbol and a word (Russian source)."""
    if state is None or not state.installed:
        return ""
    if state.installed_version == package_version:
        return f"{brand.STATUS_ICONS['ok']} актуальна"
    return f"{brand.STATUS_ICONS['warn']} доступно обновление"


def render_drift(
    payload: dict[str, object] | None,
    *,
    loading: bool = False,
    language: str = "ru",
    narrow: bool = False,
) -> Text:
    """What an update would touch, from the `harness diff --json` counters."""
    colors = brand.STATUS_COLORS
    muted = brand.PALETTE["muted"]

    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    if loading:
        text = Text(f"{brand.STATUS_ICONS['skipped']} {t(_LOADING)}", style=muted)
        text.append("\n" + t("Счётчики и список файлов появятся после проверки."))
        return text
    if payload is None:
        text = Text(
            f"{brand.STATUS_ICONS['fail']} {t('Не удалось проверить дрейф')}",
            style=colors["fail"],
        )
        text.append(
            "\n"
            + t("Нажмите «Проверить заново». Если повторяется — «Диагностика» (3)."),
            style=muted,
        )
        return text
    text = Text()
    rows = [
        (wide if not narrow else short, _count(payload.get(field)), status)
        for field, wide, short, status in _DRIFT_ROWS
        if _count(payload.get(field))
    ]
    if not rows:
        if payload.get("state") == "clean":
            return Text(
                f"{brand.STATUS_ICONS['ok']} {t('Расхождений нет')}", style=colors["ok"]
            )
        return Text(t(f"Состояние: {payload.get('state', 'неизвестно')}"))
    for index, (label, count, status) in enumerate(rows):
        if index:
            text.append("\n")
        text.append(
            f"{count:>4} {brand.STATUS_ICONS[status]} ", style=f"bold {colors[status]}"
        )
        text.append(t(label))
    return text


class UpdateHarnessScreen(ConsoleShell):
    """Update Harness on the shared console frame. Esc, the menu and the breadcrumbs wait for a
    running update; Esc and every way to Home close the screen with `dismiss(None)`, so the home
    refresh runs."""

    SECTION = "Update Harness"
    CRUMBS = "Главная › Обновить Harness"
    NARROW_FOOTER_KEYS = ("escape", "f2")
    AUTO_FOCUS = "#update-run"
    CSS = SHELL_CSS + brand.shell_css("""
    UpdateHarnessScreen #update-page { width: 100%; height: 1fr; padding: 0 2; overflow-x: hidden; }
    UpdateHarnessScreen #update-title { height: 1; text-style: bold; }
    UpdateHarnessScreen #update-subtitle { color: $text-muted; margin-bottom: 1; }
    UpdateHarnessScreen #update-columns { width: 100%; height: auto; layout: vertical; }
    UpdateHarnessScreen.-spacious #update-columns { layout: horizontal; }
    UpdateHarnessScreen #update-left, UpdateHarnessScreen #update-right { width: 1fr; height: auto; }
    UpdateHarnessScreen.-spacious #update-left { margin-right: 1; }
    UpdateHarnessScreen .section-card { margin-bottom: 1; }
    UpdateHarnessScreen Static { height: auto; }
    UpdateHarnessScreen .actions { height: auto; }
    UpdateHarnessScreen .reason-note { height: auto; color: $text-muted; }
    UpdateHarnessScreen #update-plan { text-style: bold; color: $secondary; }
    UpdateHarnessScreen #update-warning { margin-top: 1; border: round $warning; padding: 0 1; color: $warning; }
    UpdateHarnessScreen #update-confirm, UpdateHarnessScreen #seed-note { margin-top: 1; color: $text-muted; }
    UpdateHarnessScreen #seed-frame { display: none; }
    UpdateHarnessScreen #mode-caption { margin-top: 1; color: $text-muted; }
    UpdateHarnessScreen #mode-caption.-warn { color: $warning; }
    UpdateHarnessScreen #mode-reason { color: $text-muted; }
    UpdateHarnessScreen RadioSet, UpdateHarnessScreen RadioSet:focus {
        height: auto; border: none; padding: 0; background: transparent;
    }
    UpdateHarnessScreen RadioSet > RadioButton {
        height: 1; border: none; padding: 0; background: transparent;
    }
    UpdateHarnessScreen RadioSet > RadioButton.-selected { background: transparent; }
    UpdateHarnessScreen RadioSet:focus > RadioButton.-selected {
        color: $primary; background: transparent;
    }
    UpdateHarnessScreen RadioSet:focus > RadioButton.-selected > .toggle--label {
        color: $primary; text-style: underline; background: transparent;
    }
    UpdateHarnessScreen #diff-body { layout: vertical; height: 26; }
    UpdateHarnessScreen #diff-files { width: 100%; height: 9; }
    UpdateHarnessScreen #diff-files > ListItem { height: 1; padding: 0 1; }
    UpdateHarnessScreen #diff-files Static {
        height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    UpdateHarnessScreen #diff-view-scroll {
        width: 100%; height: 1fr; border-top: solid $border-subtle;
    }
    UpdateHarnessScreen #diff-why { width: 100%; }
    UpdateHarnessScreen #diff-view { width: auto; min-width: 100%; }
    UpdateHarnessScreen SelectionList { height: auto; max-height: 12; }
    UpdateHarnessScreen #update-output-scroll { height: auto; max-height: 18; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        load_drift: Callable[
            [Path], dict[str, object] | None
        ] = console_data.drift_report,
        load_state: Callable[
            [Path], console_stats.HarnessState
        ] = console_stats.harness_state,
        load_diff: Callable[[Path, str], str] = console_data.update_diff_text,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._runner = command_runner
        self._load_drift = load_drift
        self._load_state = load_state
        self._load_diff = load_diff
        self._diff_kinds: dict[str, str] = {}
        self._diff_for = ""
        self._entries = {entry.key: entry for entry in HARNESS_COMMANDS}
        self.busy = False
        self.installed = False
        self._state: console_stats.HarnessState | None = None
        self._payload: dict[str, object] | None = None
        self._payload_ready = False
        self._narrow = False
        self._result = "empty"
        self._seed_count = 0
        self._recheck = Button(
            "Проверить заново", id="update-recheck", classes="wide-only"
        )
        self._recheck_narrow = Button(
            "Проверить заново", id="update-recheck-narrow", classes="narrow-only"
        )
        self._run_button = Button("Обновить", id="update-run", disabled=True)
        self._seed_all = Button("Выбрать все", id="seed-all")
        self._seed_none = Button("Снять выбор", id="seed-none")

    def compose_strip(self) -> ComposeResult:
        yield Static("Проект: " + self.repo.name, id="update-project", markup=False)
        yield Static(
            "Команда обновления — та же, что в CLI: harness update",
            classes="wide-only",
        )

    def compose_content(self) -> ComposeResult:
        narrow = self.app.size.width < brand.NARROW_WIDTH
        with VerticalScroll(id="update-page"):
            yield Static("Обновить Harness", id="update-title")
            yield Static(
                "Сначала показываем, что изменится в управляемых файлах. "
                "Запуск — только после вашего подтверждения.",
                id="update-subtitle",
                classes="wide-only",
            )
            with Horizontal(id="update-columns"):
                with Vertical(id="update-left"):
                    with SectionCard(title="Версии", id="versions-frame"):
                        yield Static("", id="update-versions", markup=False)
                    with SectionCard(
                        title="Что изменится в управляемых файлах",
                        note="из harness diff",
                        id="drift-frame",
                    ):
                        yield Static("", id="update-drift", markup=False)
                        with Horizontal(classes="actions"):
                            yield self._recheck
                        yield ReasonNote(self._drift_reasons, id="drift-reason")
                with Vertical(id="update-right"):
                    with Fold(
                        title="Режим обновления",
                        summary=MODES[0][1],
                        collapsed=narrow,
                        id="mode-frame",
                    ):
                        with RadioSet(id="update-mode"):
                            for key, label in MODES:
                                yield RoundRadioButton(label, value=key == MODES[0][0])
                        yield Static("", id="mode-caption", markup=False)
                        yield Static("", id="mode-reason", markup=False)
                    with SectionCard(title="Seed-файлы", id="seed-frame"):
                        yield Static("", id="seed-note", markup=False)
                        yield CheckList(id="seed-files")
                        with Horizontal(classes="actions"):
                            yield self._seed_all
                            yield self._seed_none
                        yield ReasonNote(self._seed_reasons, id="seed-reason")
                    with SectionCard(title="План", id="plan-frame"):
                        yield Static("", id="update-plan", markup=False)
                        yield Static("", id="update-warning", markup=False)
                        yield Static("", id="update-confirm", markup=False)
                        with Horizontal(classes="actions"):
                            yield self._run_button
                            yield self._recheck_narrow
                        yield ReasonNote(self._plan_reasons, id="plan-reason")
            with Fold(
                title="Diff — что изменится в файлах",
                collapsed=narrow,
                id="diff-frame",
            ):
                with Horizontal(id="diff-body"):
                    yield ListView(id="diff-files")
                    with VerticalScroll(id="diff-view-scroll"):
                        yield Static("", id="diff-why", markup=False)
                        yield Static(
                            "Выберите файл в списке слева.",
                            id="diff-view",
                            markup=False,
                        )
            with Fold(
                title="Результат",
                summary=_RESULT_STATUS["empty"].render("ru"),
                collapsed=narrow,
                id="output-frame",
            ):
                with VerticalScroll(id="update-output-scroll"):
                    yield Static(
                        "Результат обновления появится здесь.",
                        id="update-output",
                        markup=False,
                    )

    def on_mount(self) -> None:
        self._narrow = self._is_narrow()
        self._reload()

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < brand.NARROW_WIDTH
        if narrow == self._narrow or self._state is None:
            return
        self._narrow = narrow
        for fold in self.query(Fold):
            fold.collapsed = narrow
        self._render_state()
        self._show_plan()

    def _is_narrow(self) -> bool:
        return self.app.size.width < brand.NARROW_WIDTH

    def _mode(self) -> str:
        return MODES[max(0, self.query_one("#update-mode", RadioSet).pressed_index)][0]

    def _selected_seeds(self) -> list[str]:
        return list(self.query_one("#seed-files", SelectionList).selected)

    def _entry(self) -> CatalogEntry:
        mode = self._mode()
        if mode == SEED_MODE:
            return seed_update_entry(self._selected_seeds())
        return self._entries[mode]

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _t(self, text: str) -> str:
        return i18n.tr_text(self._language(), text)

    def _render_state(self) -> None:
        """The version and drift cards from the stored state, in the current language."""
        state = self._state
        if state is None:
            return
        language = self._language()
        package_version = console_data.harness_version()
        self.query_one("#update-versions", Static).update(
            render_versions(state, package_version, language, narrow=self._narrow)
        )
        self.query_one("#versions-frame", SectionCard).set_note(
            version_verdict(state, package_version) if self._narrow else ""
        )
        if not state.installed:
            self.query_one("#update-drift", Static).update(
                Text(
                    f"{brand.STATUS_ICONS['skipped']} "
                    + self._t("Нет установки — сравнивать нечего."),
                    style=brand.PALETTE["muted"],
                )
            )
        else:
            self.query_one("#update-drift", Static).update(
                render_drift(
                    self._payload,
                    loading=not self._payload_ready,
                    language=language,
                    narrow=self._narrow,
                )
            )

    def sync_language(self) -> None:
        """Re-render what the walker cannot reach: styled text built from the stored state."""
        super().sync_language()
        if self._state is None:
            return
        self._render_state()
        self._fill_diff_files(self._payload)
        self._show_seed_note()
        self._show_plan()

    def _reload(self) -> None:
        state = self._load_state(self.repo)
        self.installed = state.installed
        self._state = state
        self._payload = None
        self._payload_ready = False
        self._render_state()
        self._show_plan()
        if not state.installed:
            return

        def work() -> None:
            payload = self._load_drift(self.repo)
            call_from_worker(self, lambda: self._drift_loaded(payload))

        self.run_worker(work, thread=True, exclusive=True, group="update-drift")

    def _drift_loaded(self, payload: dict[str, object] | None) -> None:
        self._payload = payload
        self._payload_ready = True
        self._render_state()
        names = payload.get("seed_differing") if payload is not None else None
        files = (
            sorted(item for item in names if isinstance(item, str))
            if isinstance(names, list)
            else []
        )
        options = self.query_one("#seed-files", SelectionList)
        kept = set(options.selected)
        options.clear_options()
        options.add_options([Selection(name, name, name in kept) for name in files])
        self._seed_count = len(files)
        self._show_seed_note()
        self._fill_diff_files(payload)
        self._show_plan()

    def _show_seed_note(self) -> None:
        """The seed card: what ticking a file does (or that nothing differs) and `ticked of all`."""
        if self._seed_count:
            note = Text(
                f"{brand.STATUS_ICONS['warn']} "
                + self._t(
                    "Отмечены файлы, которые будут перезаписаны эталоном; их правки пропадут."
                ),
                style=brand.STATUS_COLORS["warn"],
            )
        else:
            note = Text(
                f"{brand.STATUS_ICONS['ok']} "
                + self._t("Seed-файлы совпадают с шаблоном — перезаписывать нечего."),
                style=brand.STATUS_COLORS["ok"],
            )
        self.query_one("#seed-note", Static).update(note)
        ticked = len(self._selected_seeds())
        self.query_one("#seed-frame", SectionCard).set_note(
            f"{ticked} of {self._seed_count} ticked"
            if self._language() == "en"
            else f"отмечено {ticked} из {self._seed_count}"
        )

    def _fill_diff_files(self, payload: dict[str, object] | None) -> None:
        listing = self.query_one("#diff-files", ListView)
        listing.clear()
        self._diff_kinds = {}
        seen: set[str] = set()
        items: list[ListItem] = []
        for field, tag, _meaning, status in _DIFF_KINDS:
            paths = payload.get(field) if payload is not None else None
            for relative in (
                sorted(item for item in paths if isinstance(item, str))
                if isinstance(paths, list)
                else []
            ):
                if relative in seen:
                    continue
                seen.add(relative)
                self._diff_kinds[relative] = field
                label = Text()
                label.append(
                    f"{brand.STATUS_ICONS[status]} {self._t(tag):<14} ",
                    style=brand.STATUS_COLORS[status],
                )
                label.append(relative)
                items.append(ListItem(Static(label), name=relative))
        if items:
            listing.extend(items)
            listing.index = 0
        else:
            self.query_one("#diff-view", Static).update(
                Text(
                    self._t("Расхождений нет: показывать нечего."),
                    style=brand.PALETTE["muted"],
                )
            )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if (
            event.list_view.id != "diff-files"
            or event.item is None
            or not event.item.name
        ):
            return
        relative = event.item.name
        self._diff_for = relative
        self.query_one("#diff-why", Static).update("")
        self.query_one("#diff-view", Static).update(
            Text(self._t("загружается…"), style=brand.PALETTE["muted"])
        )

        def work() -> None:
            try:
                text = self._load_diff(self.repo, relative)
            except Exception as exc:
                text = f"diff недоступен: {exc!r}"
            call_from_worker(self, lambda: self._diff_loaded(relative, text))

        self.run_worker(work, thread=True, exclusive=True, group="update-diff")

    def _diff_loaded(self, relative: str, text: str) -> None:
        if relative != self._diff_for:
            return
        meaning = next(
            (
                m
                for field, _tag, m, _status in _DIFF_KINDS
                if field == self._diff_kinds.get(relative)
            ),
            "",
        )
        self.query_one("#diff-why", Static).update(
            Text(self._t(meaning) + "\n", style=brand.PALETTE["muted"])
        )
        if self._language() == "en":
            text = text.replace("--- проект/", "--- project/", 1).replace(
                "+++ после обновления/", "+++ after update/", 1
            )
        body = (
            colorize_diff(text)
            if text
            else Text(
                self._t("Файл уже совпадает с тем, что запишет обновление."),
                style=brand.STATUS_COLORS["ok"],
            )
        )
        self.query_one("#diff-view", Static).update(body)

    def _stop_warning(self) -> Text | None:
        """A normal update stops on conflicts: said in the plan from the counter already loaded."""
        conflicts = _count(self._payload.get("conflict")) if self._payload else 0
        if not conflicts or self._mode() != MODES[0][0]:
            return None
        language = self._language()
        lead = "Остановится:" if self._narrow else "Обычное обновление остановится:"
        hint = (
            "Смените режим или разберите файл."
            if self._narrow
            else "Выберите режим с перезаписью или разберите файл вручную (см. diff ниже)."
        )
        text = Text(
            f"{brand.STATUS_ICONS['warn']} {i18n.tr(language, lead)} "
            + _conflicts(conflicts, language),
            style=brand.STATUS_COLORS["warn"],
        )
        text.append("\n" + i18n.tr(language, hint), style=brand.PALETTE["muted"])
        return text

    def _diff_summary(self) -> str:
        """The diff fold's summary: how many files, and on a narrow terminal how many conflicts."""
        if not self._payload_ready or self._payload is None:
            return ""
        language = self._language()
        files = _files(len(self._diff_kinds), language)
        if not self._narrow:
            return (
                "↑↓ select a file · " if language == "en" else "↑↓ выбор файла · "
            ) + files
        conflicts = _count(self._payload.get("conflict"))
        return f"{files} · {_conflicts(conflicts, language)}" if conflicts else files

    def _mode_name(self) -> str:
        return next(label for key, label in MODES if key == self._mode())

    def _show_plan(self) -> None:
        entry = self._entry()
        line = short_repo(entry.cli_line(self.repo), self.repo)
        self.query_one("#update-plan", Static).update(f"$ {line}")
        self.query_one("#update-confirm", Static).update(
            Text(
                self._t(
                    CONFIRMATION_REASONS[entry.reversibility]
                    + "\nПеред запуском будет запрошено подтверждение."
                )
            )
        )
        warning = self._stop_warning()
        stop = self.query_one("#update-warning", Static)
        stop.display = warning is not None
        if warning is not None:
            stop.update(warning)
        caption, colour = _MODE_CONSEQUENCES[self._mode()]
        mode_caption = self.query_one("#mode-caption", Static)
        mode_caption.set_class(colour == "warn", "-warn")
        mode_caption.update(Text(self._t(caption)))
        seed_mode = self._mode() == SEED_MODE
        self.query_one("#seed-frame").display = seed_mode
        needs_files = seed_mode and not self._selected_seeds()
        self._run_button.disabled = self.busy or not self.installed or needs_files
        self.query_one("#plan-frame", SectionCard).set_note(
            self._t(self._mode_name()) if self._narrow else ""
        )
        self._show_seed_note()
        self._show_summaries()
        self._show_reasons()

    def _show_summaries(self) -> None:
        self.query_one("#mode-frame", Fold).set_summary(self._mode_name())
        self.query_one("#diff-frame", Fold).set_summary(self._diff_summary())
        self.query_one("#output-frame", Fold).set_summary(
            _RESULT_STATUS[self._result].render(self._language())
        )

    def _plan_reasons(self) -> list[tuple[Button, str]]:
        reasons = []
        if self.busy:
            reasons.append((self._run_button, _BUSY_REASON))
            if self._narrow:
                reasons.append((self._recheck_narrow, _BUSY_REASON))
        elif not self.installed:
            reasons.append((self._run_button, "Harness не установлен"))
        elif self._mode() == SEED_MODE and not self._selected_seeds():
            reasons.append((self._run_button, "Отметьте хотя бы один файл"))
        return reasons

    def _drift_reasons(self) -> list[tuple[Button, str]]:
        if self.busy and not self._narrow:
            return [(self._recheck, _BUSY_REASON)]
        return []

    def _seed_reasons(self) -> list[tuple[Button, str]]:
        if self.busy:
            return [(self._seed_all, _BUSY_REASON), (self._seed_none, _BUSY_REASON)]
        return []

    def _show_reasons(self) -> None:
        for note in self.query(ReasonNote):
            note.relabel()
        reason = self.query_one("#mode-reason", Static)
        reason.display = self.busy
        reason.update(
            f"«{self._t('Режим обновления')}»: {self._t(_BUSY_REASON)}"
            if self.busy
            else ""
        )

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._show_plan()

    def on_selection_list_selected_changed(self, event: Message) -> None:
        self._show_plan()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        if event.button.id in ("update-recheck", "update-recheck-narrow"):
            self._reload()
        elif event.button.id in ("seed-all", "seed-none"):
            options = self.query_one("#seed-files", SelectionList)
            if event.button.id == "seed-all":
                options.select_all()
            else:
                options.deselect_all()
        elif event.button.id == "update-run":
            entry = self._entry()

            def confirmed(values: "dict[str, str] | None") -> None:
                if values is not None:
                    self._run(entry)

            self.app.push_screen(CommandFormScreen(entry, self.repo), confirmed)

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for identifier in (
            "update-recheck",
            "update-recheck-narrow",
            "update-mode",
            "seed-files",
            "seed-all",
            "seed-none",
        ):
            self.query_one("#" + identifier).disabled = value
        self._show_plan()

    def _run(self, entry: CatalogEntry) -> None:
        self._result = "running"
        self._set_busy(True)
        line = entry.cli_line(self.repo)
        self.query_one("#update-output", Static).update(
            colorize_log(f"$ {line}\n{_RESULT_STATUS['running'].symbol} Выполняется…")
        )
        argv = process_argv(entry.cli_argv(self.repo), self.repo)

        def work() -> None:
            try:
                result = self._runner(argv, cwd=self.repo)
                text, success = render_result(line, result), result.returncode == 0
            except OSError as exc:
                text, success = f"$ {line}\nне удалось запустить: {exc}", False
            except Exception as exc:
                text, success = f"$ {line}\nсбой запуска: {exc!r}", False
            call_from_worker(self, lambda: self._finished(text, success))

        self.run_worker(work, thread=True, exclusive=True, group="update-run")

    def _finished(self, text: str, success: bool) -> None:
        self._result = "ok" if success else "error"
        self._set_busy(False)
        self.query_one("#update-output", Static).update(colorize_log(text))
        if success:
            self.notify("Harness обновлён")
        else:
            self.notify("Обновление завершилось с ошибкой", severity="error")
        self._reload()

    # Esc, a number key, the menu and the breadcrumbs wait for a running update. Esc and every way
    # to Home close the screen with `dismiss(None)`, which runs the push callback that refreshes
    # Home (the shell's `pop_screen` would drop it); another section opens on top of this screen.
    def _waits_for_update(self) -> bool:
        if self.busy:
            self.notify(
                i18n.tr(self._language(), "Дождитесь окончания обновления"),
                severity="warning",
            )
        return self.busy

    async def action_back(self) -> None:
        if not self._waits_for_update():
            self.dismiss(None)

    async def open_section(self, index: int) -> None:
        if self._waits_for_update():
            return
        if closes_into_home(self, index):
            self.dismiss(None)
        else:
            await super().open_section(index)

    async def navigate_crumb(self, crumb: Crumb) -> None:
        if not self._waits_for_update():
            await super().navigate_crumb(crumb)
