#!/usr/bin/env python3
"""Поведенческие hooks ChangeImpact (ADR 0024, раздел 2a; issue #802).

``change_impact_hooks.py <hook_id>`` читает JSON события runtime из stdin:

- ``impact-first-touch`` (PreToolUse, Write|Edit) предупреждает о касании рискованного пути, влияние
  которого ChangeImpact не вычисляет по графу (конфиг сборки и проверок, capabilities, skills);
- ``sensitive-path-recheck`` (PreToolUse, Write|Edit) предупреждает о правке чувствительного пути
  (манифест, CI, миграции, auth, права доступа, hooks);
- ``targeted-check-reminder`` на PreToolUse Write|Edit ведёт журнал касаний операции, а на Stop и
  SubagentStop напоминает отчитать targeted-проверки brief, если файлы менялись после отчёта.

Тело — advisory: оно всегда завершается кодом 0. Предупреждение — первая строка stderr
``[reason_code] remedy``; stdout содержит только JSON протокола runtime. Профиль применяет конверт
``hook_response.py`` до запуска тела. Hook не читает ledger, brief и отчёты coordinator-а, не запускает
тесты, make и git: только читает stdin и делает stat файлов.

Состояние операции лежит в ``<проект>/.harness/.sandboxes/hook-events/<operation_id>/``. Только
стандартная библиотека, Python 3.9+.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

sys.dont_write_bytecode = True

IMPACT_FIRST_TOUCH = "impact-first-touch"
TARGETED_CHECK_REMINDER = "targeted-check-reminder"
SENSITIVE_PATH_RECHECK = "sensitive-path-recheck"
REASON_RISKY = "risky-path-first-touch"
REASON_UNREPORTED = "targeted-checks-unreported"
REASON_SENSITIVE = "sensitive-path-touched"
REASON_INTERNAL_ERROR = "internal-error"

# Копия правил harness/repo_map/change_impact.py (path_unknown_code): hook не видит пакет harness.
# tests/core/test_change_impact_hooks.py сверяет копию с источником.
UNKNOWN_BUILD_CONFIG = "build-config-changed"
UNKNOWN_CAPABILITIES = "capabilities-or-skills-changed"
BUILD_CONFIG_NAMES = frozenset({"pyproject.toml", "uv.lock", "Makefile", "conftest.py"})
BUILD_CONFIG_GLOBS = (".github/*", "scripts/verify.py", "scripts/verification/*")
CAPABILITIES_GLOBS = ("harness/CAPABILITIES.json", "skills/*")

# Копия harness/orchestration/hook_policy.py (is_sensitive_path), ADR 0024, раздел 2.
SENSITIVE_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain",
        "rust-toolchain.toml",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
        "CODEOWNERS",
    }
)
SENSITIVE_NAME_GLOBS = ("requirements*.txt", "constraints*.txt")
SENSITIVE_PATH_GLOBS = (
    ".github/*",
    ".claude/settings*.json",
    ".claude/hooks/*",
    ".claude/agents/*",
    ".codex/hooks.json",
    ".codex/hooks/*",
    ".codex/config.toml",
    ".harness/project.json",
)
SENSITIVE_TOKENS = frozenset(
    {
        "migration",
        "migrations",
        "migrate",
        "alembic",
        "auth",
        "authn",
        "authz",
        "oauth",
        "oauth2",
        "authentication",
        "authorization",
        "permission",
        "permissions",
        "acl",
        "rbac",
    }
)
TOKEN_SEPARATORS = re.compile(r"[._-]")

STORE_PARTS = (".harness", ".sandboxes", "hook-events")
# Границы хранилища: каталогов операций и записей в одной операции.
MAX_OPERATIONS = 64
MAX_RECORDS = 2048
# Отчёт роли — запись в report_staging_path; задачи и состояние harness — не изменения кода.
REPORT_GLOB = ".harness/.sandboxes/scratch/inbox/*.json"
UNWATCHED_PREFIXES = (".harness/", "docs/tasks/")
STOP_EVENTS = ("Stop", "SubagentStop")

Output = Tuple[Optional[Dict[str, object]], List[str]]


class HookInputError(Exception):
    """Событие нельзя прочитать: не JSON-объект, нет ``session_id`` или ``file_path``."""


class StoreFull(Exception):
    """Операция достигла ``MAX_RECORDS``: новые записи не создаются."""


class Event(NamedTuple):
    """Событие runtime и идентичность операции (прогона агента), к которой оно относится."""

    name: str
    session_id: str
    agent_id: str
    operation_id: str
    payload: Dict[str, object]


class Target(NamedTuple):
    """Путь правки: корень git-checkout, абсолютный путь после realpath и путь от корня (posix)."""

    root: str
    real: str
    rel: str


def project_root() -> Path:
    """Корень проекта по расположению модуля: ``<проект>/.claude/hooks/change_impact_hooks.py``."""
    return Path(__file__).resolve().parents[2]


def _fold(text: str) -> str:
    """Сравнение путей без учёта регистра на Windows, с учётом — на остальных системах."""
    return text.casefold() if os.name == "nt" else text


def _matches(path: str, patterns: Tuple[str, ...]) -> bool:
    return any(fnmatchcase(_fold(path), _fold(pattern)) for pattern in patterns)


def risky_code(rel: str) -> Optional[str]:
    """Код ChangeImpact ``unknown`` рискованного пути или ``None`` (копия ``path_unknown_code``)."""
    name = PurePosixPath(rel).name
    if _fold(name) in {_fold(item) for item in BUILD_CONFIG_NAMES} or _matches(
        rel, BUILD_CONFIG_GLOBS
    ):
        return UNKNOWN_BUILD_CONFIG
    if _matches(rel, CAPABILITIES_GLOBS):
        return UNKNOWN_CAPABILITIES
    return None


def is_sensitive(rel: str) -> bool:
    """Чувствительный ли путь (копия ``hook_policy.is_sensitive_path``)."""
    parts = rel.split("/")
    name = parts[-1]
    if _fold(name) in {_fold(item) for item in SENSITIVE_NAMES} or _matches(
        name, SENSITIVE_NAME_GLOBS
    ):
        return True
    if _matches(rel, SENSITIVE_PATH_GLOBS):
        return True
    pieces = [*parts[:-1], PurePosixPath(name).stem]
    return any(
        token.lower() in SENSITIVE_TOKENS
        for piece in pieces
        for token in TOKEN_SEPARATORS.split(piece)
    )


def _sha256(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operation_id(session_id: str, agent_id: str) -> str:
    """Идентичность прогона агента: сессия runtime и агент внутри неё (пусто — главный агент)."""
    return _sha256(["hook-operation", session_id, agent_id])[:32]


def read_event(text: str) -> Event:
    """Разобрать JSON события; нечитаемое событие — ``HookInputError``."""
    try:
        payload: object = json.loads(text)
    except ValueError as exc:
        raise HookInputError("stdin не является JSON") from exc
    if not isinstance(payload, dict):
        raise HookInputError("событие не является JSON-объектом")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HookInputError("в событии нет строки session_id")
    agent_id = payload.get("agent_id")
    agent = agent_id if isinstance(agent_id, str) else ""
    name = payload.get("hook_event_name")
    return Event(
        name if isinstance(name, str) else "",
        session_id,
        agent,
        operation_id(session_id, agent),
        payload,
    )


def checkout_path(file_path: str, cwd: str) -> Optional[Target]:
    """Путь правки от ближайшего вверх каталога с ``.git`` (каталог или файл worktree); вне — None."""
    raw = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    real = os.path.realpath(raw)
    probe = os.path.dirname(real)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            rel = os.path.relpath(real, probe).replace(os.sep, "/")
            return Target(probe, real, rel)
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def edited_path(event: Event) -> Optional[Target]:
    """Путь ``tool_input.file_path`` события PreToolUse; другое событие или путь вне git — None."""
    if event.name != "PreToolUse":
        return None
    tool_input = event.payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        raise HookInputError("в событии нет строки tool_input.file_path")
    cwd = event.payload.get("cwd")
    return checkout_path(file_path, cwd if isinstance(cwd, str) else os.getcwd())


class Store:
    """Каталог операции: записи создаются через O_EXCL и не меняются, число записей ограничено."""

    def __init__(self, root: Path, operation: str) -> None:
        self.base = root.joinpath(*STORE_PARTS)
        self.directory = self.base / operation

    def _open(self) -> None:
        """Создать каталог операции; при переполнении удалить самые старые операции по mtime."""
        if self.directory.is_dir():
            return
        self.base.mkdir(parents=True, exist_ok=True)
        operations = sorted(
            (entry for entry in self.base.iterdir() if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime_ns,
        )
        while len(operations) >= MAX_OPERATIONS:
            shutil.rmtree(operations.pop(0), ignore_errors=True)
        self.directory.mkdir(exist_ok=True)

    def create(self, name: str, record: Dict[str, object]) -> bool:
        """Создать запись ``name``; False — запись уже есть. ``StoreFull`` — операция заполнена."""
        self._open()
        if len(os.listdir(self.directory)) >= MAX_RECORDS:
            raise StoreFull(
                f"в операции {self.directory.name} уже {MAX_RECORDS} записей"
            )
        path = self.directory / name
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        return True

    def records(self, prefix: str) -> List[Dict[str, object]]:
        """Записи операции с префиксом имени; нет каталога — пусто."""
        if not self.directory.is_dir():
            return []
        found: List[Dict[str, object]] = []
        for path in sorted(self.directory.glob(f"{prefix}*.json")):
            record: object = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict):
                found.append(record)
        return found


def _profile(root: Path) -> Optional[str]:
    """Профиль evidence-записи — ``read_profile`` конверта рядом с модулем; без helper-а — None."""
    try:
        helper = importlib.import_module("hook_response")
    except ImportError:
        return None
    value: object = helper.read_profile(root)
    return value if isinstance(value, str) else None


def notify(
    event: Event, root: Path, hook_id: str, subject: object, path: str
) -> Optional[str]:
    """Создать маркер уведомления и evidence-запись; ``None`` — такое уведомление уже было.

    Ключ — (hook_id, событие, digest предмета, операция): повтор того же события молчит, а новый
    digest или новая операция дают новое уведомление. Маркер создаётся через O_EXCL, поэтому из
    параллельных вызовов уведомляет ровно один. Результат — путь evidence от корня проекта.
    """
    digest = _sha256(subject)
    key = _sha256([hook_id, event.name, digest, event.operation_id])[:32]
    record: Dict[str, object] = {
        "hook_id": hook_id,
        "event": event.name,
        "path": path,
        "subject_digest": digest,
        "operation_id": event.operation_id,
        "session_id": event.session_id,
        "agent_id": event.agent_id,
        "profile": _profile(root),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    store = Store(root, event.operation_id)
    if not store.create(f"n-{key}.json", record):
        return None
    return (store.directory / f"n-{key}.json").relative_to(root).as_posix()


class Notice(NamedTuple):
    """Предупреждение hook-а: причина, текст, предмет дедупликации и вывод протокола runtime."""

    hook_id: str
    reason: str
    text: str
    subject: object
    path: str
    output: Dict[str, object]


def announce(event: Event, root: Path, notice: Notice) -> Output:
    """Новое предупреждение: stdout протокола, ``[reason] текст`` и evidence_ref в stderr.

    Дубликат молчит; заполненная операция разрешает с диагностикой без префикса причины.
    """
    try:
        evidence = notify(event, root, notice.hook_id, notice.subject, notice.path)
    except StoreFull as exc:
        return None, [f"уведомление не записано: {exc}"]
    if evidence is None:
        return None, []
    return notice.output, [
        f"[{notice.reason}] {notice.text}",
        f"evidence_ref={evidence}",
    ]


def _pre_tool_context(text: str) -> Dict[str, object]:
    """Контекст для модели рядом с результатом инструмента; решение о разрешении не меняется."""
    return {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}
    }


def impact_first_touch(event: Event, root: Path) -> Output:
    target = edited_path(event)
    code = risky_code(target.rel) if target is not None else None
    if target is None or code is None:
        return None, []
    text = (
        f"Рискованный путь {target.rel} ({code}): ChangeImpact не вычисляет его влияние по графу, "
        "поэтому review и QA проверяют полный набор. Назовите путь в отчёте и запустите проверки brief."
    )
    # Одно уведомление на путь в операции: это и есть первое касание.
    notice = Notice(
        IMPACT_FIRST_TOUCH,
        REASON_RISKY,
        text,
        target.rel,
        target.rel,
        _pre_tool_context(text),
    )
    return announce(event, root, notice)


def sensitive_path_recheck(event: Event, root: Path) -> Output:
    target = edited_path(event)
    if target is None or not is_sensitive(target.rel):
        return None, []
    text = (
        f"Чувствительный путь {target.rel} (манифест, CI, миграции, auth, права доступа или hooks): "
        "перепроверьте правку. Под strict review, QA и publish требуют VerificationPlan, который "
        "покрывает этот путь."
    )
    # Каждая новая правка проверяется заново; повтор той же правки — дубликат.
    subject = [
        target.rel,
        event.payload.get("tool_name"),
        event.payload.get("tool_input"),
    ]
    notice = Notice(
        SENSITIVE_PATH_RECHECK,
        REASON_SENSITIVE,
        text,
        subject,
        target.rel,
        _pre_tool_context(text),
    )
    return announce(event, root, notice)


def _record_touch(event: Event, root: Path) -> Output:
    target = edited_path(event)
    if target is None:
        return None, []
    kind = "report" if fnmatchcase(_fold(target.rel), _fold(REPORT_GLOB)) else "touch"
    record: Dict[str, object] = {
        "path": target.real,
        "root": target.root,
        "rel": target.rel,
        "kind": kind,
    }
    name = f"t-{_sha256(target.real)[:32]}.json"
    try:
        Store(root, event.operation_id).create(name, record)
    except StoreFull as exc:
        return None, [f"журнал касаний не пополнен: {exc}"]
    return None, []


def _changes(
    records: List[Dict[str, object]],
) -> Tuple[List[Tuple[str, int, int, str]], List[int]]:
    """(изменённые файлы (путь, mtime_ns, размер, корень), mtime_ns отчётов) по журналу касаний."""
    changes: List[Tuple[str, int, int, str]] = []
    reports: List[int] = []
    for record in records:
        path, rel, root = record.get("path"), record.get("rel"), record.get("root")
        if not (
            isinstance(path, str) and isinstance(rel, str) and isinstance(root, str)
        ):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if record.get("kind") == "report":
            reports.append(stat.st_mtime_ns)
        elif not _fold(rel).startswith(UNWATCHED_PREFIXES):
            changes.append((path, stat.st_mtime_ns, stat.st_size, root))
    return changes, reports


def targeted_check_reminder(event: Event, root: Path) -> Output:
    if event.name == "PreToolUse":
        return _record_touch(event, root)
    if event.name not in STOP_EVENTS or event.payload.get("stop_hook_active") is True:
        return None, []
    changes, reports = _changes(Store(root, event.operation_id).records("t-"))
    last_report = max(reports) if reports else None
    if not changes or (
        last_report is not None and last_report > max(item[1] for item in changes)
    ):
        return None, []
    worktrees = ", ".join(sorted({item[3] for item in changes}))
    text = (
        f"После последнего отчёта изменено файлов: {len(changes)} ({worktrees}). Отчитайте "
        "targeted-проверки brief в checks_run: команда и результат каждой."
    )
    # Новое изменение файла или новый отчёт меняют digest и дают новое напоминание.
    state = [sorted(item[:3] for item in changes), last_report]
    notice = Notice(
        TARGETED_CHECK_REMINDER,
        REASON_UNREPORTED,
        text,
        state,
        worktrees,
        {"systemMessage": text},
    )
    return announce(event, root, notice)


HANDLERS: Dict[str, Callable[[Event, Path], Output]] = {
    IMPACT_FIRST_TOUCH: impact_first_touch,
    TARGETED_CHECK_REMINDER: targeted_check_reminder,
    SENSITIVE_PATH_RECHECK: sensitive_path_recheck,
}


def main(argv: List[str]) -> int:
    """``<hook_id>``: прочитать событие, решить и напечатать вывод; код возврата всегда 0.

    Внутренняя ошибка (нечитаемое событие, сбой хранилища, любое исключение) разрешает действие:
    stdout пуст, а stderr начинается с ``[internal-error]`` и называет причину.
    """
    try:
        if len(argv) != 1 or argv[0] not in HANDLERS:
            raise HookInputError("ожидается один hook_id: " + ", ".join(HANDLERS))
        event = read_event(sys.stdin.read())
        output, diagnostics = HANDLERS[argv[0]](event, project_root())
    except Exception as exc:  # advisory-hook не блокирует и не ломает протокол runtime
        print(
            f"[{REASON_INTERNAL_ERROR}] change_impact_hooks.py: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 0
    if output is not None:
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
    for line in diagnostics:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
