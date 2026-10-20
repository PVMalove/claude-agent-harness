"""The full /implement route: the test author serves the worker checkout or stays disabled."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

full_route = importlib.import_module("scripts.clean_room.full_route_worktree")
clean_room = importlib.import_module("scripts.test_clean_room")

MISMATCH = {"status": "disabled", "reason": "checkout_mismatch"}


def _checkout(path: Path) -> Path:
    for package in ("src", "tests"):
        (path / package).mkdir(parents=True)
        (path / package / "__init__.py").write_text("", encoding="utf-8")
    (path / ".harness").mkdir()
    (path / ".harness" / "project.json").write_text("{}", encoding="utf-8")
    return path


def test_external_transport_never_runs_a_preflight() -> None:
    calls: list[str] = []

    def preflight() -> dict[str, object]:
        calls.append("preflight")
        return {"status": "ok"}

    answer = full_route.effective_answer("external", preflight)

    assert answer == {"status": "disabled", "reason": "transport_unsupported"}
    assert calls == []


def test_in_process_transport_uses_the_preflight_answer() -> None:
    assert full_route.effective_answer("in-process", lambda: MISMATCH) == MISMATCH
    assert full_route.effective_answer("in-process", lambda: {"status": "ok"}) == {
        "status": "ok"
    }


def test_mismatch_falls_back_to_ordinary_tdd_inside_the_worktree(
    tmp_path: Path,
) -> None:
    main = _checkout(tmp_path / "main")
    worktree = _checkout(tmp_path / "worker")
    before = full_route.snapshot(main)

    route = full_route.ordinary_in_worktree(main, worktree, MISMATCH)

    assert (route.route, route.reason) == ("ordinary", "checkout_mismatch")
    assert route.author is None
    assert route.steps == [
        "preflight",
        "seam_agreed",
        "ordinary_test",
        "implement",
        "refactor",
    ]
    assert full_route.snapshot(main) == before


def test_a_file_written_into_the_main_checkout_is_detected(tmp_path: Path) -> None:
    main = _checkout(tmp_path / "main")
    before = full_route.snapshot(main)
    (main / "tests" / "cyc_leak.py").write_text("", encoding="utf-8")

    with pytest.raises(SystemExit):
        full_route.ensure_unchanged("leak", main, before)


def test_scenario_is_registered_after_the_fast_route() -> None:
    names = [scenario.__name__.rsplit(".", 1)[-1] for scenario in clean_room.SCENARIOS]

    assert names.index("full_route_worktree") == names.index("fast_implement_route") + 1
