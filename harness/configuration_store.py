"""Durable local configuration versions and recoverable paired writes.

Distribution-side code: validates sets against the Harness contract, so it is not part of
the lightweight readers installed into target projects (see ``harness.configuration``).
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
from typing import BinaryIO, Iterator

from harness.configuration import (
    MAX_BYTES,
    NAMES,
    Bundle,
    decode_bundle,
    inside_project,
    parse_object,
    read_config_bundle,
    read_journal,
    read_limited,
    reject_secrets,
)
from harness.configuration_schema import schema_problems
from harness.health.project_files import validate_project_data
from harness.json_types import JsonObject
from harness.orchestration.contract import configuration_problems

_VERSION_ID = r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}"


def _reject_file_secrets(name: str, data: JsonObject) -> None:
    try:
        reject_secrets(data)
    except ValueError:
        raise ValueError(
            f"{name} содержит значение, похожее на секрет; замените его ссылкой "
            "на переменную окружения"
        ) from None


def _encode(bundle: Bundle) -> JsonObject:
    return {
        name: None if raw is None else base64.b64encode(raw).decode("ascii")
        for name, raw in bundle.items()
    }


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


def _lock(stream: BinaryIO) -> None:
    """Take a non-blocking exclusive lock on the first byte of the lock file."""
    if sys.platform == "win32":
        import msvcrt

        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def json_bytes(value: JsonObject) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()


class StaleConfiguration(ValueError):
    """A file was changed by another process after the draft or version was read."""


@dataclass(frozen=True)
class ConfigurationVersion:
    id: str
    created_at: str
    description: str
    files: tuple[str, ...]
    package_version: str = "unknown"
    problem: str = ""


class ConfigurationStore:
    """Immutable local versions and serialized application with automatic backup."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo.expanduser().resolve()
        self.root = inside_project(self.repo, self.repo / ".harness/config-versions")
        self.marker = self.root / ".transaction.json"

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Also protects versions in projects with an older preserved .harness/.gitignore.
        ignore = inside_project(self.repo, self.root / ".gitignore")
        if not ignore.exists():
            _atomic(ignore, b"*\n")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._prepare()
        path = inside_project(self.repo, self.root / ".writer.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            try:
                _lock(stream)
            except OSError:
                raise ValueError("Другой процесс изменяет конфигурацию") from None
            try:
                yield
            finally:
                _unlock(stream)

    def installed_version(self) -> str:
        """Harness version recorded in the target's lock, or ``unknown``."""
        lock = read_limited(
            inside_project(self.repo, self.repo / ".harness/harness.lock")
        )
        version = parse_object(lock).get("package_version", "unknown") if lock else ""
        if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", version):
            return version
        return "unknown"

    def _version_path(self, identifier: str) -> Path:
        if not re.fullmatch(_VERSION_ID, identifier):
            raise ValueError("Некорректный идентификатор версии")
        return inside_project(self.repo, self.root / identifier)

    def problems(self, bundle: Bundle) -> list[str]:
        """Contract problems of a set; secrets and malformed files raise ValueError."""
        problems: list[str] = []
        for name, raw in bundle.items():
            if name not in NAMES:
                raise ValueError("Неизвестный файл конфигурации")
            if raw is None:
                continue
            if len(raw) > MAX_BYTES:
                raise ValueError("Файл конфигурации слишком большой")
            data = parse_object(raw)
            _reject_file_secrets(name, data)
            problems.extend(schema_problems(self.repo, name, data))
            if name == "project.json":
                validate_project_data(data, problems)
            else:
                problems.extend(
                    configuration_problems(
                        data, self.repo / ".harness/orchestration/roles"
                    )
                )
        return problems

    def _validate(self, bundle: Bundle) -> None:
        problems = self.problems(bundle)
        if problems:
            # Secrets are rejected first, so the remaining authored text is safe to show.
            shown = "; ".join(problem[:200] for problem in problems[:5])
            more = f" (и ещё {len(problems) - 5})" if len(problems) > 5 else ""
            raise ValueError(
                "Набор конфигурации несовместим с контрактом Harness: "
                + shown
                + more
                + ". Исправьте поля в мастере"
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
            for name, raw in bundle.items():
                if raw is not None:
                    _reject_file_secrets(name, parse_object(raw))
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
        package_version = self.installed_version()
        version = ConfigurationVersion(
            identifier,
            created,
            description,
            tuple(n for n in NAMES if bundle[n] is not None),
            package_version,
        )
        _atomic(
            staging / "manifest.json",
            json_bytes(
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
        raw = read_limited(inside_project(self.repo, path))
        if raw is None:
            raise ValueError("Версия отсутствует или повреждена")
        manifest = parse_object(raw)
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
            raw = read_limited(
                inside_project(self.repo, self._version_path(identifier) / name)
            )
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
                # Hash or contract damage is reported when the version is loaded.
                result.append(
                    ConfigurationVersion(
                        path.name,
                        manifest["created_at"],
                        manifest["description"],
                        tuple(n for n in NAMES if manifest["hashes"][n] is not None),
                        str(manifest.get("package_version", "unknown")),
                    )
                )
            except ValueError as exc:
                if re.fullmatch(_VERSION_ID, path.name):
                    result.append(
                        ConfigurationVersion(
                            path.name, path.name[:16], "", (), problem=str(exc)
                        )
                    )
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    def _recover(self) -> None:
        journal = read_journal(inside_project(self.repo, self.marker))
        if journal is None or journal["state"] != "prepared":
            return
        before = decode_bundle(journal["before"])
        after = decode_bundle(journal["after"])
        for name in NAMES:
            path = inside_project(self.repo, self.repo / ".harness" / name)
            if read_limited(path) not in (before[name], after[name]):
                raise ValueError(
                    "Незавершённая запись и внешнее изменение: восстановление требует проверки"
                )
        for name in NAMES:
            _atomic(
                inside_project(self.repo, self.repo / ".harness" / name), before[name]
            )
        journal["state"] = "recovered"
        _atomic(inside_project(self.repo, self.marker), json_bytes(journal))

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
                raise StaleConfiguration(
                    "Файл изменён другим процессом; загрузите внешние изменения и заново просмотрите итог"
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
            _atomic(inside_project(self.repo, self.marker), json_bytes(journal))
            try:
                for name in NAMES:
                    path = inside_project(self.repo, self.repo / ".harness" / name)
                    _atomic(path, after[name])
                journal["state"] = "committed"
                _atomic(inside_project(self.repo, self.marker), json_bytes(journal))
            except OSError:
                try:
                    # replace may succeed before directory fsync fails: restore the
                    # prepared generation marker before rolling back the pair.
                    journal["state"] = "prepared"
                    _atomic(inside_project(self.repo, self.marker), json_bytes(journal))
                    self._recover()
                except (OSError, ValueError):
                    raise ValueError(
                        "Запись прервана; проверьте конфигурацию и восстановите незавершённую транзакцию"
                    ) from None
                raise ValueError(
                    "Запись не выполнена; прежняя конфигурация восстановлена"
                ) from None
            return backup
