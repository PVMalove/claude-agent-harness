"""Чтение входа Repo Map из объектов Git закреплённого коммита.

Рабочее дерево и untracked-файлы не читаются: пути берутся из `git ls-tree`, а содержимое — из
объектов коммита. Размеры и содержимое всех файлов читаются двумя процессами `git cat-file` на весь
запрос, а не отдельными процессами на каждый файл.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.errors import HarnessError

# One `git cat-file` batch gets the policy timeout once per this many objects it reads.
BLOBS_PER_TIMEOUT = 1000
# A full object id: SHA-1 (40 hex) or SHA-256 (64 hex) repositories.
_OBJECT_ID_RE = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")


class RepoMapGitError(HarnessError):
    """Git-команда Repo Map завершилась ошибкой или превысила таймаут."""


@dataclass(frozen=True)
class Blob:
    """Файл коммита: размер и содержимое; `content` равен `None`, если объект не прочитан."""

    size: int | None
    content: bytes | None


def run_git(repo: Path, timeout_seconds: int, *args: str, stdin: bytes | None = None) -> bytes:
    """Выполнить Git-команду в `repo` с таймаутом и вернуть stdout.

    Ошибка или превышение таймаута поднимают `RepoMapGitError` с рекомендацией.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoMapGitError(
            f"git {' '.join(args)} timed out after {timeout_seconds} seconds",
            remedy="raise repo_map_policy.timeout_seconds or retry on a less loaded machine",
        ) from exc
    if result.returncode:
        raise RepoMapGitError(
            result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed",
            remedy="check that --repo is a Git repository and --commit names an existing commit",
        )
    return result.stdout


def resolve_commit(repo: Path, commit: str, timeout_seconds: int) -> str:
    """Разрешить ссылку на коммит в полный SHA."""
    return run_git(
        repo, timeout_seconds, "rev-parse", "--verify", f"{commit}^{{commit}}"
    ).decode().strip()


def tracked_paths(repo: Path, commit: str, timeout_seconds: int) -> list[str]:
    """Отсортированные пути всех tracked-файлов коммита."""
    raw = run_git(repo, timeout_seconds, "ls-tree", "-rz", "--name-only", commit)
    return sorted(
        path.decode("utf-8", "surrogateescape") for path in raw.split(b"\0") if path
    )


def _object_names(commit: str, paths: list[str]) -> bytes:
    """Входные строки `git cat-file` для путей коммита."""
    return "".join(f"{commit}:{path}\n" for path in paths).encode("utf-8", "surrogateescape")


def _batch_timeout(timeout_seconds: int, count: int) -> int:
    """Таймаут одного пакетного `git cat-file` с запасом на число объектов."""
    return timeout_seconds * (1 + count // BLOBS_PER_TIMEOUT)


def _read_header(output: bytes, position: int) -> tuple[tuple[bytes, int] | None, int]:
    """Прочитать заголовок ответа `git cat-file` и позицию после него.

    Найденный объект даёт `(тип, размер)`; строки вида `<имя> missing` или `<имя> ambiguous` дают
    `None` (имя может содержать пробелы, поэтому разбирается только форма `<sha> <тип> <размер>`).
    """
    end = output.index(b"\n", position)
    fields = output[position:end].split(b" ")
    found = (
        len(fields) == 3
        and _OBJECT_ID_RE.fullmatch(fields[0]) is not None
        and fields[2].isdigit()
    )
    return ((fields[1], int(fields[2])) if found else None), end + 1


def blob_sizes(repo: Path, commit: str, paths: list[str], timeout_seconds: int) -> dict[str, int | None]:
    """Размеры blob'ов по путям одним `git cat-file --batch-check`; `None` для не-blob объектов."""
    if not paths:
        return {}
    output = run_git(
        repo,
        _batch_timeout(timeout_seconds, len(paths)),
        "cat-file",
        "--batch-check",
        stdin=_object_names(commit, paths),
    )
    sizes: dict[str, int | None] = {}
    position = 0
    for path in paths:
        header, position = _read_header(output, position)
        sizes[path] = header[1] if header is not None and header[0] == b"blob" else None
    return sizes


def blob_contents(repo: Path, commit: str, paths: list[str], timeout_seconds: int) -> dict[str, bytes | None]:
    """Содержимое blob'ов по путям одним `git cat-file --batch`; `None` для не-blob объектов."""
    if not paths:
        return {}
    output = run_git(
        repo,
        _batch_timeout(timeout_seconds, len(paths)),
        "cat-file",
        "--batch",
        stdin=_object_names(commit, paths),
    )
    contents: dict[str, bytes | None] = {}
    position = 0
    for path in paths:
        header, position = _read_header(output, position)
        if header is None:
            contents[path] = None
            continue
        kind, size = header
        body = output[position : position + size]
        position += size + 1
        contents[path] = body if kind == b"blob" else None
    return contents


def read_blobs(
    repo: Path, commit: str, paths: list[str], *, max_file_bytes: int, timeout_seconds: int
) -> dict[str, Blob]:
    """Прочитать размеры всех файлов и содержимое тех, что не больше `max_file_bytes`.

    Путь с переводом строки нельзя передать в пакетный режим `git cat-file`, поэтому его содержимое
    не читается. Подмодули и другие не-blob объекты получают `Blob(None, None)`.
    """
    batchable = [path for path in paths if "\n" not in path]
    sizes = blob_sizes(repo, commit, batchable, timeout_seconds)
    small = [
        path for path in batchable
        if (size := sizes[path]) is not None and size <= max_file_bytes
    ]
    contents = blob_contents(repo, commit, small, timeout_seconds)
    return {
        path: Blob(sizes.get(path), contents.get(path))
        for path in paths
    }
