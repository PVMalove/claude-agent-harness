"""Формат `parser_bundle.lock.json`: структуры lock и их строгая проверка.

Модуль не читает файловую систему и не запускает процессов: он только разбирает байты lock. Любое
отклонение от формата поднимает `BundleFormatError`, которую загрузчик превращает в деградацию до
`minimal`, а не в падение или установку.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

LOCK_FILENAME = "parser_bundle.lock.json"

# A bare, safe file name: no path separators, no `.`/`..` traversal, no absolute path or drive
# letter, no control characters or whitespace (the allowed character set excludes all of these).
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")

# A strict wheel file name (PEP 427): distribution-version(-build)?-pytag-abitag-platformtag.whl.
# The character classes below are a subset of `_SAFE_FILENAME_RE`'s, so a match is automatically a
# safe bare file name too.
_WHEEL_FILENAME_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?"
    r"-[A-Za-z0-9][A-Za-z0-9._!+]*"
    r"(?:-[A-Za-z0-9][A-Za-z0-9._]*)?"
    r"-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+"
    r"\.whl"
)

# A bare lowercase sha256 hex digest, the form `hashlib.sha256().hexdigest()` produces. Artifact
# digests are interpolated into the generated requirements file, so nothing else may pass.
SHA256_RE = re.compile(r"[0-9a-f]{64}")

# A wheel distribution name as it appears in a wheel file name prefix.
_DISTRIBUTION_RE = re.compile(r"[A-Za-z0-9_]+")


class BundleFormatError(ValueError):
    """Файл `parser_bundle.lock.json` имеет неверную структуру."""


@dataclass(frozen=True)
class ArtifactSpec:
    """Артефакт wheelhouse: безопасное имя wheel и его SHA-256."""

    filename: str
    sha256: str


@dataclass(frozen=True)
class GrammarSpec:
    """Грамматика из lock: имя, версия, ABI, хеш и расширения; для релизного lock — хеши по парам."""

    name: str
    version: str
    abi: int
    sha256: str
    extensions: tuple[str, ...]
    sha256_by_pair: dict[str, str] | None = None
    distribution: str | None = None


@dataclass(frozen=True)
class BundleLock:
    """Разобранный и проверенный `parser_bundle.lock.json` вместе с SHA-256 его исходных байтов."""

    core_version: str
    core_abi_range: str
    worker_script: str
    script_sha256: str
    grammars: tuple[GrammarSpec, ...]
    wheelhouses: dict[str, tuple[ArtifactSpec, ...]]
    raw_sha256: str

    def extension_grammars(self) -> dict[str, str]:
        """Соответствие расширения файла имени грамматики, которая его разбирает."""
        return {
            extension: grammar.name
            for grammar in self.grammars
            for extension in grammar.extensions
        }


def _string_field(obj: dict[str, object], field: str) -> str:
    """Вернуть непустое строковое поле lock или поднять `BundleFormatError`."""
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise BundleFormatError(f"parser_bundle.lock.json: {field} must be a non-empty string")
    return value


def _safe_bare_filename(value: str, where: str) -> str:
    """Проверить, что `value` — безопасное голое имя файла без обхода каталогов, разделителей и инъекций.

    Применяется к полям, которые подставляются в `Path` или в генерируемый файл requirements
    (`worker_script`, имена wheels). Lock, не прошедший проверку, даёт деградацию (`BundleFormatError`),
    а не падение или установку.
    """
    if not _SAFE_FILENAME_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a safe bare file name, got {value!r}"
        )
    return value


def _wheel_filename(value: str, where: str) -> str:
    """Проверить, что значение — корректное имя wheel по PEP 427."""
    if not _WHEEL_FILENAME_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a valid wheel file name, got {value!r}"
        )
    return value


def _sha256_field(obj: dict[str, object], field: str, where: str) -> str:
    """Проверить digest из lock как строчный hex SHA-256 без инъекций.

    Digest подставляется в генерируемый файл requirements: значение с переводом строки или опцией
    установщика добавило бы строки. Такой lock даёт деградацию (`BundleFormatError`), а не падение или
    установку.
    """
    value = _string_field(obj, field)
    if not SHA256_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a lowercase sha256 hex digest, got {value!r}"
        )
    return value


def _int_field(obj: dict[str, object], field: str) -> int:
    """Вернуть целочисленное поле lock (bool не допускается) или поднять `BundleFormatError`."""
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleFormatError(f"parser_bundle.lock.json: {field} must be an integer")
    return value


def _artifact_specs(value: object, where: str) -> tuple[ArtifactSpec, ...]:
    """Разобрать список артефактов wheelhouse одной пары интерпретатор/платформа."""
    if not isinstance(value, list):
        raise BundleFormatError(f"parser_bundle.lock.json: {where} must be a list")
    specs: list[ArtifactSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise BundleFormatError(f"parser_bundle.lock.json: {where} entries must be objects")
        specs.append(
            ArtifactSpec(
                filename=_wheel_filename(_string_field(item, "filename"), f"{where}.filename"),
                sha256=_sha256_field(item, "sha256", f"{where}.sha256"),
            )
        )
    return tuple(specs)


def _pair_hashes(value: object) -> dict[str, str] | None:
    """Разобрать необязательные хеши грамматики по парам интерпретатор/платформа."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BundleFormatError("parser_bundle.lock.json: grammars[].sha256_by_pair must be an object")
    hashes: dict[str, str] = {}
    for pair, digest in value.items():
        if not isinstance(pair, str) or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise BundleFormatError("parser_bundle.lock.json: invalid per-pair grammar hash")
        hashes[pair] = digest
    return hashes


def _grammar_spec(item: object) -> GrammarSpec:
    """Разобрать и проверить одну запись `grammars[]`."""
    if not isinstance(item, dict):
        raise BundleFormatError("parser_bundle.lock.json: grammars entries must be objects")
    extensions_raw = item.get("extensions")
    if not isinstance(extensions_raw, list) or not all(
        isinstance(entry, str) and entry for entry in extensions_raw
    ):
        raise BundleFormatError(
            "parser_bundle.lock.json: grammars[].extensions must be a list of non-empty strings"
        )
    pair_hashes = _pair_hashes(item.get("sha256_by_pair"))
    distribution = item.get("distribution")
    if pair_hashes is not None and (
        not isinstance(distribution, str) or _DISTRIBUTION_RE.fullmatch(distribution) is None
    ):
        raise BundleFormatError("parser_bundle.lock.json: per-pair grammar distribution is invalid")
    return GrammarSpec(
        name=_string_field(item, "name"),
        version=_string_field(item, "version"),
        abi=_int_field(item, "abi"),
        sha256=_sha256_field(item, "sha256", "grammars[].sha256"),
        extensions=tuple(extensions_raw),
        sha256_by_pair=pair_hashes,
        distribution=distribution if isinstance(distribution, str) else None,
    )


def _wheelhouses(value: object) -> dict[str, tuple[ArtifactSpec, ...]]:
    """Разобрать объект `wheelhouses`: пара интерпретатор/платформа → её артефакты."""
    if not isinstance(value, dict):
        raise BundleFormatError("parser_bundle.lock.json: wheelhouses must be an object")
    wheelhouses: dict[str, tuple[ArtifactSpec, ...]] = {}
    for pair, artifacts_raw in value.items():
        if not isinstance(pair, str):
            raise BundleFormatError("parser_bundle.lock.json: wheelhouses keys must be strings")
        wheelhouses[pair] = _artifact_specs(artifacts_raw, f"wheelhouses[{pair!r}]")
    return wheelhouses


def _check_pair_hashes(
    grammars: list[GrammarSpec], wheelhouses: dict[str, tuple[ArtifactSpec, ...]]
) -> None:
    """Сверить хеши грамматик по парам с wheel их дистрибутива в каждом wheelhouse."""
    for grammar in grammars:
        pair_hashes = grammar.sha256_by_pair
        if pair_hashes is None:
            continue
        prefix = f"{grammar.distribution}-{grammar.version}-"
        if set(pair_hashes) != set(wheelhouses) or any(
            not any(
                artifact.filename.startswith(prefix) and artifact.sha256 == pair_hashes[pair]
                for artifact in artifacts
            )
            for pair, artifacts in wheelhouses.items()
        ):
            raise BundleFormatError(
                "parser_bundle.lock.json: per-pair grammar hash does not match its distribution wheel"
            )


def parse_lock(raw: bytes) -> BundleLock:
    """Разобрать и структурно проверить содержимое `parser_bundle.lock.json`.

    Любая структурная ошибка поднимает `BundleFormatError`; вызывающий код трактует её так же, как
    отсутствие bundle (`offline parser bundle unavailable`).
    """
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"parser_bundle.lock.json: invalid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise BundleFormatError("parser_bundle.lock.json: root must be an object")
    grammars_raw = decoded.get("grammars")
    if not isinstance(grammars_raw, list):
        raise BundleFormatError("parser_bundle.lock.json: grammars must be a list")
    grammars = [_grammar_spec(item) for item in grammars_raw]
    wheelhouses = _wheelhouses(decoded.get("wheelhouses"))
    _check_pair_hashes(grammars, wheelhouses)
    return BundleLock(
        core_version=_string_field(decoded, "core_version"),
        core_abi_range=_string_field(decoded, "core_abi_range"),
        worker_script=_safe_bare_filename(_string_field(decoded, "worker_script"), "worker_script"),
        script_sha256=_sha256_field(decoded, "script_sha256", "script_sha256"),
        grammars=tuple(grammars),
        wheelhouses=wheelhouses,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def wheel_name_version(filename: str) -> tuple[str, str]:
    """Извлечь имя дистрибутива и версию из имени wheel."""
    stem = filename.removesuffix(".whl")
    parts = stem.split("-")
    if len(parts) < 2:
        raise BundleFormatError(f"parser_bundle.lock.json: malformed wheel filename {filename!r}")
    return parts[0], parts[1]
