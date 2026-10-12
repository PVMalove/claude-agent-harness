"""Сборка offline parser bundle из подготовленного wheelhouse (общая для скрипта и консоли).

Функция ничего не скачивает и не устанавливает: wheels уже лежат в `wheelhouse`. Набор грамматик
можно сузить, тогда в lock и wheelhouse попадают только они и ядро tree-sitter."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Sequence

from harness.repo_map.bundle_pins import (
    CORE_ABI_RANGE,
    CORE_DISTRIBUTION,
    CORE_VERSION,
    GRAMMARS,
    GrammarPin,
)

WORKER = Path(__file__).with_name("tree_sitter_worker.py")


class BundleBuildError(ValueError):
    """Wheelhouse не содержит ровно закреплённых wheels."""


def sha256_file(path: Path) -> str:
    """SHA-256 содержимого файла."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def single_wheel(wheelhouse: Path, distribution: str, version: str) -> Path:
    """Найти единственный wheel дистрибутива нужной версии."""
    matches = sorted(wheelhouse.glob(f"{distribution}-{version}-*.whl"))
    if len(matches) != 1:
        raise BundleBuildError(
            f"expected exactly one {distribution}=={version} wheel in {wheelhouse}, "
            f"found {len(matches)}"
        )
    return matches[0]


def build_bundle(
    wheelhouse: Path,
    out: Path,
    pair: str,
    grammars: Sequence[GrammarPin] = GRAMMARS,
    worker: Path = WORKER,
) -> Path:
    """Скопировать wheels и worker в `out` и записать lock для пары `pair`; вернуть путь к lock."""
    core = single_wheel(wheelhouse, CORE_DISTRIBUTION, CORE_VERSION)
    grammar_wheels = {
        pin.name: single_wheel(wheelhouse, pin.distribution, pin.version)
        for pin in grammars
    }
    target = out / "wheelhouse" / pair
    target.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for wheel in dict.fromkeys((core, *grammar_wheels.values())):
        shutil.copyfile(wheel, target / wheel.name)
        artifacts.append({"filename": wheel.name, "sha256": sha256_file(wheel)})
    shutil.copyfile(worker, out / worker.name)
    lock = {
        "core_version": CORE_VERSION,
        "core_abi_range": CORE_ABI_RANGE,
        "worker_script": worker.name,
        "script_sha256": sha256_file(out / worker.name),
        "grammars": [
            {
                "name": pin.name,
                "distribution": pin.distribution,
                "version": pin.version,
                "abi": pin.abi,
                "sha256": sha256_file(grammar_wheels[pin.name]),
                "extensions": list(pin.extensions),
            }
            for pin in grammars
        ],
        "wheelhouses": {pair: artifacts},
    }
    lock_path = out / "parser_bundle.lock.json"
    lock_path.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return lock_path
