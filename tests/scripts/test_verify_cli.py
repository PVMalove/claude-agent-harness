"""CLI of scripts/verify.py: `--help` describes the stages and runs none of them."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify


@pytest.fixture
def no_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if any verification stage starts."""

    def started() -> None:
        pytest.fail("verification started")

    monkeypatch.setattr(verify, "_prepare_run_root", started)


@pytest.mark.usefixtures("no_stages")
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_lists_the_stages_without_running_them(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        verify.main([flag])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: scripts/verify.py")
    for stage in (
        "static checks",
        "skills/REGISTRY.md",
        "mypy",
        "pytest",
        "clean-room",
    ):
        assert stage in out


@pytest.mark.usefixtures("no_stages")
def test_an_unknown_argument_is_refused_before_any_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        verify.main(["--fast"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --fast" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "stages"),
    [
        ([], ["mypy", "pytest", "clean-room"]),
        (["--only", "tests"], ["mypy", "pytest"]),
        (["--only", "clean-room"], ["clean-room"]),
    ],
)
def test_only_selects_the_stages_to_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    stages: list[str],
) -> None:
    started: list[str] = []
    monkeypatch.setattr(verify, "_prepare_run_root", lambda: (tmp_path / "run", {}))
    monkeypatch.setattr(verify, "_static_checks", lambda env: None)
    monkeypatch.setattr(
        verify, "run_stage", lambda name, cmd, **kwargs: started.append(name)
    )
    monkeypatch.setattr(verify, "remove_tree", lambda path: None)
    verify.main(argv)
    assert started == stages


def test_an_unknown_stage_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        verify.main(["--only", "lint"])
    assert exit_info.value.code == 2
