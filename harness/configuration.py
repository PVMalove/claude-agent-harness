"""Durable local configuration versions and recoverable paired writes.

Readers observe the previous set while a transaction is prepared. The committed journal
is retained as a generation marker, so readers retry if a writer crosses their read.
The module is portable stdlib code shared by health, the coordinator and the console.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from harness.json_types import JsonObject

NAMES = ("project.json", "orchestration.json")
MAX_BYTES = 2_000_000
Bundle = dict[str, bytes | None]
_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|password|secret|(?:access|auth|refresh|id)[_-]?token|token|client[_-]?secret|private[_-]?key|authorization|credential)s?$",
    re.I,
)
_SECRET_TEXT = re.compile(
    r"(?:\b(?:token|password|api[_-]?key|secret)\s*[=:]\s*[^\s<]+|"
    r"\bBearer\s+\S+|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})|"
    r"https?://[^/\s:@]+:[^/\s@]+@)",
    re.I,
)


def _inside(repo: Path, path: Path) -> Path:
    root = repo.resolve()
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Пути конфигурации не должны быть символическими ссылками")
        if current == current.parent:
            raise ValueError("Путь конфигурации вне проекта")
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise ValueError("Путь конфигурации вне проекта")
    return path


def _read(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл конфигурации слишком большой")
    return raw


def parse_config_json(raw: bytes | str) -> object:
    """Accept standard JSON only, including finite numbers at every depth."""

    def invalid_constant(value: str) -> object:
        raise ValueError("Некорректное JSON-значение")

    try:
        value = json.loads(raw, parse_constant=invalid_constant)
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError):
        raise ValueError("Некорректное JSON-значение") from None
    return value


def _object(raw: bytes) -> JsonObject:
    try:
        value = parse_config_json(raw)
    except (ValueError, RecursionError):
        raise ValueError("Некорректный JSON конфигурации или версии") from None
    if not isinstance(value, dict):
        raise ValueError("Конфигурация или версия должна быть JSON-объектом")
    return value


def reject_secrets(value: object) -> None:
    """Reject recognizable secrets without putting values in error messages."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                raise ValueError("Секреты нельзя сохранять в конфигурации и версиях")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise ValueError("Секреты нельзя сохранять в конфигурации и версиях")


def _encode(bundle: Bundle) -> JsonObject:
    return {
        name: None if raw is None else base64.b64encode(raw).decode("ascii")
        for name, raw in bundle.items()
    }


def _decode(value: object) -> Bundle:
    if not isinstance(value, dict) or set(value) != set(NAMES):
        raise ValueError("Неполный набор файлов версии")
    result: Bundle = {}
    for name in NAMES:
        raw = value[name]
        if raw is None:
            result[name] = None
        elif isinstance(raw, str) and len(raw) <= MAX_BYTES * 2:
            try:
                decoded = base64.b64decode(raw, validate=True)
            except ValueError:
                raise ValueError("Повреждённый набор конфигурации") from None
            if len(decoded) > MAX_BYTES:
                raise ValueError("Файл конфигурации слишком большой")
            result[name] = decoded
        else:
            raise ValueError("Повреждённый набор конфигурации")
    return result


def _journal(path: Path) -> JsonObject | None:
    # Journals contain two encoded sets and may exceed the single-file limit.
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES * 8 + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES * 8:
        raise ValueError("Журнал конфигурации слишком большой")
    data = _object(raw)
    if data.get("schema") != 1 or data.get("state") not in (
        "prepared",
        "committed",
        "recovered",
    ):
        raise ValueError("Несовместимый журнал конфигурации")
    _decode(data.get("before"))
    _decode(data.get("after"))
    return data


def read_config_bundle(repo: Path) -> Bundle:
    """Read a coherent generation without changing files or recovering a live writer."""
    root = _inside(repo, repo / ".harness")
    marker = _inside(repo, root / "config-versions/.transaction.json")
    for _ in range(8):
        before = _journal(marker)
        if before is not None and before["state"] == "prepared":
            return _decode(before["before"])
        bundle = {name: _read(_inside(repo, root / name)) for name in NAMES}
        if before == _journal(marker):
            return bundle
    raise ValueError("Конфигурация меняется; повторите чтение")


def read_config_file(path: Path) -> bytes | None:
    """Use paired reads for the two project-owned config files, ordinary reads otherwise."""
    if path.name in NAMES and path.parent.name == ".harness":
        return read_config_bundle(path.parent.parent)[path.name]
    return _read(path)


def _atomic(path: Path, raw: bytes | None) -> None:
    if raw is None:
        path.unlink(missing_ok=True)
        _sync_directory(path.parent)
        return
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _json(value: JsonObject) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()


@dataclass(frozen=True)
class ConfigurationVersion:
    id: str
    created_at: str
    description: str
    files: tuple[str, ...]
    package_version: str = "unknown"


class ConfigurationStore:
    """Immutable local versions and serialized application with automatic backup."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo.expanduser().resolve()
        self.root = _inside(self.repo, self.repo / ".harness/config-versions")
        self.marker = self.root / ".transaction.json"

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Also protects versions in projects with an older preserved .harness/.gitignore.
        ignore = _inside(self.repo, self.root / ".gitignore")
        if not ignore.exists():
            _atomic(ignore, b"*\n")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._prepare()
        path = _inside(self.repo, self.root / ".writer.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    if path.stat().st_size == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Другой процесс изменяет конфигурацию") from None
            try:
                yield
            finally:
                if sys.platform == "win32":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _version_path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", identifier):
            raise ValueError("Некорректный идентификатор версии")
        return _inside(self.repo, self.root / identifier)

    def _validate(self, bundle: Bundle) -> None:
        from harness.configuration_schema import schema_problems
        from harness.health.project_files import validate_project_data

        problems: list[str] = []
        for name, raw in bundle.items():
            if name not in NAMES:
                raise ValueError("Неизвестный файл конфигурации")
            if raw is None:
                continue
            if len(raw) > MAX_BYTES:
                raise ValueError("Файл конфигурации слишком большой")
            data = _object(raw)
            reject_secrets(data)
            problems.extend(schema_problems(self.repo, name, data))
            if name == "project.json":
                validate_project_data(data, problems)
            else:
                from harness.orchestration.contract import configuration_problems

                problems.extend(
                    configuration_problems(
                        data, self.repo / ".harness/orchestration/roles"
                    )
                )
        if problems:
            # Contract messages can contain authored strings; UI exposes field errors separately.
            raise ValueError(
                "Набор конфигурации несовместим с контрактом Harness; исправьте поля в мастере"
            )

    def save(self, bundle: Bundle, description: str = "") -> ConfigurationVersion:
        return self._save(bundle, description, check_contract=True)

    def _save(
        self, bundle: Bundle, description: str, *, check_contract: bool
    ) -> ConfigurationVersion:
        if set(bundle) != set(NAMES):
            raise ValueError("Сохраняйте полный набор конфигурации")
        if check_contract:
            self._validate(bundle)
        else:
            # A repair must retain the previous bytes even if they fail today's contract.
            for raw in bundle.values():
                if raw is not None:
                    reject_secrets(_object(raw))
        reject_secrets(description)
        self._prepare()
        now = datetime.now(timezone.utc)
        identifier = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        destination = self._version_path(identifier)
        staging = self.root / ("." + identifier)
        staging.mkdir(mode=0o700)
        hashes: JsonObject = {}
        for name, raw in bundle.items():
            if raw is None:
                hashes[name] = None
            else:
                _atomic(staging / name, raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
        created = now.isoformat()
        lock = _read(_inside(self.repo, self.repo / ".harness/harness.lock"))
        package_version = (
            _object(lock).get("package_version", "unknown") if lock else "unknown"
        )
        if not isinstance(package_version, str) or not re.fullmatch(
            r"[0-9A-Za-z.+_-]{1,80}", package_version
        ):
            package_version = "unknown"
        version = ConfigurationVersion(
            identifier,
            created,
            description,
            tuple(n for n in NAMES if bundle[n] is not None),
            package_version,
        )
        _atomic(
            staging / "manifest.json",
            _json(
                {
                    "schema": 1,
                    "id": identifier,
                    "created_at": created,
                    "description": description,
                    "hashes": hashes,
                    "package_version": package_version,
                }
            ),
        )
        os.replace(staging, destination)
        _sync_directory(self.root)
        return version

    def _manifest(self, identifier: str) -> JsonObject:
        path = self._version_path(identifier) / "manifest.json"
        raw = _read(_inside(self.repo, path))
        if raw is None:
            raise ValueError("Версия отсутствует или повреждена")
        manifest = _object(raw)
        if manifest.get("schema") != 1 or manifest.get("id") != identifier:
            raise ValueError("Несовместимый формат версии")
        if not isinstance(manifest.get("hashes"), dict) or set(
            manifest["hashes"]
        ) != set(NAMES):
            raise ValueError("Повреждённый состав версии")
        if not isinstance(manifest.get("created_at"), str) or not isinstance(
            manifest.get("description"), str
        ):
            raise ValueError("Повреждённые метаданные версии")
        return manifest

    def load(self, identifier: str) -> Bundle:
        manifest = self._manifest(identifier)
        bundle: Bundle = {}
        for name in NAMES:
            raw = _read(_inside(self.repo, self._version_path(identifier) / name))
            digest = None if raw is None else hashlib.sha256(raw).hexdigest()
            if digest != manifest["hashes"][name]:
                raise ValueError("Версия повреждена: хеш файла не совпадает")
            bundle[name] = raw
        self._validate(bundle)
        return bundle

    def versions(self) -> list[ConfigurationVersion]:
        result: list[ConfigurationVersion] = []
        if not self.root.exists():
            return result
        for path in sorted(self.root.iterdir(), reverse=True):
            if path.name.startswith("."):
                continue
            try:
                manifest = self._manifest(path.name)
                # Corrupted versions are listed safely but cannot be loaded/applied.
                result.append(
                    ConfigurationVersion(
                        path.name,
                        manifest["created_at"],
                        manifest["description"],
                        tuple(n for n in NAMES if manifest["hashes"][n] is not None),
                        str(manifest.get("package_version", "unknown")),
                    )
                )
            except ValueError:
                continue
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    def _recover(self) -> None:
        journal = _journal(_inside(self.repo, self.marker))
        if journal is None or journal["state"] != "prepared":
            return
        before = _decode(journal["before"])
        after = _decode(journal["after"])
        for name in NAMES:
            path = _inside(self.repo, self.repo / ".harness" / name)
            if _read(path) not in (before[name], after[name]):
                raise ValueError(
                    "Незавершённая запись и внешнее изменение: восстановление требует проверки"
                )
        for name in NAMES:
            _atomic(_inside(self.repo, self.repo / ".harness" / name), before[name])
        journal["state"] = "recovered"
        _atomic(_inside(self.repo, self.marker), _json(journal))

    def recover(self) -> None:
        with self._locked():
            self._recover()

    def apply(self, changes: Bundle, *, expected: Bundle) -> ConfigurationVersion:
        with self._locked():
            self._recover()
            before = read_config_bundle(self.repo)
            if any(
                name not in NAMES or before[name] != raw
                for name, raw in expected.items()
            ):
                raise ValueError(
                    "Файл изменён другим процессом; загрузите его и заново просмотрите изменения"
                )
            after = {**before, **changes}
            self._validate(after)
            backup = self._save(
                before, "Перед применением конфигурации", check_contract=False
            )
            journal: JsonObject = {
                "schema": 1,
                "state": "prepared",
                "id": uuid.uuid4().hex,
                "before": _encode(before),
                "after": _encode(after),
            }
            _atomic(_inside(self.repo, self.marker), _json(journal))
            try:
                for name in NAMES:
                    path = _inside(self.repo, self.repo / ".harness" / name)
                    _atomic(path, after[name])
                journal["state"] = "committed"
                _atomic(_inside(self.repo, self.marker), _json(journal))
            except OSError:
                try:
                    # replace may succeed before directory fsync fails: restore the
                    # prepared generation marker before rolling back the pair.
                    journal["state"] = "prepared"
                    _atomic(_inside(self.repo, self.marker), _json(journal))
                    self._recover()
                except (OSError, ValueError):
                    raise ValueError(
                        "Запись прервана; проверьте конфигурацию и восстановите незавершённую транзакцию"
                    ) from None
                raise ValueError(
                    "Запись не выполнена; прежняя конфигурация восстановлена"
                ) from None
            return backup
