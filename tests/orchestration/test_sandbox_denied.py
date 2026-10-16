"""A ledger the process may not write is an infrastructure refusal, not a traceback."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from harness.orchestration.ledger.ledger_ops import SandboxDeniedError, _ledger_lock
from harness.orchestration.ledger.lifecycle import LifecycleLedger


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_unwritable_ledger_state_is_reported_as_sandbox_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    real_mkdir = Path.mkdir

    def denied(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == ".coordinator.lock":
            raise OSError(code, "Read-only file system", str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(SandboxDeniedError) as caught:
        with _ledger_lock(LifecycleLedger(tmp_path)):
            pytest.fail("the lock must not be taken")
    assert caught.value.message.startswith("sandbox_denied:")
    assert ".coordinator.lock" in caught.value.message
    assert "allowWrite" in caught.value.remedy


def test_other_os_errors_are_not_called_sandbox_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = Path.mkdir

    def broken(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == ".coordinator.lock":
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", broken)
    with pytest.raises(OSError) as caught:
        with _ledger_lock(LifecycleLedger(tmp_path)):
            pytest.fail("the lock must not be taken")
    assert not isinstance(caught.value, SandboxDeniedError)
