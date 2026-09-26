#!/usr/bin/env python3
"""Offline parser bundle: найти, проверить, установить и запустить закреплённый bundle.

Модуль — фасад пакета bundle. Он ищет bundle в registry, сверяет его с интерпретатором и платформой,
устанавливает через `bundle_install` и отдаёт готовый `AppliedBundle` для запуска `bundle_worker`.
Формат lock описан в `bundle_lock`, протокол worker — в `bundle_worker`; их публичные имена
реэкспортируются здесь, поэтому потребители импортируют только этот модуль. Пакет не импортирует
`tree_sitter*` и проверяется основным `mypy --strict`; сам worker — tree_sitter_worker.py, а
scripts/build_parser_bundle.py собирает bundle из закреплённых wheels (ADR 0024).

Все точки входа вызываются только при `repo_map_policy.tier`, равном `full` (по умолчанию). Для
отсутствующего, несовпадающего или сбойного bundle они не бросают исключений, а возвращают строку
`DegradationReason`, поэтому любой сбой превращается в корректную карту `minimal`, а не в падение
или сетевой вызов.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from harness.repo_map.bundle_install import (
    INSTALL_MARKER_FILENAME,
    BundleInstallError,
    UvUnavailableError,
    install_bundle,
    install_dir_name,
)
from harness.repo_map.bundle_lock import (
    LOCK_FILENAME,
    ArtifactSpec,
    BundleFormatError,
    BundleLock,
    GrammarSpec,
    parse_lock,
)
from harness.repo_map.bundle_worker import (
    SUPPORTED_GRAMMARS,
    WORKER_ERROR_LOG_FILENAME,
    BundleParseResult,
    FileFacts,
    ImportFact,
    SignatureFact,
    WorkerFailure,
    _file_facts,
    run_bundle_parser,
)
from harness.storage import storage_path

__all__ = [
    "INSTALL_MARKER_FILENAME",
    "LOCK_FILENAME",
    "AppliedBundle",
    "ArtifactSpec",
    "BundleFormatError",
    "BundleInstallError",
    "BundleLock",
    "BundleParseResult",
    "DegradationReason",
    "FileFacts",
    "GrammarSpec",
    "ImportFact",
    "LocatedBundle",
    "SignatureFact",
    "UvUnavailableError",
    "_file_facts",
    "acquire_bundle",
    "build_provenance",
    "bundle_cache_root",
    "check_bundle",
    "default_registry_dir",
    "find_bundle",
    "install_bundle",
    "install_dir_name",
    "locate_bundle",
    "parse_lock",
    "python_platform_tags",
    "run_bundle_parser",
    "search_dirs",
    "supports_real_sources",
    "verify_wheelhouse",
    "verify_worker_script",
]

DegradationReason = Literal[
    WorkerFailure,
    "offline parser bundle unavailable",
    "parser wheelhouse missing for interpreter/platform pair",
    "parser bundle install failed",
    "uv executable unavailable",
]
BundleSource = Literal["local-cache", "internal-registry"]

# Cache identity of a bundle whose bytes could not be read in full.
_INCOMPLETE_IDENTITY = b"invalid-or-incomplete"


class VerifyResult(TypedDict):
    """Результат проверки wheelhouse: успех либо причина деградации."""

    ok: bool
    reason: DegradationReason | None


@dataclass(frozen=True)
class LocatedBundle:
    """Найденный bundle: каталог, разобранный lock и пара интерпретатора, который запустит worker."""

    directory: Path
    lock: BundleLock
    python_tag: str
    platform_tag: str
    bundle_source: BundleSource

    @property
    def pair(self) -> str:
        """Пара интерпретатор/платформа в формате `cpXY-platform`."""
        return f"{self.python_tag}-{self.platform_tag}"

    @property
    def wheelhouse_dir(self) -> Path:
        """Каталог wheels этой пары внутри bundle."""
        return self.directory / "wheelhouse" / self.pair

    @property
    def worker_script(self) -> Path:
        """Путь к копии worker внутри bundle."""
        return self.directory / self.lock.worker_script

    def identity(self) -> str:
        """SHA-256 каталога, lock, worker и wheels этой пары: ключ кэша меняется с любым их байтом."""
        digest = hashlib.sha256(str(self.directory.resolve()).encode("utf-8"))
        digest.update(self.lock.raw_sha256.encode("ascii"))
        paths = [self.worker_script]
        paths.extend(
            self.wheelhouse_dir / artifact.filename
            for artifact in self.lock.wheelhouses.get(self.pair, ())
        )
        try:
            for path in paths:
                digest.update(path.read_bytes())
        except OSError:
            digest.update(_INCOMPLETE_IDENTITY)
        return digest.hexdigest()


@dataclass(frozen=True)
class AppliedBundle:
    """Bundle, у которого lock, wheelhouse, установка и worker-скрипт успешно прошли проверку."""

    lock: BundleLock
    install_dir: Path
    worker_script: Path
    python_tag: str
    platform_tag: str
    bundle_source: BundleSource
    worker_extensions: dict[str, str]

    @property
    def error_log(self) -> Path:
        """Диагностический лог последнего сбоя worker рядом с установкой bundle."""
        return self.install_dir.parent / WORKER_ERROR_LOG_FILENAME


def default_registry_dir(repo: Path) -> Path:
    """Переносимый локальный registry bundle проекта; никогда не коммитится (`.harness/` в gitignore)."""
    return storage_path(repo, ".cache", "repo_map", "parser_bundle", "registry")


def bundle_cache_root(repo: Path) -> Path:
    """Общий каталог bundle: registry и установленные копии по lock и паре."""
    return storage_path(repo, ".cache", "repo_map", "parser_bundle")


def search_dirs(repo: Path, registry_paths: tuple[str, ...]) -> tuple[Path, ...]:
    """Каталоги поиска bundle: локальный registry, затем `parser_bundle_registry_paths` (относительные — от репозитория)."""
    extra = tuple(
        Path(path) if Path(path).is_absolute() else repo / path for path in registry_paths
    )
    return (default_registry_dir(repo), *extra)


def find_bundle(dirs: tuple[Path, ...]) -> Path | None:
    """Вернуть первый каталог из `dirs`, где есть `parser_bundle.lock.json`, иначе `None`."""
    for candidate in dirs:
        if (candidate / LOCK_FILENAME).is_file():
            return candidate
    return None


def python_platform_tags(python_executable: str, timeout_seconds: int) -> tuple[str, str]:
    """Вернуть пару (python_tag, platform_tag) интерпретатора, который будет запускать worker.

    Теги берутся коротким subprocess `-c` этого интерпретатора, а не из текущего процесса: загрузчик и
    worker не обязаны работать в одном интерпретаторе (ADR 0024).
    """
    script = (
        "import sys, sysconfig, json;"
        "print(json.dumps(["
        "f'cp{sys.version_info[0]}{sys.version_info[1]}',"
        "sysconfig.get_platform().replace('-', '_').replace('.', '_'),"
        "]))"
    )
    result = subprocess.run(
        [python_executable, "-c", script],
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise BundleFormatError(
            f"could not determine python/platform tags: {result.stderr.decode('utf-8', 'replace')}"
        )
    decoded: object = json.loads(result.stdout.decode("utf-8"))
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise BundleFormatError("could not determine python/platform tags: malformed output")
    python_tag, platform_tag = cast(list[str], decoded)
    return python_tag, platform_tag


def verify_wheelhouse(lock: BundleLock, wheelhouse_dir: Path, pair: str) -> VerifyResult:
    """Проверить SHA-256 каждого артефакта, нужного паре интерпретатор/платформа."""
    artifacts = lock.wheelhouses.get(pair)
    if artifacts is None or not wheelhouse_dir.is_dir():
        return {"ok": False, "reason": "parser wheelhouse missing for interpreter/platform pair"}
    for artifact in artifacts:
        artifact_path = wheelhouse_dir / artifact.filename
        if not artifact_path.is_file():
            return {"ok": False, "reason": "parser bundle hash mismatch"}
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != artifact.sha256:
            return {"ok": False, "reason": "parser bundle hash mismatch"}
    return {"ok": True, "reason": None}


def verify_worker_script(lock: BundleLock, worker_script: Path) -> bool:
    """Проверить, что worker-скрипт существует и его SHA-256 совпадает с lock."""
    if not worker_script.is_file():
        return False
    digest = hashlib.sha256(worker_script.read_bytes()).hexdigest()
    return digest == lock.script_sha256


def locate_bundle(
    *,
    repo: Path,
    registry_paths: tuple[str, ...],
    python_executable: str,
    timeout_seconds: int,
) -> LocatedBundle | DegradationReason:
    """Найти bundle и определить пару интерпретатора, ничего не проверяя и не устанавливая.

    Результат используется и для ключа кэша, и для последующей установки, поэтому обе операции
    опираются на один и тот же найденный bundle.
    """
    dirs = search_dirs(repo, registry_paths)
    bundle_dir = find_bundle(dirs)
    if bundle_dir is None:
        return "offline parser bundle unavailable"
    try:
        lock = parse_lock((bundle_dir / LOCK_FILENAME).read_bytes())
    except (OSError, BundleFormatError):
        return "offline parser bundle unavailable"
    try:
        python_tag, platform_tag = python_platform_tags(python_executable, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired, BundleFormatError, json.JSONDecodeError):
        return "parser subprocess failed"
    return LocatedBundle(
        directory=bundle_dir,
        lock=lock,
        python_tag=python_tag,
        platform_tag=platform_tag,
        bundle_source="local-cache" if bundle_dir == dirs[0] else "internal-registry",
    )


def check_bundle(located: LocatedBundle) -> DegradationReason | None:
    """Проверить wheels пары и worker найденного bundle без установки; `None`, если всё совпало."""
    verify = verify_wheelhouse(located.lock, located.wheelhouse_dir, located.pair)
    if not verify["ok"]:
        return verify["reason"]
    if not verify_worker_script(located.lock, located.worker_script):
        return "parser bundle hash mismatch"
    return None


def supports_real_sources(lock: BundleLock) -> bool:
    """Есть ли в lock хотя бы одна грамматика, для которой у worker есть extractor."""
    return any(grammar.name in SUPPORTED_GRAMMARS for grammar in lock.grammars)


def acquire_bundle(
    *,
    repo: Path,
    registry_paths: tuple[str, ...],
    python_executable: str,
    timeout_seconds: int,
    located: LocatedBundle | DegradationReason | None = None,
) -> AppliedBundle | DegradationReason:
    """Проверить и установить parser bundle для тегов `python_executable`.

    `located` — результат `locate_bundle`; без него bundle ищется здесь же. Возвращает
    `AppliedBundle`, готовый для `run_bundle_parser`, или `DegradationReason` и никогда не бросает
    исключений. Сетевых вызовов нет: читается только локальная файловая система, а установка идёт
    через `uv pip install --offline --no-index --find-links <локальный wheelhouse>`.
    """
    if located is None:
        located = locate_bundle(
            repo=repo,
            registry_paths=registry_paths,
            python_executable=python_executable,
            timeout_seconds=timeout_seconds,
        )
    if isinstance(located, str):
        return located
    problem = check_bundle(located)
    if problem is not None:
        return problem
    lock = located.lock
    install_dir = bundle_cache_root(repo) / install_dir_name(lock, located.pair) / "install"
    try:
        install_bundle(
            lock,
            located.wheelhouse_dir,
            install_dir,
            python_executable,
            pair=located.pair,
            timeout_seconds=timeout_seconds,
        )
    except UvUnavailableError:
        return "uv executable unavailable"
    except BundleInstallError:
        return "parser bundle install failed"
    return AppliedBundle(
        lock=lock,
        install_dir=install_dir,
        worker_script=located.worker_script,
        python_tag=located.python_tag,
        platform_tag=located.platform_tag,
        bundle_source=located.bundle_source,
        worker_extensions=lock.extension_grammars(),
    )


def build_provenance(
    lock: BundleLock | None,
    *,
    bundle_mode: Literal["applied", "degraded"],
    bundle_source: Literal["local-cache", "internal-registry", "none"],
    python_tag: str | None,
    platform_tag: str | None,
) -> dict[str, object]:
    """Собрать поля `parser_provenance`, за которые отвечает этот модуль; они дополняют поля repo_map."""
    provenance: dict[str, object] = {
        "bundle_mode": bundle_mode,
        "bundle_source": bundle_source,
    }
    if python_tag is not None:
        provenance["python_tag"] = python_tag
    if platform_tag is not None:
        provenance["platform_tag"] = platform_tag
    if bundle_mode == "applied":
        assert lock is not None
        provenance["lock_sha256"] = lock.raw_sha256
        provenance["script_hash"] = lock.script_sha256
        provenance["core_version"] = lock.core_version
        provenance["core_abi_range"] = lock.core_abi_range
        provenance["grammars"] = [
            {
                "name": grammar.name,
                "version": grammar.version,
                "abi": grammar.abi,
                "sha256": (
                    grammar.sha256_by_pair[f"{python_tag}-{platform_tag}"]
                    if grammar.sha256_by_pair is not None and python_tag is not None and platform_tag is not None
                    else grammar.sha256
                ),
            }
            for grammar in lock.grammars
        ]
    return provenance
