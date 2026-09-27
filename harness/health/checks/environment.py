"""Group 'environment': cross-platform checks of the tools and settings around the repository
(ticket #343) - git and its identity, line endings, Python, uv, this repository's dev environment,
and the output encoding.

Every external tool is resolved with `shutil.which` and invoked by that full path, so a `.cmd`
shim on Windows PATH is found the same way the shell would find it. A missing tool or a tool that
cannot run is a result (`fail`/`skipped`), never an exception: health must report every check.
"""

from __future__ import annotations

import codecs
import locale
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from ..context import HealthContext
from ..model import CheckResult, Fix
from ..process import run_tool

GROUP = "environment"

MIN_PYTHON: tuple[int, int] = (3, 12)
# The canonical harness source repository: the only project whose dev environment health syncs.
HARNESS_PROJECT_NAME = "claude-agent-harness"
DEV_VENV_REL = Path(".harness/.venv")

_TOOL_TIMEOUT_SECONDS = 60
_MAX_LISTED_FILES = 20
_TEXT_EOLS = frozenset({"lf", "crlf"})

_GIT_INSTALL_FIX = Fix(text="установите git: https://git-scm.com/downloads")
_UV_INSTALL_FIX = Fix(
    text="установите uv: https://docs.astral.sh/uv/getting-started/installation/"
)


def _run(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run a local tool with this group's timeout; None when it cannot start or finish."""
    return run_tool(argv, timeout=_TOOL_TIMEOUT_SECONDS, cwd=cwd, env=env)


def _git(
    context: HealthContext, *arguments: str
) -> subprocess.CompletedProcess[str] | None:
    """Invoke git against the explicit repository only (same trust rule as project_files.git_command)."""
    executable = shutil.which("git")
    if executable is None:
        return None
    checkout = context.repo.resolve()
    return _run(
        [
            executable,
            "-c",
            f"safe.directory={checkout}",
            "-C",
            str(checkout),
            *arguments,
        ]
    )


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _last_line(text: str) -> str:
    return text.strip().splitlines()[-1] if text.strip() else ""


def _tool_version(name: str) -> str | None:
    """`<name> --version`'s first line, or None when the tool is absent or broken."""
    executable = shutil.which(name)
    if executable is None:
        return None
    result = _run([executable, "--version"])
    if result is None or result.returncode != 0:
        return None
    return _first_line(result.stdout) or name


def _is_git_worktree(context: HealthContext) -> bool:
    result = _git(context, "rev-parse", "--is-inside-work-tree")
    return (
        result is not None
        and result.returncode == 0
        and result.stdout.strip() == "true"
    )


# --- git ---------------------------------------------------------------------------------------


def check_os(_context: HealthContext) -> CheckResult:
    """Informational: the OS and its version, so a report shows which platform rules applied."""
    release = platform.release()
    version = platform.version()
    details = " ".join(part for part in (platform.system() or os.name, release) if part)
    if version and version != release:
        details = f"{details} ({version})"
    return CheckResult(id="environment.os", group=GROUP, status="ok", message=f"ОС: {details}")


def check_git(_context: HealthContext) -> CheckResult:
    version = _tool_version("git")
    if version is None:
        return CheckResult(
            id="environment.git",
            group=GROUP,
            status="fail",
            message="git не найден в PATH или не запускается",
            fix=_GIT_INSTALL_FIX,
        )
    return CheckResult(id="environment.git", group=GROUP, status="ok", message=version)


def _git_config(context: HealthContext, key: str) -> str | None:
    result = _git(context, "config", "--get", key)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def check_git_identity(context: HealthContext) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult(
            id="environment.git_identity",
            group=GROUP,
            status="skipped",
            message="git недоступен: user.name/user.email не проверены",
        )
    placeholders = {"user.name": "Имя Фамилия", "user.email": "you@example.com"}
    missing = [key for key in placeholders if _git_config(context, key) is None]
    if not missing:
        return CheckResult(
            id="environment.git_identity",
            group=GROUP,
            status="ok",
            message="git user.name и user.email заданы",
        )
    return CheckResult(
        id="environment.git_identity",
        group=GROUP,
        status="warn",
        message=f"в git не заданы: {', '.join(missing)}",
        fix=Fix(
            text="задайте автора коммитов",
            command=" && ".join(
                f'git config --global {key} "{placeholders[key]}"' for key in missing
            ),
        ),
    )


# --- line endings ------------------------------------------------------------------------------


def check_gitattributes(context: HealthContext) -> CheckResult:
    if (context.repo / ".gitattributes").is_file():
        return CheckResult(
            id="environment.gitattributes",
            group=GROUP,
            status="ok",
            message=".gitattributes есть",
        )
    return CheckResult(
        id="environment.gitattributes",
        group=GROUP,
        status="warn",
        message="нет .gitattributes: переводы строк зависят от core.autocrlf каждой машины",
        fix=Fix(
            text="добавьте .gitattributes с явной нормализацией, например строку `* text=auto eol=lf`, "
            "и закоммитьте его"
        ),
    )


def eol_mismatch(index_eol: str, worktree_eol: str, attributes: str) -> bool:
    """Whether one `git ls-files --eol` entry diverges from what its attributes declare.

    Only files git treats as text by attribute are judged; without a text/eol attribute the line
    ending legitimately depends on core.autocrlf, which health reports as information only.
    Divergence is: mixed line endings, CRLF committed to the index of a normalized text file, or a
    worktree ending that differs from an explicit `eol=`.
    """
    tokens = attributes.split()
    if not tokens or "-text" in tokens or "binary" in tokens:
        return False
    declared_eol = next(
        (token.removeprefix("eol=") for token in tokens if token.startswith("eol=")),
        None,
    )
    is_text = declared_eol is not None or any(
        token == "text" or token.startswith("text=") for token in tokens
    )
    if not is_text:
        return False
    if "mixed" in (index_eol, worktree_eol):
        return True
    if index_eol == "crlf":
        return True
    return (
        declared_eol is not None
        and worktree_eol in _TEXT_EOLS
        and worktree_eol != declared_eol
    )


def parse_ls_files_eol(output: str) -> list[tuple[str, str, str, str]]:
    """Parse `git ls-files --eol -z` into (index_eol, worktree_eol, attributes, path) tuples."""
    entries: list[tuple[str, str, str, str]] = []
    for record in output.split("\0"):
        if "\t" not in record:
            continue
        info, path = record.split("\t", 1)
        fields = info.split(None, 2)
        index_eol = worktree_eol = attributes = ""
        for field in fields:
            if field.startswith("i/"):
                index_eol = field[2:]
            elif field.startswith("w/"):
                worktree_eol = field[2:]
            elif field.startswith("attr/"):
                attributes = field[5:].strip()
        entries.append((index_eol, worktree_eol, attributes, path))
    return entries


def check_line_endings(context: HealthContext) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult(
            id="environment.line_endings",
            group=GROUP,
            status="skipped",
            message="git недоступен: переводы строк не проверены",
        )
    if not _is_git_worktree(context):
        return CheckResult(
            id="environment.line_endings",
            group=GROUP,
            status="skipped",
            message="не git-репозиторий: переводы строк не проверены",
        )
    autocrlf = _git_config(context, "core.autocrlf") or "не задан"
    info = f"core.autocrlf={autocrlf} (информация)"
    result = _git(context, "ls-files", "--eol", "-z")
    if result is None or result.returncode != 0:
        detail = (
            _last_line(result.stderr) if result is not None else "git не запустился"
        )
        return CheckResult(
            id="environment.line_endings",
            group=GROUP,
            status="skipped",
            message=f"git ls-files --eol не выполнился ({detail}); {info}",
        )
    diverged = [
        path
        for index_eol, worktree_eol, attributes, path in parse_ls_files_eol(
            result.stdout
        )
        if eol_mismatch(index_eol, worktree_eol, attributes)
    ]
    if not diverged:
        return CheckResult(
            id="environment.line_endings",
            group=GROUP,
            status="ok",
            message=f"переводы строк не расходятся с атрибутами git; {info}",
        )
    listed = ", ".join(diverged[:_MAX_LISTED_FILES])
    if len(diverged) > _MAX_LISTED_FILES:
        listed += f" и ещё {len(diverged) - _MAX_LISTED_FILES}"
    return CheckResult(
        id="environment.line_endings",
        group=GROUP,
        status="warn",
        message=f"переводы строк расходятся с атрибутами git в {len(diverged)} файл(ах): {listed}; {info}",
        fix=Fix(
            text="нормализуйте файлы в индексе и закоммитьте результат; рабочие копии с чужими "
            "переводами строк извлеките заново после сохранения своих изменений",
            command="git add --renormalize .",
        ),
    )


# --- Python and uv -----------------------------------------------------------------------------


def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def check_python(_context: HealthContext) -> CheckResult:
    version = _python_version()
    shown = ".".join(str(part) for part in version)
    required = ".".join(str(part) for part in MIN_PYTHON)
    if version[:2] < MIN_PYTHON:
        return CheckResult(
            id="environment.python",
            group=GROUP,
            status="fail",
            message=f"Python {shown} ({sys.executable}) ниже требуемого {required}",
            fix=Fix(
                text=f"установите Python {required} или новее и запускайте harness этим интерпретатором"
            ),
        )
    return CheckResult(
        id="environment.python",
        group=GROUP,
        status="ok",
        message=f"Python {shown} ({sys.executable})",
    )


def check_uv(_context: HealthContext) -> CheckResult:
    version = _tool_version("uv")
    if version is None:
        return CheckResult(
            id="environment.uv",
            group=GROUP,
            status="fail",
            message="uv не найден в PATH или не запускается",
            fix=_UV_INSTALL_FIX,
        )
    return CheckResult(id="environment.uv", group=GROUP, status="ok", message=version)


def is_harness_source_repo(repo: Path) -> bool:
    """Whether `repo` is the canonical harness repository itself (pyproject name plus uv.lock)."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file() or not (repo / "uv.lock").is_file():
        return False
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == HARNESS_PROJECT_NAME


def _dev_sync_command() -> str:
    if os.name == "nt":
        return "$env:UV_PROJECT_ENVIRONMENT='.harness\\.venv'; uv sync --locked"
    return "UV_PROJECT_ENVIRONMENT=.harness/.venv uv sync --locked"


def check_dev_environment(context: HealthContext) -> CheckResult:
    if not is_harness_source_repo(context.repo):
        return CheckResult(
            id="environment.dev_env",
            group=GROUP,
            status="skipped",
            message=f"не репозиторий {HARNESS_PROJECT_NAME}: синхронность dev-окружения не проверяется",
        )
    executable = shutil.which("uv")
    if executable is None:
        return CheckResult(
            id="environment.dev_env",
            group=GROUP,
            status="skipped",
            message="uv недоступен: синхронность dev-окружения не проверена",
        )
    venv = context.repo / DEV_VENV_REL
    argv = [executable, "sync", "--locked", "--check"]
    if not context.online:
        argv.append("--offline")
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv))
    result = _run(argv, cwd=context.repo, env=env)
    if result is not None and result.returncode == 0:
        return CheckResult(
            id="environment.dev_env",
            group=GROUP,
            status="ok",
            message=f"{DEV_VENV_REL.as_posix()} синхронизировано с uv.lock",
        )
    detail = (
        _last_line(result.stderr) or _last_line(result.stdout)
        if result is not None
        else "uv sync --check не завершился"
    )
    return CheckResult(
        id="environment.dev_env",
        group=GROUP,
        status="warn",
        message=f"{DEV_VENV_REL.as_posix()} не синхронизировано с uv.lock: {detail or 'без подробностей'}",
        fix=Fix(
            text="синхронизируйте dev-окружение по uv.lock (или выполните make bootstrap)",
            command=_dev_sync_command(),
        ),
    )


# --- output encoding ---------------------------------------------------------------------------


def _is_utf8(encoding: str | None) -> bool:
    if not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def check_output_encoding(context: HealthContext) -> CheckResult:
    """The stream encoding the caller saw before any in-process reconfiguration (see
    HealthContext.output_encoding) and the locale encoding child processes inherit."""
    stream = context.output_encoding
    if stream is None:
        stream = getattr(sys.stdout, "encoding", None)
    preferred = locale.getpreferredencoding(False)
    shown = f"вывод: {stream or 'неизвестна'}, локаль: {preferred or 'неизвестна'}"
    if _is_utf8(stream) and _is_utf8(preferred):
        return CheckResult(
            id="environment.output_encoding",
            group=GROUP,
            status="ok",
            message=f"кодировка UTF-8 ({shown})",
        )
    return CheckResult(
        id="environment.output_encoding",
        group=GROUP,
        status="warn",
        message=f"кодировка не UTF-8 ({shown}): кириллица и метки статуса могут искажаться",
        fix=Fix(
            text="включите UTF-8 режим Python для пользователя и перезапустите терминал",
            command="setx PYTHONUTF8 1" if os.name == "nt" else "export PYTHONUTF8=1",
        ),
    )
