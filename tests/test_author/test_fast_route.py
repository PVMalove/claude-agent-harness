"""The /fast-implement short route: the same cycle, a human-agreed seam, and a safe fallback."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

isolated_test_runner = importlib.import_module(
    "scripts.clean_room.isolated_test_runner"
)
fast_route = importlib.import_module("scripts.clean_room.fast_implement_route")


def _project(tmp_path: Path) -> object:
    for package in ("src", "tests"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.json").write_text("{}", encoding="utf-8")
    return fast_route.Scenario(tmp_path, isolated_test_runner._direct_execute(tmp_path))


@pytest.mark.parametrize(
    ("answer", "route", "reason"),
    [
        ({"status": "ok"}, "author", ""),
        (
            {"status": "disabled", "reason": "isolation_unavailable"},
            "ordinary",
            "isolation_unavailable",
        ),
        (
            {"status": "disabled", "reason": "guard_hook_unavailable"},
            "ordinary",
            "guard_hook_unavailable",
        ),
        ({}, "ordinary", "preflight_failed"),
        ({"status": "ok", "extra": 1}, "ordinary", "preflight_failed"),
    ],
)
def test_preflight_answer_selects_the_route(
    answer: dict[str, object], route: str, reason: str
) -> None:
    assert fast_route.choose_route(answer) == (route, reason)


def test_author_route_follows_the_shared_cycle(tmp_path: Path) -> None:
    fast_route._author_cases(_project(tmp_path))

    assert not list((tmp_path / "src").glob("cyc_*.py"))


def test_unavailable_author_keeps_the_ordinary_route_with_a_reason(
    tmp_path: Path,
) -> None:
    fast_route._fallback_cases(_project(tmp_path), fast_route.FALLBACK_ANSWERS)

    assert not list((tmp_path / "tests").glob("cyc_*.py"))


def test_unagreed_seam_stops_before_any_author_call_or_test(tmp_path: Path) -> None:
    scenario = _project(tmp_path)
    route = fast_route.ShortRoute(scenario, {"status": "ok"})

    with pytest.raises(fast_route.SeamNotAgreed):
        route.begin("cyc_x", seam_agreed=False, script=[])

    assert route.author is None
    assert route.steps == ["preflight", "ask_seam"]
    assert not list((tmp_path / "tests").glob("cyc_*.py"))
