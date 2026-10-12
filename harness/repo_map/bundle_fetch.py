"""Установка парсеров для выбранных языков: скачать закреплённые wheels и собрать bundle.

Единственный сетевой шаг Repo Map и только по явной команде `harness parsers install`. Каждый
wheel берётся из закреплённого `parser_wheels.json` (хост PyPI, SHA-256, лимит размера) и
отбрасывается при любом расхождении хеша. Из wheels собирается bundle в локальный registry проекта:
в lock попадают только выбранные и уже установленные грамматики. Дальше Repo Map работает офлайн,
как и раньше."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence, cast

from harness.errors import HarnessError
from harness.repo_map.bundle_build import BundleBuildError, build_bundle
from harness.repo_map.bundle_pins import (
    CORE_DISTRIBUTION,
    GRAMMARS,
    LANGUAGES,
    GrammarPin,
)
from harness.repo_map.parser_bundle import (
    AppliedBundle,
    BundleFormatError,
    LocatedBundle,
    acquire_bundle,
    check_bundle,
    default_registry_dir,
    locate_bundle,
    python_platform_tags,
)

WHEELS_MANIFEST = Path(__file__).with_name("parser_wheels.json")
ALLOWED_HOSTS = frozenset({"files.pythonhosted.org"})
MAX_WHEEL_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 60
CHUNK = 1024 * 1024


class ParserInstallError(HarnessError):
    """Парсер нельзя скачать или собрать; сообщение называет причину, remedy — что делать."""


@dataclass(frozen=True)
class WheelPin:
    """Закреплённый wheel: файл, SHA-256, адрес и пары интерпретатор/платформа."""

    distribution: str
    version: str
    filename: str
    sha256: str
    url: str
    pairs: tuple[str, ...]


@dataclass(frozen=True)
class ParserStatus:
    """Что установлено в registry проекта и пригодно ли это для режима full."""

    registry: Path
    pair: str | None
    source: str | None
    grammars: tuple[str, ...]
    problem: str | None

    @property
    def ready(self) -> bool:
        return self.source is not None and self.problem is None

    @property
    def languages(self) -> tuple[str, ...]:
        """Ключи языков, все грамматики которых есть в проверенном bundle."""
        if not self.ready:
            return ()
        return tuple(
            language.key
            for language in LANGUAGES
            if set(language.grammars) <= set(self.grammars)
        )


@dataclass(frozen=True)
class InstallResult:
    """Итог установки: языки, пара, каталог registry и состояние режима full."""

    languages: tuple[str, ...]
    pair: str
    registry: Path
    ready: bool
    problem: str | None


Fetch = Callable[[WheelPin, Path], None]
Progress = Callable[[str], None]


class Acquire(Protocol):
    """Установка bundle для Repo Map; в тестах заменяется заглушкой без `uv`."""

    def __call__(
        self,
        *,
        repo: Path,
        registry_paths: tuple[str, ...],
        python_executable: str,
        timeout_seconds: int,
    ) -> AppliedBundle | str: ...


def load_pins(manifest: Path = WHEELS_MANIFEST) -> tuple[WheelPin, ...]:
    """Закреплённые wheels из манифеста; любое нарушение формата — ошибка, а не пропуск."""
    try:
        raw: object = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("schema_version")
        pins = []
        for item in cast(list[object], raw["wheels"]):
            if not isinstance(item, dict):
                raise ValueError("wheel")
            fields = cast(dict[str, object], item)
            pairs = fields["pairs"]
            if not isinstance(pairs, list):
                raise ValueError("pairs")
            pins.append(
                WheelPin(
                    str(fields["distribution"]),
                    str(fields["version"]),
                    str(fields["filename"]),
                    str(fields["sha256"]),
                    str(fields["url"]),
                    tuple(str(pair) for pair in pairs),
                )
            )
    except (OSError, ValueError, KeyError, TypeError):
        raise ParserInstallError(
            "Список закреплённых wheels парсеров повреждён",
            remedy="Переустановите или обновите харнесс (harness update).",
        ) from None
    return tuple(pins)


def manifest_pair(pair: str) -> str:
    """Пара интерпретатор/платформа в записи манифеста (macOS называется `macos_arm64`)."""
    python_tag, _, platform = pair.partition("-")
    if platform.startswith("macosx_") and platform.endswith("_arm64"):
        platform = "macos_arm64"
    return f"{python_tag}-{platform}"


def wheels_for(
    pins: Sequence[WheelPin], pair: str, grammars: Sequence[GrammarPin]
) -> list[WheelPin]:
    """Ядро tree-sitter и по одному wheel на дистрибутив выбранных грамматик для пары."""
    wanted = [CORE_DISTRIBUTION, *dict.fromkeys(g.distribution for g in grammars)]
    key = manifest_pair(pair)
    chosen: list[WheelPin] = []
    for distribution in wanted:
        matches = [p for p in pins if p.distribution == distribution and key in p.pairs]
        if not matches:
            raise ParserInstallError(
                f"Для {pair} нет закреплённого wheel {distribution}",
                remedy="Поддерживаются Python 3.12–3.14 на Windows x64, Linux x64 и macOS arm64; "
                "запустите консоль в таком интерпретаторе.",
            )
        chosen.append(matches[0])
    return chosen


def download_wheel(pin: WheelPin, destination: Path) -> None:
    """Скачать wheel по https с хоста PyPI и сверить SHA-256 и размер."""
    parts = urllib.parse.urlsplit(pin.url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ParserInstallError(
            f"Адрес wheel {pin.filename} не принадлежит PyPI",
            remedy="Обновите харнесс: список закреплённых wheels повреждён.",
        )
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(pin.url, headers={"User-Agent": "agent-harness"})
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as source,
            destination.open("wb") as target,
        ):
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > MAX_WHEEL_BYTES:
                    raise ParserInstallError(
                        f"Wheel {pin.filename} больше {MAX_WHEEL_BYTES // 2**20} МБ",
                        remedy="Проверьте закреплённый список wheels.",
                    )
                digest.update(chunk)
                target.write(chunk)
    except OSError as error:
        raise ParserInstallError(
            f"Не удалось скачать {pin.filename}: {error}",
            remedy="Проверьте сеть и доступ к files.pythonhosted.org, затем повторите.",
        ) from None
    if digest.hexdigest() != pin.sha256:
        destination.unlink(missing_ok=True)
        raise ParserInstallError(
            f"SHA-256 wheel {pin.filename} не совпал с закреплённым",
            remedy="Файл подменён или повреждён при загрузке: повторите позже, ничего не установлено.",
        )


def parser_status(
    repo: Path,
    registry_paths: tuple[str, ...] = (),
    python_executable: str = sys.executable,
) -> ParserStatus:
    """Состояние bundle, который увидит Repo Map: каталог, пара, грамматики и причина деградации."""
    registry = default_registry_dir(repo)
    located = locate_bundle(
        repo=repo,
        registry_paths=registry_paths,
        python_executable=python_executable,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    if isinstance(located, str):
        return ParserStatus(registry, None, None, (), located)
    return ParserStatus(
        registry,
        located.pair,
        located.bundle_source,
        tuple(grammar.name for grammar in located.lock.grammars),
        check_bundle(located),
    )


def _existing_grammars(located: LocatedBundle | str, pair: str) -> tuple[str, ...]:
    """Грамматики уже установленного локального bundle той же пары: их нужно сохранить."""
    if (
        isinstance(located, str)
        or located.bundle_source != "local-cache"
        or located.pair != pair
        or check_bundle(located) is not None
    ):
        return ()
    return tuple(grammar.name for grammar in located.lock.grammars)


def _replace_registry(registry: Path, built: Path) -> None:
    """Подменить registry собранным bundle; прежний возвращается, если подмена не удалась."""
    backup = registry.with_name(registry.name + ".previous")
    shutil.rmtree(backup, ignore_errors=True)
    if registry.exists():
        os.replace(registry, backup)
    try:
        os.replace(built, registry)
    except OSError:
        if backup.exists():
            os.replace(backup, registry)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def install_languages(
    repo: Path,
    languages: Sequence[str],
    *,
    registry_paths: tuple[str, ...] = (),
    python_executable: str = sys.executable,
    fetch: Fetch = download_wheel,
    progress: Progress = lambda _line: None,
    pins: Sequence[WheelPin] | None = None,
    acquire: Acquire = acquire_bundle,
) -> InstallResult:
    """Скачать wheels выбранных языков, собрать bundle в registry проекта и проверить режим full."""
    known = {language.key: language for language in LANGUAGES}
    unknown = [key for key in languages if key not in known]
    if unknown or not languages:
        raise ParserInstallError(
            "Неизвестный или не выбранный язык: " + ", ".join(unknown or ["—"]),
            remedy="Допустимы: " + ", ".join(known),
        )
    try:
        python_tag, platform_tag = python_platform_tags(
            python_executable, PROBE_TIMEOUT_SECONDS
        )
    except (OSError, BundleFormatError) as error:
        raise ParserInstallError(
            f"Не удалось определить интерпретатор и платформу: {error}",
            remedy="Проверьте, что интерпретатор запускается.",
        ) from None
    pair = f"{python_tag}-{platform_tag}"
    registry = default_registry_dir(repo)
    located = locate_bundle(
        repo=repo,
        registry_paths=registry_paths,
        python_executable=python_executable,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    names = set(_existing_grammars(located, pair))
    for key in languages:
        names.update(known[key].grammars)
    grammars = [pin for pin in GRAMMARS if pin.name in names]
    wanted = wheels_for(load_pins() if pins is None else pins, pair, grammars)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=registry.parent, prefix=".stage-"
    ) as stage_name:
        stage = Path(stage_name)
        wheelhouse = stage / "wheels"
        wheelhouse.mkdir()
        for index, pin in enumerate(wanted, 1):
            progress(f"[{index}/{len(wanted)}] {pin.filename}")
            fetch(pin, wheelhouse / pin.filename)
        try:
            build_bundle(wheelhouse, stage / "bundle", pair, grammars)
        except BundleBuildError as error:
            raise ParserInstallError(
                f"Не удалось собрать bundle: {error}",
                remedy="Повторите установку; если ошибка остаётся, обновите харнесс.",
            ) from None
        _replace_registry(registry, stage / "bundle")
    applied = acquire(
        repo=repo,
        registry_paths=registry_paths,
        python_executable=python_executable,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    installed = tuple(
        key for key, language in known.items() if set(language.grammars) <= names
    )
    return InstallResult(
        installed,
        pair,
        registry,
        not isinstance(applied, str),
        applied if isinstance(applied, str) else None,
    )
