"""Private console discovery and lifetime ownership, separate from runtime credentials."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import NamedTuple

from harness.storage import storage_path, storage_root


class DiscoveryBusyError(RuntimeError):
    """Another console owns the project's stable OS lock."""


def discovery_path(repo: Path) -> Path:
    """Resolve a known main checkout; never create linked-worktree fallback state."""
    repo = repo.expanduser().resolve()
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode or Path(result.stdout.strip()).resolve() != repo:
        raise ValueError("console discovery requires a Git project root")
    root = storage_root(repo, require_main_checkout=True)
    if not (root.parent / ".git").exists():
        raise ValueError("cannot locate console project owner")
    path = storage_path(repo, "runs", "console") / "server.json"
    if path.parent.parent.parent.parent != root:
        raise ValueError("console storage owner changed during resolution")
    for part in (root, root / ".sandboxes", path.parent.parent, path.parent, path):
        if part.is_symlink() or part.is_junction():
            raise ValueError("console storage must not contain links")
        if sys.platform != "win32" and part.exists() and part.stat().st_uid != os.getuid():
            raise PermissionError("console storage has a foreign owner")
    return path


class ServerEndpoint(NamedTuple):
    """Где слушает Сервер консоли проекта: loopback-порт и токен из discovery-записи."""

    port: int
    token: str


def read_record(repo: Path) -> ServerEndpoint | None:
    """Return the published endpoint, or None when no valid record is readable."""
    try:
        record = json.loads(discovery_path(repo).read_text(encoding="utf-8"))
        port, token = record["port"], record["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(port, int) and isinstance(token, str):
        return ServerEndpoint(port, token)
    return None


if sys.platform == "win32":
    class _WindowsSecurity:
        """Own Win32 token/descriptor allocations until secure file creation completes."""

        def __init__(self) -> None:
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.LocalFree.argtypes = [wintypes.HLOCAL]
            self.kernel.LocalFree.restype = wintypes.HLOCAL
            self.advapi.OpenProcessToken.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ]
            self.advapi.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.advapi.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
            ]
            self.kernel.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            self.kernel.CreateFileW.restype = wintypes.HANDLE
            self.kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
            self.advapi.GetNamedSecurityInfoW.argtypes = [
                wintypes.LPWSTR,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self.advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
            self.advapi.SetFileSecurityW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            self.descriptor = ctypes.c_void_p()
            token = wintypes.HANDLE()
            if not self.advapi.OpenProcessToken(
                self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                needed = wintypes.DWORD()
                self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                if not needed.value:
                    raise ctypes.WinError(ctypes.get_last_error())
                buffer = ctypes.create_string_buffer(needed.value)
                if not self.advapi.GetTokenInformation(
                    token, 1, buffer, needed, ctypes.byref(needed)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is a SID pointer.
                sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
                text = ctypes.c_void_p()
                if not self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    self.sid = ctypes.wstring_at(text)
                    sddl = "D:P(A;;FA;;;" + self.sid + ")"
                    if not self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                        sddl, 1, ctypes.byref(self.descriptor), None
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    self.kernel.LocalFree(text)
            finally:
                self.kernel.CloseHandle(token)

        def close(self) -> None:
            if self.descriptor.value:
                self.kernel.LocalFree(self.descriptor)
                self.descriptor.value = None

        def attributes(self) -> ctypes.Structure:
            class SecurityAttributes(ctypes.Structure):
                _fields_ = [
                    ("length", wintypes.DWORD),
                    ("descriptor", ctypes.c_void_p),
                    ("inherit", wintypes.BOOL),
                ]

            return SecurityAttributes(
                ctypes.sizeof(SecurityAttributes), self.descriptor, False
            )

        def protect_directory(self, path: Path) -> None:
            attrs = self.attributes()
            if not path.exists():
                if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attrs)):
                    raise ctypes.WinError(ctypes.get_last_error())
            owner = ctypes.c_void_p()
            descriptor = ctypes.c_void_p()
            error = int(
                self.advapi.GetNamedSecurityInfoW(
                    str(path),
                    1,
                    1,
                    ctypes.byref(owner),
                    None,
                    None,
                    None,
                    ctypes.byref(descriptor),
                )
            )
            if error:
                raise ctypes.WinError(error)
            try:
                text = ctypes.c_void_p()
                if not self.advapi.ConvertSidToStringSidW(owner, ctypes.byref(text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if ctypes.wstring_at(text) != self.sid:
                        raise PermissionError("console directory has a foreign owner")
                finally:
                    self.kernel.LocalFree(text)
            finally:
                self.kernel.LocalFree(descriptor)
            # Protected DACL, with no inherited ACEs, including on a stale existing directory.
            if not self.advapi.SetFileSecurityW(str(path), 0x80000004, self.descriptor):
                raise ctypes.WinError(ctypes.get_last_error())

        def create_file(self, path: Path) -> int:
            import msvcrt

            attrs = self.attributes()
            handle = self.kernel.CreateFileW(
                str(path), 0xC0000000, 0x00000007, ctypes.byref(attrs), 1, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                # open_osfhandle transfers ownership of the handle to the CRT descriptor.
                return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except BaseException:
                self.kernel.CloseHandle(handle)
                raise


def _private_file(path: Path) -> int:
    if sys.platform == "win32":
        security = _WindowsSecurity()
        try:
            return security.create_file(path)
        finally:
            security.close()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


class Discovery:
    """Hold a stable lock inode and remove only the exact record this owner published."""

    def __init__(self, repo: Path) -> None:
        self.path = discovery_path(repo)
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None
        self._payload: bytes | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("console discovery is already owned")
        directory = self.path.parent
        directory.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            security = _WindowsSecurity()
            try:
                security.protect_directory(directory)
            finally:
                security.close()
        else:
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise PermissionError("console directory has a foreign owner")
            directory.chmod(0o700)
        lock = directory / "server.lock"
        if lock.is_symlink() or lock.is_junction():
            raise ValueError("console lock must not be a link")
        try:
            fd = _private_file(lock)
        except FileExistsError:
            fd = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("console lock must be an unaliased regular file")
            if sys.platform == "win32":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise DiscoveryBusyError(
                        "console project is already owned"
                    ) from None
            else:
                import fcntl

                if info.st_uid != os.getuid():
                    raise PermissionError("console lock has a foreign owner")
                os.fchmod(fd, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DiscoveryBusyError(
                        "console project is already owned"
                    ) from None
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def publish(self, port: int, token: str) -> None:
        if self._fd is None:
            raise RuntimeError("console discovery is not owned")
        if self.path.is_symlink() or self.path.is_junction():
            raise ValueError("console record must not be a link")
        temporary = self.path.with_name(".server-" + secrets.token_hex(16) + ".tmp")
        fd = _private_file(temporary)
        try:
            payload = (
                json.dumps({"port": port, "token": token, "pid": os.getpid()}).encode()
                + b"\n"
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            info = self.path.stat()
            self._identity = (info.st_dev, info.st_ino)
            self._payload = payload
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if self._identity is not None and not self.path.is_symlink():
                try:
                    info = self.path.stat()
                    if (info.st_dev, info.st_ino) == self._identity:
                        with self.path.open("rb") as stream:
                            same = stream.read(4096) == self._payload
                        if same:
                            self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._identity = None
            self._payload = None
            if self._fd is not None:
                # Closing the descriptor releases flock / the Windows byte-range lock.
                os.close(self._fd)
                self._fd = None
