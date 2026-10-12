#!/usr/bin/env python3
"""Собрать offline parser bundle Repo Map из заранее скачанного wheelhouse.

Только для сборки: скрипт копирует wheels и tree-sitter worker, хеширует их и пишет
`parser_bundle.lock.json` в формате, который проверяет harness/repo_map/parser_bundle.py. Он ничего
не скачивает, не разрешает зависимости и не устанавливает: wheelhouse уже должен содержать ровно
закреплённые ниже wheels для одной пары интерпретатор/платформа (ADR 0008). Версии меняются только
вместе с релизом harness.

Использование: build_parser_bundle.py --wheelhouse DIR --out DIR [--pair cpXY-platform]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.repo_map.bundle_build import (  # noqa: E402
    WORKER,
    BundleBuildError,
    build_bundle,
    sha256_file,
    single_wheel,
)
from harness.repo_map.bundle_pins import (  # noqa: E402
    CORE_ABI_RANGE,
    CORE_DISTRIBUTION,
    CORE_VERSION,
    GRAMMARS,
    GrammarPin,
)

__all__ = [
    "CORE_ABI_RANGE",
    "CORE_DISTRIBUTION",
    "CORE_VERSION",
    "GRAMMARS",
    "WORKER",
    "GrammarPin",
    "build",
    "main",
]
# Bound for the interpreter probe that names the running cpXY-platform pair.
PROBE_TIMEOUT_SECONDS = 60


def _sha256(path: Path) -> str:
    """SHA-256 содержимого файла."""
    return sha256_file(path)


def _running_pair() -> str:
    """Пара интерпретатор/платформа текущего Python в формате `cpXY-platform`."""
    script = (
        "import sys, sysconfig;"
        "print(f'cp{sys.version_info[0]}{sys.version_info[1]}-'"
        " + sysconfig.get_platform().replace('-', '_').replace('.', '_'))"
    )
    return subprocess.check_output(
        [sys.executable, "-c", script], text=True, timeout=PROBE_TIMEOUT_SECONDS
    ).strip()


def _single_wheel(wheelhouse: Path, distribution: str, version: str) -> Path:
    """Найти единственный wheel дистрибутива нужной версии или завершить скрипт с ошибкой."""
    try:
        return single_wheel(wheelhouse, distribution, version)
    except BundleBuildError as error:
        raise SystemExit(str(error)) from None


def build(wheelhouse: Path, out: Path, pair: str) -> Path:
    """Скопировать wheels и worker в `out` и записать lock для пары `pair`; вернуть путь к lock."""
    try:
        return build_bundle(wheelhouse, out, pair)
    except BundleBuildError as error:
        raise SystemExit(str(error)) from None


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: собрать bundle и напечатать путь к lock."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--pair", help="interpreter/platform pair; defaults to this interpreter"
    )
    args = parser.parse_args(argv)
    lock_path = build(args.wheelhouse, args.out, args.pair or _running_pair())
    print(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
