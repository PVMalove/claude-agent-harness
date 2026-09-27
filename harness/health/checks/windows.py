"""Windows-only checks of the group 'environment' (ticket #344): the long-path setting, the
repository path's headroom under MAX_PATH, pytest's per-user temp root, the right to create
symlinks, and which `bash` the project hooks (`bash "<hook>.sh"` in .claude/settings.local.json)
resolve to.

Outside Windows every check here is `skipped`. Nothing on the machine is changed: the registry is
only read, and the only writes are a probe file and a probe symlink inside temporary directories,
both removed before the check returns.
"""

from __future__ import annotations

import getpass
import ntpath
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from ..context import HealthContext
from ..model import CheckResult, Fix
from . import files

GROUP = "environment"

# Windows MAX_PATH counts the terminating NUL, so a full path holds at most 259 characters.
MAX_PATH = 260
# Room a checkout needs below its root: `\.harness\.sandboxes\worktrees\<issue-branch>\` (about
# 70 characters for a typical branch) plus the deepest tracked file (about 85 characters today).
MIN_PATH_HEADROOM = 160

_FILESYSTEM_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"
_LONG_PATHS_COMMAND = (
    "New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
    "-Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
)
_DEVELOPER_MODE_COMMAND = (
    'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AppModelUnlock" '
    "/t REG_DWORD /f /v AllowDevelopmentWithoutDevLicense /d 1"
)
_GIT_FOR_WINDOWS_URL = "https://git-scm.com/download/win"


def _is_windows() -> bool:
    return os.name == "nt"


def _skipped(check_id: str, what: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        group=GROUP,
        status="skipped",
        message=f"не Windows: {what} не проверяется",
    )


# --- LongPathsEnabled --------------------------------------------------------------------------


def _long_paths_enabled() -> int | None:
    """HKLM\\...\\FileSystem\\LongPathsEnabled, or None when the value is absent or unreadable."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _FILESYSTEM_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return None
    return value if isinstance(value, int) else None


def check_long_paths(_context: HealthContext) -> CheckResult:
    if not _is_windows():
        return _skipped("environment.long_paths", "LongPathsEnabled")
    value = _long_paths_enabled()
    if value == 1:
        return CheckResult(
            id="environment.long_paths",
            group=GROUP,
            status="ok",
            message="LongPathsEnabled=1: длинные пути включены",
        )
    shown = "не задан" if value is None else str(value)
    return CheckResult(
        id="environment.long_paths",
        group=GROUP,
        status="warn",
        message=f"LongPathsEnabled {shown}: пути длиннее {MAX_PATH - 1} символов недоступны "
        "большинству инструментов",
        fix=Fix(
            text="включите длинные пути в PowerShell от имени администратора и перезапустите "
            "терминал; для git дополнительно: git config --global core.longpaths true",
            command=_LONG_PATHS_COMMAND,
        ),
    )


# --- repository path length --------------------------------------------------------------------


def path_headroom(repo_path: str) -> int:
    """Characters left for paths below `repo_path` before MAX_PATH (separator and NUL counted)."""
    return MAX_PATH - 1 - len(repo_path.rstrip("\\/")) - 1


def check_path_length(context: HealthContext) -> CheckResult:
    """Epic #341 lists the repository root length against 260 outside its Windows-only block, so the
    length and headroom are reported on every OS; only Windows, where MAX_PATH applies, can warn."""
    repo_path = str(context.repo.resolve())
    headroom = path_headroom(repo_path)
    shown = (
        f"путь репозитория {len(repo_path)} символов, запас до {MAX_PATH}: {headroom}"
    )
    if not _is_windows():
        return CheckResult(
            id="environment.path_length",
            group=GROUP,
            status="ok",
            message=f"{shown} (MAX_PATH ограничивает только Windows)",
        )
    if headroom >= MIN_PATH_HEADROOM:
        return CheckResult(
            id="environment.path_length",
            group=GROUP,
            status="ok",
            message=shown,
        )
    return CheckResult(
        id="environment.path_length",
        group=GROUP,
        status="warn",
        message=f"{shown} (нужно не меньше {MIN_PATH_HEADROOM}): глубокие файлы worktree и "
        ".venv могут не создаться",
        fix=Fix(
            text="перенесите репозиторий ближе к корню диска (например, C:\\src\\<имя>) "
            "и включите LongPathsEnabled"
        ),
    )


# --- pytest-of-<user> --------------------------------------------------------------------------


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (ImportError, OSError, KeyError):
        return "unknown"


def pytest_temp_root(temproot: str, user: str) -> Path:
    """The per-user directory pytest's tmp_path factory uses (see _pytest/tmpdir.py)."""
    return Path(temproot) / f"pytest-of-{user or 'unknown'}"


def _temproot() -> str:
    return os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()


class Owner(NamedTuple):
    """A file owner as `DOMAIN\\name`; `is_user` is False for a group such as Administrators."""

    name: str
    is_user: bool


_SID_TYPE_USER = 1


def _path_owner(path: Path) -> Owner | None:
    """The file owner via advapi32, or None when it cannot be read."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_info.restype = wintypes.DWORD
    lookup = advapi32.LookupAccountSidW
    lookup.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    lookup.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    se_file_object = 1
    owner_security_information = 1
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = get_info(
        str(path),
        se_file_object,
        owner_security_information,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    try:
        name = ctypes.create_unicode_buffer(256)
        domain = ctypes.create_unicode_buffer(256)
        name_size = wintypes.DWORD(len(name))
        domain_size = wintypes.DWORD(len(domain))
        sid_use = wintypes.DWORD()
        if not lookup(
            None,
            owner,
            name,
            ctypes.byref(name_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        ):
            return None
        account = f"{domain.value}\\{name.value}" if domain.value else name.value
        return Owner(account, sid_use.value == _SID_TYPE_USER)
    finally:
        kernel32.LocalFree(descriptor)


def is_other_user(owner: Owner, user: str) -> bool:
    """Whether `owner` is a user account other than `user`. A group owner (an elevated
    administrator's files belong to BUILTIN\\Administrators) is left to the write probe."""
    return (
        owner.is_user
        and ntpath.basename(owner.name).casefold() != ntpath.basename(user).casefold()
    )


def _is_writable(directory: Path) -> bool:
    """Probe by creating a file: os.access ignores Windows ACLs."""
    try:
        handle, probe = tempfile.mkstemp(dir=directory, prefix="harness-health-")
    except OSError:
        return False
    os.close(handle)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def _pytest_temp_fix(root: Path) -> Fix:
    return Fix(
        text="удалите каталог из сессии его владельца или от имени администратора — pytest "
        "создаст его заново; либо задайте свой каталог через PYTEST_DEBUG_TEMPROOT",
        command=f"Remove-Item -Recurse -Force '{root}'",
    )


def check_pytest_temp(_context: HealthContext) -> CheckResult:
    if not _is_windows():
        return _skipped("environment.pytest_temp", "каталог pytest-of-<user>")
    user = _current_user()
    temproot = _temproot()
    root = pytest_temp_root(temproot, user)
    if not root.exists():
        if _is_writable(Path(temproot)):
            return CheckResult(
                id="environment.pytest_temp",
                group=GROUP,
                status="ok",
                message=f"{root} ещё не создан; {temproot} записываем",
            )
        return CheckResult(
            id="environment.pytest_temp",
            group=GROUP,
            status="warn",
            message=f"временный каталог {temproot} не записываем: pytest не создаст {root.name}",
            fix=Fix(
                text="укажите записываемый каталог в TEMP/TMP или PYTEST_DEBUG_TEMPROOT"
            ),
        )
    if not root.is_dir():
        return CheckResult(
            id="environment.pytest_temp",
            group=GROUP,
            status="warn",
            message=f"{root} существует, но это не каталог",
            fix=_pytest_temp_fix(root),
        )
    owner = _path_owner(root)
    owner_name = owner.name if owner is not None else "не определён"
    foreign = owner is not None and is_other_user(owner, user)
    writable = _is_writable(root)
    if foreign or not writable:
        problems = [f"принадлежит {owner_name}, а не {user}"] if foreign else []
        if not writable:
            problems.append(
                "не записываем"
                if foreign
                else f"не записываем (владелец: {owner_name})"
            )
        return CheckResult(
            id="environment.pytest_temp",
            group=GROUP,
            status="warn",
            message=f"{root} {' и '.join(problems)}",
            fix=_pytest_temp_fix(root),
        )
    return CheckResult(
        id="environment.pytest_temp",
        group=GROUP,
        status="ok",
        message=f"{root} записываем, владелец: {owner_name}",
    )


# --- symlinks ----------------------------------------------------------------------------------


def _symlink_error() -> str | None:
    """Create and remove a probe directory symlink in a temp directory; the error or None."""
    with tempfile.TemporaryDirectory(
        prefix="harness-health-", ignore_cleanup_errors=True
    ) as scratch:
        target = Path(scratch) / "target"
        target.mkdir()
        link = Path(scratch) / "link"
        try:
            os.symlink("target", link, target_is_directory=True)
        except OSError as error:
            return error.strerror or str(error)
        try:
            os.rmdir(link)
        except OSError:
            link.unlink(missing_ok=True)
    return None


def check_symlinks(context: HealthContext) -> CheckResult:
    if not _is_windows():
        return _skipped("environment.symlinks", "право создавать symlink")
    error = _symlink_error()
    if error is None:
        return CheckResult(
            id="environment.symlinks",
            group=GROUP,
            status="ok",
            message="symlink во временном каталоге создаётся",
        )
    broken = files.broken_discovery_links(context.repo)
    if broken:
        return CheckResult(
            id="environment.symlinks",
            group=GROUP,
            status="fail",
            message=f"{'; '.join(broken)}: причина — Windows запрещает этому пользователю "
            f"создавать symlink ({error})",
            fix=Fix(
                text="включите Developer Mode (Параметры → Для разработчиков) или выполните "
                "команду от имени администратора, затем пересоздайте ссылки через harness update",
                command=_DEVELOPER_MODE_COMMAND,
            ),
        )
    return CheckResult(
        id="environment.symlinks",
        group=GROUP,
        status="warn",
        message=f"пробный symlink во временном каталоге не создаётся ({error}): новые "
        "discovery-ссылки и worktree со ссылками не создадутся",
        fix=Fix(
            text="включите Developer Mode (Параметры → Для разработчиков) или выполните "
            "команду от имени администратора",
            command=_DEVELOPER_MODE_COMMAND,
        ),
    )


# --- bash for hooks ----------------------------------------------------------------------------


def _system_root() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:\\Windows"


def is_wsl_stub(bash: str, system_root: str) -> bool:
    """Whether `bash` is the WSL launcher `%SystemRoot%\\System32\\bash.exe` (or its Sysnative
    alias), which runs hooks inside a Linux distribution instead of Git Bash."""
    candidate = ntpath.normcase(ntpath.normpath(bash))
    return any(
        candidate == ntpath.normcase(ntpath.join(system_root, folder, "bash.exe"))
        for folder in ("System32", "Sysnative")
    )


def git_bash_candidates(git: str | None, environ: dict[str, str]) -> list[str]:
    """Where Git for Windows keeps bash.exe: next to the git on PATH first (its `cmd`,
    `mingw64\\bin` or `bin` folder sits one or two levels below the install root), then the
    default install roots."""
    roots: list[str] = []
    if git:
        folder = ntpath.dirname(git)
        roots += [ntpath.dirname(folder), ntpath.dirname(ntpath.dirname(folder))]
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if environ.get(variable):
            roots.append(ntpath.join(environ[variable], "Git"))
    if environ.get("LOCALAPPDATA"):
        roots.append(ntpath.join(environ["LOCALAPPDATA"], "Programs", "Git"))
    candidates: list[str] = []
    for root in roots:
        candidate = ntpath.join(root, "bin", "bash.exe")
        if root and candidate.casefold() not in (
            seen.casefold() for seen in candidates
        ):
            candidates.append(candidate)
    return candidates


def _find_git_bash() -> str | None:
    # os.environ (not a dict copy of it) looks names up case-insensitively on Windows.
    environ = {
        name: os.environ[name]
        for name in (
            "ProgramW6432",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "LOCALAPPDATA",
        )
        if name in os.environ
    }
    for candidate in git_bash_candidates(shutil.which("git"), environ):
        if Path(candidate).is_file():
            return candidate
    return None


def _git_bash_fix(git_bash: str | None) -> Fix:
    if git_bash is None:
        return Fix(
            text=f"Git Bash не найден: установите Git for Windows ({_GIT_FOR_WINDOWS_URL}), затем "
            "поставьте его каталог bin в системный Path раньше System32 и перезапустите терминал "
            "и агента"
        )
    folder = ntpath.dirname(git_bash)
    return Fix(
        text=f"поставьте {folder} в системный Path раньше System32 "
        "(PowerShell от имени администратора) и перезапустите терминал и агента",
        command=f"[Environment]::SetEnvironmentVariable('Path', '{folder};' + "
        "[Environment]::GetEnvironmentVariable('Path', 'Machine'), 'Machine')",
    )


def check_hook_bash(_context: HealthContext) -> CheckResult:
    if not _is_windows():
        return _skipped("environment.hook_bash", "bash для хуков")
    bash = shutil.which("bash")
    if bash is not None and not is_wsl_stub(bash, _system_root()):
        return CheckResult(
            id="environment.hook_bash",
            group=GROUP,
            status="ok",
            message=f"хуки вызываются через {bash}",
        )
    git_bash = _find_git_bash()
    found = f"Git Bash найден: {git_bash}" if git_bash else "Git Bash не найден"
    if bash is None:
        return CheckResult(
            id="environment.hook_bash",
            group=GROUP,
            status="warn",
            message=f'bash не найден в PATH: хуки проекта (bash "<hook>.sh") не запустятся; '
            f"{found}",
            fix=_git_bash_fix(git_bash),
        )
    return CheckResult(
        id="environment.hook_bash",
        group=GROUP,
        status="fail",
        message=f"bash из PATH — заглушка WSL {bash}: хуки проекта запустятся не в Git Bash; "
        f"{found}",
        fix=_git_bash_fix(git_bash),
    )
