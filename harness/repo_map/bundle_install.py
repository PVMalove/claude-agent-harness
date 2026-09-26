"""Offline-установка проверенного parser bundle через `uv` в общий каталог кэша.

Установка выполняется один раз для всех процессов и worktree, разделяющих каталог: межпроцессная
блокировка сериализует параллельные запуски, а маркер с SHA-256 lock делает повторный вызов
бесплатным. Сеть, `pip` и конфигурация окружения не используются.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from harness.repo_map.bundle_lock import BundleLock, wheel_name_version

INSTALL_MARKER_FILENAME = ".install-complete"
INSTALL_LOCK_FILENAME = ".install.lock"
# Length of the lock-hash prefix that names a per-lock, per-pair install directory.
INSTALL_DIR_HASH_PREFIX = 16

# How often a process waiting for the shared install lock retries, and how many install timeouts it
# waits in total before giving up (one for a concurrent install plus one for its own).
LOCK_POLL_SECONDS = 0.05
INSTALL_LOCK_TIMEOUT_FACTOR = 2


def install_dir_name(lock: BundleLock, pair: str) -> str:
    """Имя каталога установки bundle для lock и пары интерпретатор/платформа."""
    return f"{lock.raw_sha256[:INSTALL_DIR_HASH_PREFIX]}-{pair}"


class BundleInstallError(RuntimeError):
    """Установка `uv pip install --offline --no-index --target` завершилась ошибкой или неожиданным результатом."""


class UvUnavailableError(BundleInstallError):
    """`uv` не найден в PATH: harness устанавливает bundle только через uv, а не через pip."""


def _lock_first_byte(handle: BinaryIO, *, acquire: bool) -> None:
    """Захватить без ожидания или освободить первый байт файла (Windows и POSIX)."""
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


@contextmanager
def _installation_lock(path: Path, timeout_seconds: int) -> Iterator[None]:
    """Держать межпроцессную блокировку на время установки общего bundle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _lock_first_byte(handle, acquire=True)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise BundleInstallError("parser bundle installation lock failed") from exc
                if time.monotonic() >= deadline:
                    raise BundleInstallError("parser bundle installation lock timed out") from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _lock_first_byte(handle, acquire=False)


def install_bundle(
    lock: BundleLock,
    wheelhouse_dir: Path,
    install_dir: Path,
    python_executable: str,
    *,
    pair: str,
    timeout_seconds: int,
) -> None:
    """Установить bundle один раз для всех процессов, разделяющих каталог кэша."""
    try:
        with _installation_lock(
            install_dir.parent / INSTALL_LOCK_FILENAME,
            timeout_seconds * INSTALL_LOCK_TIMEOUT_FACTOR,
        ):
            _install_bundle_unlocked(
                lock, wheelhouse_dir, install_dir, python_executable,
                pair=pair, timeout_seconds=timeout_seconds,
            )
    except OSError as exc:
        raise BundleInstallError("parser bundle installation failed") from exc


def _requirements_text(lock: BundleLock, pair: str) -> str:
    """Сформировать requirements с хешами для артефактов пары."""
    lines = []
    for artifact in lock.wheelhouses.get(pair, ()):
        name, version = wheel_name_version(artifact.filename)
        lines.append(f"{name}=={version} --hash=sha256:{artifact.sha256}")
    return "\n".join(lines) + "\n"


def _uv_install_command(
    uv: str, wheelhouse_dir: Path, python_executable: str, install_dir: Path, requirements: Path
) -> list[str]:
    """Команда offline-установки: только локальный wheelhouse, хеши обязательны, только wheels."""
    return [
        uv,
        "pip",
        "install",
        "--offline",
        "--no-config",
        "--no-cache",
        "--no-index",
        "--find-links",
        str(wheelhouse_dir),
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--python",
        python_executable,
        "--target",
        str(install_dir),
        "--requirement",
        str(requirements),
    ]


def _isolated_environment() -> dict[str, str]:
    """Окружение процесса без `UV_*` и `PIP_*`, которые могли бы перенаправить установку.

    `--no-config` отключает uv.toml и pyproject; очистка переменных закрывает остальное (например,
    UV_INDEX_URL, UV_FIND_LINKS, UV_CONFIG_FILE), так что окружение родителя не может повлиять на
    offline-установку с `--no-index`.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith(("UV_", "PIP_"))}


def _install_bundle_unlocked(
    lock: BundleLock,
    wheelhouse_dir: Path,
    install_dir: Path,
    python_executable: str,
    *,
    pair: str,
    timeout_seconds: int,
) -> None:
    """Установить проверенные wheels через uv; вызывать под межпроцессной блокировкой."""
    marker = install_dir / INSTALL_MARKER_FILENAME
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == lock.raw_sha256:
        return
    uv = shutil.which("uv")
    if uv is None:
        raise UvUnavailableError("uv executable not found on PATH")
    install_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = install_dir / ".requirements.txt"
    requirements_path.write_text(_requirements_text(lock, pair), encoding="utf-8")
    try:
        result = subprocess.run(
            _uv_install_command(uv, wheelhouse_dir, python_executable, install_dir, requirements_path),
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=_isolated_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise BundleInstallError("uv pip install timed out") from exc
    finally:
        requirements_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise BundleInstallError(result.stderr.decode("utf-8", "replace").strip())
    marker.write_text(lock.raw_sha256, encoding="utf-8")
