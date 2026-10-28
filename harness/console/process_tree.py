"""Остановка отменяемой команды вместе с её потомками для `capturing_runner`.

На POSIX команда запускается в новой сессии, и сигнал её группе доходит до потомков. На Windows
потомков держит Job Object: он действует и после выхода корня. Если хост не даёт создать Job
Object, запасной путь — `taskkill /T`, а при его отказе останавливается хотя бы корень."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from threading import Event

# Pause between the soft stop and the forced one, for a command that cleans up on SIGTERM.
_CANCEL_GRACE_SECONDS = 0.5
_POLL_SECONDS = 0.05


class CommandCancelled(Exception):
    """Отмена остановила команду до её завершения; кода возврата у команды нет."""


def is_windows() -> bool:
    """Работает ли процесс через нативные API процессов Windows."""
    return os.name == "nt"


class WindowsJobObject:
    """Job Object Windows, который держит потомков команды и после выхода её корня."""

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        import ctypes
        from ctypes import wintypes

        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("Windows Job Objects are unavailable")
        last_error: Callable[[], int] = getattr(ctypes, "get_last_error")
        self._kernel32 = loader("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._handle: object | None = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(last_error(), "CreateJobObjectW failed")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, process_handle
        ):
            self.close()
            raise OSError(last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> bool:
        """Завершить все процессы Job Object, включая потомков; `False`, если Windows отказала."""
        return bool(
            self._handle is not None
            and self._kernel32.TerminateJobObject(self._handle, 1)
        )

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class ProcessTree:
    """Корень отменяемой команды и его потомки."""

    def __init__(
        self,
        process: "subprocess.Popen[bytes]",
        job_object: WindowsJobObject | None,
    ) -> None:
        self._process = process
        self._job_object = job_object

    def stop(self, *, force: bool) -> None:
        """Мягко (SIGTERM группе) или принудительно (SIGKILL) остановить дерево.

        На Windows остановка всегда принудительная: после CTRL_BREAK корень мог бы выйти раньше,
        чем `taskkill /T` найдёт его дерево."""
        if is_windows():
            self._kill_windows_tree()
            return
        try:
            os.killpg(self._process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            # The group is gone or not ours: the root alone still gets the signal.
            with contextlib.suppress(OSError):
                (self._process.kill if force else self._process.terminate)()

    def _kill_windows_tree(self) -> None:
        if self._job_object is not None and self._job_object.terminate():
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            result = subprocess.run(
                ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return
        with contextlib.suppress(OSError):
            self._process.kill()

    def close(self) -> None:
        if self._job_object is not None:
            self._job_object.close()


def _job_object(process: "subprocess.Popen[bytes]") -> WindowsJobObject | None:
    """Job Object для команды на Windows; без него остаётся запасной путь `taskkill /T`."""
    if not is_windows():
        return None
    try:
        return WindowsJobObject(process)
    except OSError:
        return None


def wait_cancellable(
    process: "subprocess.Popen[bytes]",
    cancel_event: Event,
    outputs_done: Callable[[], bool],
) -> int:
    """Дождаться команды и её вывода, останавливая дерево, если установлено `cancel_event`.

    Команда, которая завершилась и закрыла вывод до отмены, возвращает свой код: поздняя отмена
    его не подменяет. Иначе дерево получает мягкую остановку, а через `_CANCEL_GRACE_SECONDS` —
    принудительную, и функция поднимает `CommandCancelled`."""
    tree = ProcessTree(process, _job_object(process))
    try:
        stop_started: float | None = None
        forced = False
        while True:
            returncode = process.poll()
            if returncode is not None and outputs_done():
                if stop_started is not None:
                    raise CommandCancelled
                return returncode
            if stop_started is None:
                if cancel_event.is_set():
                    stop_started = time.monotonic()
                    tree.stop(force=False)
            elif (
                not forced and time.monotonic() - stop_started >= _CANCEL_GRACE_SECONDS
            ):
                # Descendants that ignored the soft stop, or still hold the output, end here.
                forced = True
                tree.stop(force=True)
            time.sleep(_POLL_SECONDS)
    finally:
        tree.close()
