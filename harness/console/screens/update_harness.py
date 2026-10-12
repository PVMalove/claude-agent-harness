"""Update Harness: installed and available versions, the managed-file drift, the update mode and a
confirmed run of `harness update`. The command is the CLI one - the screen only shows what it will
do and asks for the same confirmation as the Harness section."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    RadioButton,
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
from ..chrome import TopBar
from ..translate import Translated
from ..logtext import colorize_diff, colorize_log
from ..runner import CommandRunner, capturing_runner
from ..widgets import CheckList
from .commands import call_from_worker, render_result, short_repo
from .harness import CommandFormScreen

SEED_MODE = "update-seed-selected"
# (key, radio label): the three ways `harness update` can run. The last one overwrites only the
# seed files ticked in the list, so there is no catalog entry for it until they are known.
MODES = (
    ("update", "Обычное — локальные правки сохраняются"),
    ("update-force-managed", "С перезаписью управляемых файлов"),
    (SEED_MODE, "С перезаписью выбранных seed-файлов"),
)
# Drift counters of `harness diff --json`: (field, what the update does, status colour).
_DRIFT_ROWS = (
    ("package_changed", "обновятся", "ok"),
    ("added", "добавятся", "ok"),
    ("missing", "будут восстановлены", "warn"),
    ("retired", "будут убраны", "warn"),
    ("local_changed", "с локальными правками (сохранятся)", "warn"),
    ("conflict", "в конфликте (нужна перезапись)", "fail"),
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
        "skipped",
    ),
    (
        "seed_differing",
        "seed",
        "Seed-файл отличается от шаблона: обновление его не трогает, пока не выбрана перезапись.",
        "warn",
    ),
)
_LOADING = "проверяется…"


def _count(value: object) -> int:
    """A drift field is a list of paths in `harness diff --json`; a bare number is also accepted."""
    if isinstance(value, list):
        return len(value)
    return value if isinstance(value, int) else 0


def render_versions(
    state: console_stats.HarnessState, package_version: str, language: str = "ru"
) -> Text:
    """Installed against available version with the update verdict."""
    colors = brand.STATUS_COLORS
    muted = brand.PALETTE["muted"]

    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    if not state.installed:
        return Text(
            t(
                "Харнесс не установлен: нет читаемого .harness/harness.lock.\n"
                "Установите его через «Открыть проект»."
            ),
            style=colors["fail"],
        )
    current = state.installed_version == package_version
    text = Text()
    text.append(t("Установлено") + "  ", style=muted)
    text.append(f"{state.installed_version}\n", style="bold")
    text.append(t("Доступно") + "  ", style=muted)
    text.append(f"{package_version}\n", style="bold")
    if current:
        text.append(
            f"{brand.STATUS_ICONS['ok']} {t('Версия актуальна')}", style=colors["ok"]
        )
    else:
        text.append(
            f"{brand.STATUS_ICONS['warn']} {t('Доступно обновление')}",
            style=colors["warn"],
        )
    text.append(
        t(
            f"\nCapability: {', '.join(state.capabilities) or 'нет'}"
            f"\nСкилов: {state.skills}, управляемых файлов: {state.managed_files}"
            f"\nLock обновлён: {state.updated_at}"
        ),
        style=muted,
    )
    return text


def render_drift(
    payload: dict[str, object] | None, *, loading: bool = False, language: str = "ru"
) -> Text:
    """What an update would touch, from the `harness diff --json` counters."""
    colors = brand.STATUS_COLORS

    def t(text: str) -> str:
        return i18n.tr_text(language, text)

    if loading:
        return Text(t(_LOADING), style=brand.PALETTE["muted"])
    if payload is None:
        return Text(t("Не удалось проверить дрейф."), style=colors["fail"])
    text = Text()
    rows = [
        (label, _count(payload.get(field)), status)
        for field, label, status in _DRIFT_ROWS
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
        text.append(f"{count:>4}  ", style=f"bold {colors[status]}")
        text.append(t(label))
    return text


class UpdateHarnessScreen(Translated, Screen[None]):
    BINDINGS = [Binding("escape", "cancel", "Назад")]
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (120, "-wide")]
    DEFAULT_CSS = """
    UpdateHarnessScreen #update-scroll { width: 100%; height: 1fr; overflow-x: hidden; }
    UpdateHarnessScreen #update-columns { width: 100%; height: auto; layout: vertical; }
    UpdateHarnessScreen.-wide #update-columns { layout: horizontal; }
    UpdateHarnessScreen #update-left, UpdateHarnessScreen #update-right {
        width: 1fr; height: auto;
    }
    UpdateHarnessScreen .frame { height: auto; }
    UpdateHarnessScreen Vertical.frame { overflow: hidden hidden; }
    UpdateHarnessScreen Static { height: auto; }
    UpdateHarnessScreen .actions { height: auto; }
    UpdateHarnessScreen RadioSet { height: auto; }
    UpdateHarnessScreen #seed-frame { display: none; }
    UpdateHarnessScreen #diff-body { layout: vertical; height: 26; }
    UpdateHarnessScreen #diff-files { width: 100%; height: 9; }
    UpdateHarnessScreen #diff-files > ListItem { height: 1; padding: 0 1; }
    UpdateHarnessScreen #diff-files Static {
        height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    UpdateHarnessScreen #diff-view-scroll {
        width: 100%; height: 1fr; border-top: solid $panel-lighten-2;
    }
    UpdateHarnessScreen #diff-view { width: auto; min-width: 100%; }
    UpdateHarnessScreen SelectionList { height: auto; max-height: 12; }
    UpdateHarnessScreen #update-output-scroll { height: auto; max-height: 18; }
    """

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

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Обновление Harness")
        with VerticalScroll(id="update-scroll"):
            with Horizontal(id="update-columns"):
                with Vertical(id="update-left"):
                    with Vertical(id="versions-frame", classes="frame") as versions:
                        versions.border_title = brand.frame_title("Версии")
                        yield Static("", id="update-versions", markup=False)
                    with Vertical(id="drift-frame", classes="frame") as drift:
                        drift.border_title = brand.frame_title(
                            "Что изменится в управляемых файлах"
                        )
                        yield Static("", id="update-drift", markup=False)
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Проверить заново", id="update-recheck", compact=True
                            )
                with Vertical(id="update-right"):
                    with Vertical(id="mode-frame", classes="frame") as mode:
                        mode.border_title = brand.frame_title("Режим обновления")
                        with RadioSet(id="update-mode"):
                            for key, label in MODES:
                                yield RadioButton(label, value=key == MODES[0][0])
                    with Vertical(id="seed-frame", classes="frame") as seed:
                        seed.border_title = brand.frame_title("Seed-файлы")
                        yield Static("", id="seed-note", markup=False)
                        yield CheckList(id="seed-files")
                        with Horizontal(classes="actions"):
                            yield Button("Выбрать все", id="seed-all", compact=True)
                            yield Button("Снять выбор", id="seed-none", compact=True)
                    with Vertical(id="plan-frame", classes="frame") as plan:
                        plan.border_title = brand.frame_title("План")
                        yield Static("", id="update-plan", markup=False)
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Обновить", id="update-run", disabled=True, compact=True
                            )
            with Vertical(id="diff-frame", classes="frame") as diff:
                diff.border_title = brand.frame_title("Diff — что изменится в файлах")
                with Horizontal(id="diff-body"):
                    yield ListView(id="diff-files")
                    with VerticalScroll(id="diff-view-scroll"):
                        yield Static(
                            "Выберите файл в списке слева.",
                            id="diff-view",
                            markup=False,
                        )
            with VerticalScroll(id="update-output-scroll", classes="frame") as output:
                output.border_title = brand.frame_title("Результат")
                yield Static(
                    "Результат обновления появится здесь.",
                    id="update-output",
                    markup=False,
                )
        yield Footer()

    def on_mount(self) -> None:
        # The file list sits inside the frame: no second border of its own, focused or not.
        self.query_one("#diff-files", ListView).styles.border = ("none", "black")
        self._reload()

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)

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
        """The version and drift frames from the stored state, in the current language."""
        state = self._state
        if state is None:
            return
        language = self._language()
        self.query_one("#update-versions", Static).update(
            render_versions(state, console_data.harness_version(), language)
        )
        if not state.installed:
            self.query_one("#update-drift", Static).update(
                Text(
                    self._t("Нет установки — сравнивать нечего."),
                    style=brand.PALETTE["muted"],
                )
            )
        else:
            self.query_one("#update-drift", Static).update(
                render_drift(
                    self._payload, loading=not self._payload_ready, language=language
                )
            )

    def sync_language(self) -> None:
        """Re-render what the walker cannot reach: styled text built from the stored state."""
        if self._state is None:
            return
        self._render_state()
        self._fill_diff_files(self._payload)
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
        self.query_one("#seed-note", Static).update(
            Text(
                self._t(
                    "Отмечены файлы, которые будут перезаписаны эталоном; их правки пропадут."
                    if files
                    else "Seed-файлы совпадают с шаблоном — перезаписывать нечего."
                ),
                style=brand.PALETTE["muted"],
            )
        )
        self._fill_diff_files(payload)
        self._show_plan()

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
                label.append(f"{self._t(tag):<14}", style=brand.STATUS_COLORS[status])
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
        header = Text(self._t(meaning) + "\n\n", style=brand.PALETTE["muted"])
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
        self.query_one("#diff-view", Static).update(Text.assemble(header, body))

    def _show_plan(self) -> None:
        entry = self._entry()
        line = short_repo(entry.cli_line(self.repo), self.repo)
        text = Text()
        text.append(f"$ {line}\n", style=f"bold {brand.PALETTE['secondary']}")
        text.append(
            self._t(
                CONFIRMATION_REASONS[entry.reversibility]
                + "\nПеред запуском будет запрошено подтверждение."
            ),
            style=brand.STATUS_COLORS["warn"],
        )
        self.query_one("#update-plan", Static).update(text)
        seed_mode = self._mode() == SEED_MODE
        self.query_one("#seed-frame").display = seed_mode
        needs_files = seed_mode and not self._selected_seeds()
        self.query_one("#update-run", Button).disabled = (
            self.busy or not self.installed or needs_files
        )

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._show_plan()

    def on_selection_list_selected_changed(self, event: Message) -> None:
        self._show_plan()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        if event.button.id == "update-recheck":
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
        for identifier in ("update-recheck", "update-mode", "seed-files"):
            self.query_one("#" + identifier).disabled = value
        self._show_plan()

    def _run(self, entry: CatalogEntry) -> None:
        self._set_busy(True)
        line = entry.cli_line(self.repo)
        self.query_one("#update-output", Static).update(
            colorize_log(f"$ {line}\nВыполняется…")
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
        self._set_busy(False)
        self.query_one("#update-output", Static).update(colorize_log(text))
        if success:
            self.notify("Harness обновлён")
        else:
            self.notify("Обновление завершилось с ошибкой", severity="error")
        self._reload()

    def action_cancel(self) -> None:
        if self.busy:
            self.notify("Дождитесь окончания обновления", severity="warning")
        else:
            self.dismiss(None)
