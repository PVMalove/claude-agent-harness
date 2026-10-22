"""stage-evidence.v1: one typed schema, a golden fixture shared with the Rust contracts crate, and
the JUnit parser that reconstructs exact pytest nodeids (ADR 0023, section 5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from harness.errors import HarnessError
from harness.gate_runner import stage_evidence
from harness.gate_runner.junit import failed_nodeids

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = (
    ROOT
    / "native/harness-runtime/crates/contracts/tests/fixtures/stage-evidence.v1.json"
)
REGISTRY = ROOT / "harness/orchestration/system_fields.v1.json"


def _golden() -> dict[str, object]:
    document: dict[str, object] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return document


def _stages(document: dict[str, object]) -> list[dict[str, object]]:
    stages: list[dict[str, object]] = document["stages"]  # type: ignore[assignment]
    return stages


def test_the_golden_fixture_round_trips_unchanged() -> None:
    parsed = stage_evidence.parse(GOLDEN.read_text(encoding="utf-8"))

    assert parsed.to_json() == _golden()


def test_the_golden_fixture_covers_pass_fail_and_not_run() -> None:
    parsed = stage_evidence.parse(GOLDEN.read_text(encoding="utf-8"))

    assert [stage.result for stage in parsed.stages] == ["pass", "fail", "not_run"]
    failed = parsed.stages[1]
    assert failed.stdout is not None and failed.stderr is not None
    assert failed.stdout.sha256 != failed.stderr.sha256
    assert failed.exit_code == 1 and failed.duration_ms > 0
    assert failed.failed_nodeids


def test_dumps_is_canonical_and_parses_back() -> None:
    parsed = stage_evidence.parse(GOLDEN.read_text(encoding="utf-8"))

    text = parsed.dumps()

    assert text.endswith("\n")
    assert stage_evidence.parse(text) == parsed
    assert parsed.dumps() == stage_evidence.parse(text).dumps()


def test_a_missing_or_extra_document_key_is_refused() -> None:
    extra = {**_golden(), "note": "x"}
    missing = {key: value for key, value in _golden().items() if key != "stages"}

    for broken in (extra, missing):
        with pytest.raises(stage_evidence.StageEvidenceError):
            stage_evidence.StageEvidenceDocument.from_json(broken)


@pytest.mark.parametrize("key", sorted(stage_evidence.STAGE_KEYS))
def test_a_stage_without_one_key_is_refused(key: str) -> None:
    document = copy.deepcopy(_golden())
    del _stages(document)[0][key]

    with pytest.raises(stage_evidence.StageEvidenceError, match=key):
        stage_evidence.StageEvidenceDocument.from_json(document)


def test_a_stage_with_an_unknown_key_is_refused() -> None:
    document = copy.deepcopy(_golden())
    _stages(document)[0]["stdout_text"] = "inline"

    with pytest.raises(stage_evidence.StageEvidenceError, match="stdout_text"):
        stage_evidence.StageEvidenceDocument.from_json(document)


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (0, "result", "passed"),
        (0, "kind", "unit"),
        (0, "selection", "failed_only"),
        (0, "candidate_sha", "not-a-sha"),
        (0, "plan_sha256", "ABC"),
        (0, "exit_code", True),
        (0, "duration_ms", -1),
        (0, "duration_ms", 1.5),
        (0, "stage_id", ""),
        (0, "failed_nodeids", "tests/a.py::t"),
        (0, "failed_nodeids", [1]),
        (0, "stdout", {"path": "x", "sha256": "short"}),
        (0, "stdout", {"path": "x"}),
        (0, "stdout", {"path": "x", "sha256": "0" * 64, "extra": 1}),
        # A result must agree with its exit code and its streams.
        (0, "exit_code", 3),
        (1, "exit_code", 0),
        (0, "failed_nodeids", ["tests/a.py::t"]),
        (2, "exit_code", 0),
        (2, "failed_nodeids", ["tests/a.py::t"]),
        (2, "stdout", {"path": "x", "sha256": "0" * 64}),
        (1, "stdout", None),
    ],
)
def test_an_invalid_stage_value_is_refused(
    index: int, field: str, value: object
) -> None:
    document = copy.deepcopy(_golden())
    _stages(document)[index][field] = value

    with pytest.raises(stage_evidence.StageEvidenceError):
        stage_evidence.StageEvidenceDocument.from_json(document)


def test_a_not_run_stage_may_carry_the_exit_code_of_a_run_that_selected_nothing() -> (
    None
):
    document = copy.deepcopy(_golden())
    stage = _stages(document)[1]
    stage.update(result="not_run", exit_code=5, failed_nodeids=[])

    parsed = stage_evidence.StageEvidenceDocument.from_json(document)

    assert parsed.stages[1].result == "not_run" and parsed.stages[1].exit_code == 5


@pytest.mark.parametrize("code", [-(2**31), -1, 3221225477, 2**32 - 1])
def test_an_exit_code_within_the_shared_32_bit_range_is_accepted(code: int) -> None:
    document = copy.deepcopy(_golden())
    _stages(document)[1]["exit_code"] = code

    parsed = stage_evidence.StageEvidenceDocument.from_json(document)

    assert parsed.stages[1].exit_code == code


@pytest.mark.parametrize("code", [-(2**31) - 1, 2**32, 2**63])
def test_an_exit_code_outside_the_shared_32_bit_range_is_refused(code: int) -> None:
    document = copy.deepcopy(_golden())
    _stages(document)[1]["exit_code"] = code

    with pytest.raises(stage_evidence.StageEvidenceError, match="exit_code"):
        stage_evidence.StageEvidenceDocument.from_json(document)


def test_an_unknown_schema_name_and_bad_json_are_refused() -> None:
    with pytest.raises(stage_evidence.StageEvidenceError, match="schema"):
        stage_evidence.StageEvidenceDocument.from_json(
            {**_golden(), "schema": "stage-evidence.v2"}
        )
    with pytest.raises(stage_evidence.StageEvidenceError):
        stage_evidence.parse("{not json")
    assert issubclass(stage_evidence.StageEvidenceError, HarnessError)


def _registry_values(field: str) -> tuple[str, ...]:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return tuple(registry["fields"][field]["values"])


def test_the_vocabularies_equal_the_system_fields_registry() -> None:
    assert stage_evidence.RESULTS == _registry_values("report.check_result")
    assert stage_evidence.SELECTIONS == _registry_values("verification_plan.selection")
    assert stage_evidence.KINDS == _registry_values("verification_plan.stage_kind")


# --- JUnit: exact pytest nodeids -------------------------------------------------------------


def _junit(*cases: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>"
    )


def _case(classname: str, name: str, outcome: str = "failure") -> str:
    body = f'<{outcome} message="m">trace &lt;x&gt;</{outcome}>' if outcome else ""
    return f'<testcase classname="{classname}" name="{name}" time="0.001">{body}</testcase>'


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for relative in (
        "tests/__init__.py",
        "tests/sub/__init__.py",
        "tests/sub/test_a.py",
        "tests/test_b.py",
        "top_test.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def test_failed_and_errored_cases_become_exact_nodeids(project: Path) -> None:
    xml = _junit(
        _case("tests.sub.test_a", "test_plain"),
        _case("tests.sub.test_a", "test_param[a/b]"),
        _case("tests.sub.test_a", "test_param[x::y]"),
        _case("tests.sub.test_a.TestK", "test_m"),
        _case("tests.sub.test_a.TestK.TestInner", "test_n"),
        _case("tests.sub.test_a", "test_fixture_broke", "error"),
        _case("top_test", "test_top"),
    )

    result = failed_nodeids(xml, project)

    assert result.nodeids == (
        "tests/sub/test_a.py::test_plain",
        "tests/sub/test_a.py::test_param[a/b]",
        "tests/sub/test_a.py::test_param[x::y]",
        "tests/sub/test_a.py::TestK::test_m",
        "tests/sub/test_a.py::TestK::TestInner::test_n",
        "tests/sub/test_a.py::test_fixture_broke",
        "top_test.py::test_top",
    )
    assert result.unresolved == ()


def test_passed_and_skipped_cases_are_not_failures(project: Path) -> None:
    xml = _junit(
        _case("tests.test_b", "test_ok", ""),
        _case("tests.test_b", "test_skip", "skipped"),
        _case("tests.test_b", "test_bad"),
    )

    assert failed_nodeids(xml, project).nodeids == ("tests/test_b.py::test_bad",)


def test_a_case_failing_in_call_and_teardown_is_listed_once(project: Path) -> None:
    xml = _junit(
        _case("tests.test_b", "test_bad"),
        _case("tests.test_b", "test_bad", "error"),
    )

    assert failed_nodeids(xml, project).nodeids == ("tests/test_b.py::test_bad",)


def test_a_collection_error_names_the_module_file(project: Path) -> None:
    xml = _junit(_case("", "tests.test_b", "error"))

    assert failed_nodeids(xml, project).nodeids == ("tests/test_b.py",)


def test_a_case_whose_module_is_gone_is_unresolved_not_guessed(project: Path) -> None:
    xml = _junit(_case("tests.test_gone", "test_x"), _case("tests.test_b", "test_bad"))

    result = failed_nodeids(xml, project)

    assert result.nodeids == ("tests/test_b.py::test_bad",)
    assert result.unresolved == ("tests.test_gone::test_x",)


def test_a_report_that_is_not_junit_is_refused(project: Path) -> None:
    with pytest.raises(stage_evidence.StageEvidenceError):
        failed_nodeids("<html/>", project)
    with pytest.raises(stage_evidence.StageEvidenceError):
        failed_nodeids("not xml", project)
