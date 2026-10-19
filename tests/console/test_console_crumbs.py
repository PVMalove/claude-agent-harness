"""Breadcrumbs as data: splitting a path and collapsing the middle segments on a narrow terminal."""

from __future__ import annotations

import pytest

from harness.console.crumbs import SEPARATOR, Crumb, collapse_crumbs, split_path

PATH = ["Главная", "Оркестрация", "batch-31007025-83d2", "Worker / Наблюдение"]


def _width(labels: list[str]) -> int:
    return sum(len(label) for label in labels) + len(SEPARATOR) * (len(labels) - 1)


def test_split_path_trims_the_segments() -> None:
    assert split_path("Главная › Оркестрация  ›  Worker") == [
        "Главная",
        "Оркестрация",
        "Worker",
    ]
    assert split_path("Главная") == ["Главная"]


def test_a_crumb_is_a_link_only_when_it_names_a_section_or_an_action() -> None:
    assert Crumb("Главная", section="Home").is_link
    assert Crumb("batch", action="open_batch").is_link
    assert not Crumb("Worker / Наблюдение").is_link


def test_everything_is_shown_when_it_fits() -> None:
    assert collapse_crumbs(PATH, _width(PATH)) == [0, 1, 2, 3]
    assert collapse_crumbs(PATH, 200) == [0, 1, 2, 3]


@pytest.mark.parametrize("count", [1, 2])
def test_a_short_path_is_never_collapsed(count: int) -> None:
    assert collapse_crumbs(PATH[:count], 1) == list(range(count))


def test_middle_segments_collapse_one_by_one_into_a_single_ellipsis() -> None:
    first_hidden = _width(PATH) - 1
    assert collapse_crumbs(PATH, first_hidden) == [0, None, 2, 3]
    narrower = (
        _width(["Главная", "…", "batch-31007025-83d2", "Worker / Наблюдение"]) - 1
    )
    assert collapse_crumbs(PATH, narrower) == [0, None, 3]


def test_the_first_and_the_last_segment_always_stay() -> None:
    assert collapse_crumbs(PATH, 1) == [0, None, 3]
    assert collapse_crumbs(PATH, 0) == [0, None, 3]
