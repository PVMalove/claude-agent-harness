"""Coherent, bounded reads of the paired project configuration files.

Readers observe the previous set while a transaction is prepared. The committed journal
is retained as a generation marker, so readers retry if a writer crosses their read.
The module is portable stdlib code shared by health, the coordinator and the console;
writing and versions live in ``harness.configuration_store``.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from harness.json_types import JsonObject

NAMES = ("project.json", "orchestration.json")
MAX_BYTES = 2_000_000
Bundle = dict[str, bytes | None]
_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|password|secret|(?:access|auth|refresh|id)[_-]?token|token|client[_-]?secret|private[_-]?key|authorization|credential)s?$",
    re.I,
)
_SECRET_TEXT = re.compile(
    r"(?:\b(?:token|password|api[_-]?key|secret)\s*[=:]\s*(?![$%<{])[^\s<]+|"
    r"\bBearer\s+\S+|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})|"
    r"https?://[^/\s:@]+:[^/\s@]+@)",
    re.I,
)


def inside_project(repo: Path, path: Path) -> Path:
    """Reject links and escapes below ``repo``; the repository root itself may be a link."""
    root = repo.resolve()
    try:
        relative = path.relative_to(repo)
    except ValueError:
        raise ValueError("Путь конфигурации вне проекта") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Пути конфигурации не должны быть символическими ссылками")
    if not current.resolve().is_relative_to(root):
        raise ValueError("Путь конфигурации вне проекта")
    return path


def read_limited(path: Path) -> bytes | None:
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


def parse_object(raw: bytes, label: str = "") -> JsonObject:
    """Parse a standard-JSON object; ``label`` names the file in the error text."""
    prefix = label + ": " if label else ""
    try:
        value = parse_config_json(raw)
    except (ValueError, RecursionError):
        raise ValueError(prefix + "требуется корректный JSON в UTF-8") from None
    if not isinstance(value, dict):
        raise ValueError(prefix + "требуется JSON-объект")
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


def decode_bundle(value: object) -> Bundle:
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


def read_journal(path: Path) -> JsonObject | None:
    # Journals contain two encoded sets and may exceed the single-file limit.
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES * 8 + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES * 8:
        raise ValueError("Журнал конфигурации слишком большой")
    data = parse_object(raw)
    if data.get("schema") != 1 or data.get("state") not in (
        "prepared",
        "committed",
        "recovered",
    ):
        raise ValueError("Несовместимый журнал конфигурации")
    decode_bundle(data.get("before"))
    decode_bundle(data.get("after"))
    return data


def read_config_bundle(repo: Path) -> Bundle:
    """Read a coherent generation without changing files or recovering a live writer."""
    root = inside_project(repo, repo / ".harness")
    marker = inside_project(repo, root / "config_versions/.transaction.json")
    for _ in range(8):
        before = read_journal(marker)
        if before is not None and before["state"] == "prepared":
            return decode_bundle(before["before"])
        bundle = {
            name: read_limited(inside_project(repo, root / name)) for name in NAMES
        }
        if before == read_journal(marker):
            return bundle
    raise ValueError("Конфигурация меняется; повторите чтение")


def read_config_file(path: Path) -> bytes | None:
    """Use paired reads for the two project-owned config files, ordinary reads otherwise."""
    if path.name in NAMES and path.parent.name == ".harness":
        return read_config_bundle(path.parent.parent)[path.name]
    return read_limited(path)


def read_config_json(path: Path) -> object:
    """Parsed JSON of a project config file, ``{}`` when it is absent.

    Raises ``ValueError`` for malformed, oversized or linked files.
    """
    raw = read_config_file(path)
    return {} if raw is None else json.loads(raw)
