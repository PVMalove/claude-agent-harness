"""Context package and risk assessment hardening: ``--base-commit`` is checked against the base the
record is built on, and a mismatch is an operator input error, not an internal invariant."""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import context_package, risk

ORIGINAL = "a" * 40
REBASED = "b" * 40
CANDIDATE = "c" * 40


@contextlib.contextmanager
def _no_lock(ledger: object) -> Iterator[None]:
    yield


def _rebased_batch() -> JsonObject:
    return {
        "batch_id": "batch-1",
        "state": "awaiting-approval",
        "base_commit": ORIGINAL,
        "integration_base_commit": REBASED,
        "definition_of_done": ["first"],
    }


def _stub_ledger(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    monkeypatch.setattr(module, "_candidate_commit", lambda _repo, value: value)
    monkeypatch.setattr(module, "_ledger_lock", _no_lock)
    monkeypatch.setattr(module, "_load_batch", lambda *_: _rebased_batch())
    monkeypatch.setattr(module, "_validate_batch_integrity", lambda *_: None)


def _register(tmp_path: Path, base_commit: str) -> JsonObject:
    return context_package.register_context_package(
        argparse.Namespace(
            repo=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            batch="batch-1",
            candidate_commit=CANDIDATE,
            base_commit=base_commit,
            min_starting_files=1,
            max_starting_files=None,
            max_package_size_bytes=None,
        )
    )


def test_a_context_package_refuses_a_base_it_is_not_built_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_ledger(monkeypatch, context_package)

    with pytest.raises(CoordinatorError) as refused:
        _register(tmp_path, ORIGINAL)

    assert refused.value.remedy == (
        f"omit --base-commit, or pass the batch base commit {REBASED}"
    )
    assert refused.value.remedy != INTERNAL_INVARIANT_REMEDY


def test_a_context_package_accepts_the_base_it_is_built_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_ledger(monkeypatch, context_package)
    built: list[str] = []

    def persist(
        repo: Path, root: Path, ledger: object, batch: JsonObject, **_: object
    ) -> JsonObject:
        built.append(batch.get("integration_base_commit") or batch["base_commit"])
        return {"context_package_id": "context-package-1"}

    monkeypatch.setattr(
        context_package, "_latest_developer_candidate", lambda *_: CANDIDATE
    )
    monkeypatch.setattr(context_package, "_persist_context_package", persist)
    monkeypatch.setattr(context_package, "_replace_record", lambda *_: None)

    assert _register(tmp_path, REBASED) == {"context_package_id": "context-package-1"}
    assert built == [REBASED]


def test_a_risk_assessment_base_mismatch_names_the_batch_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_ledger(monkeypatch, risk)
    monkeypatch.setattr(risk, "_risk_triggers", lambda _repo: ["public api"])

    with pytest.raises(CoordinatorError) as refused:
        risk.assess_risk(
            argparse.Namespace(
                repo=str(tmp_path),
                state_dir=str(tmp_path / "state"),
                batch="batch-1",
                candidate_commit=CANDIDATE,
                base_commit=ORIGINAL,
                changed_file=["a.py"],
                developer_trigger=None,
            )
        )

    assert refused.value.remedy == (
        f"omit --base-commit, or pass the batch base commit {REBASED}"
    )
