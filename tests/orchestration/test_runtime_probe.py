"""Runtime probes diagnose a launch without starting a worker or replacing its self-report (#679)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.runtime_probe import (
    probe_adapter,
    probe_app_server,
    validate_runtime_evidence,
)

FAKE_APP_SERVER = Path(__file__).with_name("fake_app_server.py")
HELP_ADAPTER = (
    "import sys\n"
    "from pathlib import Path\n"
    "if '--help' in sys.argv:\n"
    "    print('usage: adapter dispatch [--profile PROFILE] --repo REPO --brief BRIEF')\n"
    "    raise SystemExit(0)\n"
    "Path(sys.argv[0]).with_suffix('.launched').write_text('launched')\n"
)


def _adapter(tmp_path: Path, source: str = HELP_ADAPTER) -> list[str]:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(source, encoding="utf-8")
    return [sys.executable, str(adapter)]


def test_adapter_flags_missing_from_its_help_are_unsupported_without_a_launch(
    tmp_path: Path,
) -> None:
    command = _adapter(tmp_path)

    evidence = probe_adapter(
        command, ["--profile=fast", "--legacy-flag", "value"], cwd=tmp_path
    )

    assert evidence["status"] == "unsupported"
    assert evidence["unsupported_arguments"] == ["--legacy-flag"]
    assert evidence["worker_started"] is False
    assert not (tmp_path / "adapter.launched").exists()


def test_adapter_flags_in_its_help_are_supported(tmp_path: Path) -> None:
    evidence = probe_adapter(_adapter(tmp_path), ["--profile", "fast"], cwd=tmp_path)

    assert evidence["status"] == "supported"
    assert evidence["unsupported_arguments"] == []


def test_a_flag_prefix_is_not_mistaken_for_a_supported_flag(tmp_path: Path) -> None:
    evidence = probe_adapter(_adapter(tmp_path), ["--prof"], cwd=tmp_path)

    assert evidence["unsupported_arguments"] == ["--prof"]


@pytest.mark.parametrize(
    ("source", "status"),
    [
        (
            "import sys\nsys.stderr.write('boom token=abc123')\nraise SystemExit(3)\n",
            "unknown",
        ),
        ("import time\ntime.sleep(30)\n", "timeout"),
    ],
)
def test_an_adapter_help_that_confirms_nothing_is_a_limitation(
    tmp_path: Path, source: str, status: str
) -> None:
    evidence = probe_adapter(
        _adapter(tmp_path, source), ["--profile=fast"], cwd=tmp_path, timeout=1
    )

    assert evidence["status"] == status
    assert evidence["limitation"]
    assert "abc123" not in str(evidence)


def test_help_that_does_not_describe_dispatch_never_blocks_a_flag(
    tmp_path: Path,
) -> None:
    evidence = probe_adapter(
        _adapter(tmp_path, "print('adapter 1.0')\n"), ["--profile=fast"], cwd=tmp_path
    )

    assert evidence["status"] == "unknown"
    assert "cannot be checked" in evidence["limitation"]


def test_a_missing_adapter_executable_is_unavailable(tmp_path: Path) -> None:
    evidence = probe_adapter([str(tmp_path / "missing")], [], cwd=tmp_path)

    assert evidence["status"] == "unavailable"


def test_runtime_evidence_reports_missing_and_mismatched_fields_separately(
    tmp_path: Path,
) -> None:
    expected = {"model": "sonnet", "cwd": str(tmp_path), "reasoningEffort": "high"}

    verified = validate_runtime_evidence(
        expected, {"model": "sonnet", "cwd": str(tmp_path), "reasoningEffort": "high"}
    )
    missing = validate_runtime_evidence(
        expected, {"model": "sonnet", "cwd": str(tmp_path)}
    )
    mismatch = validate_runtime_evidence(
        expected, {"model": "opus", "cwd": str(tmp_path), "reasoningEffort": "high"}
    )

    assert verified["match"] is True
    assert (missing["status"], missing["missing"]) == ("missing", ["reasoningEffort"])
    assert (mismatch["status"], mismatch["mismatched"]) == ("mismatch", ["model"])
    assert all(
        result["model_self_report_required"] is True
        for result in (verified, missing, mismatch)
    )


def _probe(tmp_path: Path, mode: str, *, timeout: int = 20) -> JsonObject:
    return probe_app_server(
        [sys.executable, str(FAKE_APP_SERVER), "--mode", mode],
        {"resolved_model": "sonnet", "resolved_effort": "high"},
        cwd=tmp_path,
        timeout=timeout,
    )


def test_app_server_probe_is_ready_only_after_matching_fields_and_a_completed_turn(
    tmp_path: Path,
) -> None:
    evidence = _probe(tmp_path, "ready")

    assert evidence["status"] == "ready"
    assert evidence["ready"] is True
    assert evidence["model_listed"] is True
    assert evidence["worker_started"] is False
    assert evidence["model_self_report_required"] is True


def test_capacity_after_thread_start_is_a_classified_failure_not_ready(
    tmp_path: Path,
) -> None:
    evidence = _probe(tmp_path, "capacity")

    assert evidence["status"] == "failed"
    assert evidence["ready"] is False
    assert evidence["runtime_evidence"]["match"] is True
    assert evidence["classification"]["category"] == "capacity"
    assert evidence["classification"]["method"] == "structured"


@pytest.mark.parametrize(
    ("mode", "status"), [("mismatch", "mismatch"), ("missing-cwd", "missing")]
)
def test_thread_start_fields_that_do_not_match_the_brief_are_not_ready(
    tmp_path: Path, mode: str, status: str
) -> None:
    evidence = _probe(tmp_path, mode)

    assert evidence["status"] == "not-ready"
    assert evidence["ready"] is False
    assert evidence["runtime_evidence"]["status"] == status


def test_a_silent_turn_times_out_and_the_app_server_is_stopped(tmp_path: Path) -> None:
    evidence = _probe(tmp_path, "silent", timeout=2)

    assert evidence["status"] == "timeout"
    assert evidence["ready"] is False


def test_an_app_server_that_exits_early_is_a_failure_with_its_diagnostic(
    tmp_path: Path,
) -> None:
    evidence = _probe(tmp_path, "exit")

    assert evidence["status"] == "failed"
    assert "not logged in" in evidence["failure"]["error"]


def test_a_missing_runtime_executable_is_unavailable(tmp_path: Path) -> None:
    evidence = probe_app_server(
        [str(tmp_path / "missing")], {"resolved_model": "sonnet"}, cwd=tmp_path
    )

    assert evidence["status"] == "unavailable"
    assert evidence["ready"] is False
