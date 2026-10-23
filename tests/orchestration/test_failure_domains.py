"""Every failure carries one ADR 0019 domain with structured evidence; an unrecognised one is
``unknown`` (fail-closed), and ``code_test`` needs a proven code or test defect."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from harness.orchestration import runtime_client
from harness.orchestration.core.constants import FailureDomain
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.workflow import decisions, failure_domains
from harness.orchestration.workflow.failure_domains import Failure
from harness.orchestration.workflow.startup import assess_startup

REGISTRY = Path(__file__).resolve().parents[2] / "harness/orchestration/system_fields.v1.json"


def _stages(
    *,
    failed_stage: str = "preparation",
    started: str = "not_started",
    category: str | None = "infrastructure",
    probe: str | None = "fail",
    checks: tuple[str, ...] = ("pass",),
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    if probe is not None:
        entries.append({"stage": "environment-probe", "result": probe})
    entries.extend({"stage": "project-file-check", "result": result} for result in checks)
    entries.append(
        {"stage": failed_stage, "result": "fail", "command": "make bootstrap", "diagnostics": ""}
    )
    stages: dict[str, object] = {
        "failed_stage": failed_stage,
        "code_checks_started": started,
        "stages": entries,
    }
    if category is not None:
        stages["diagnosis"] = {"category": category}
    return stages


def test_the_domains_are_the_registry_vocabulary() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    values = registry["fields"]["runtime.failure_domain"]["values"]

    assert [str(domain) for domain in FailureDomain] == list(values)
    assert runtime_client.FAILURE_DOMAINS == tuple(FailureDomain)


def test_a_failure_serialises_to_its_four_evidence_keys() -> None:
    failure = Failure(FailureDomain.TOOLING, "structured", "tooling_blocker", "report")

    assert failure.to_json() == {
        "domain": "tooling",
        "method": "structured",
        "matched": "tooling_blocker",
        "source": "report",
    }


@pytest.mark.parametrize(
    ("domain", "method"),
    [
        (FailureDomain.UNKNOWN, "structured"),
        (FailureDomain.UNKNOWN, "heuristic"),
        (FailureDomain.UNKNOWN, "approver"),
        (FailureDomain.INFRASTRUCTURE, "fallback"),
        (FailureDomain.CODE_TEST, "fallback"),
        (FailureDomain.CODE_TEST, "heuristic"),
        (FailureDomain.INFRASTRUCTURE, "guess"),
    ],
)
def test_the_invariants_refuse_an_unproven_or_inconsistent_failure(
    domain: FailureDomain, method: str
) -> None:
    with pytest.raises(ValueError):
        Failure(domain, method, "x", "y")


@pytest.mark.parametrize(("matched", "source"), [("", "report"), ("x", ""), ("  ", "r")])
def test_evidence_is_never_empty(matched: str, source: str) -> None:
    with pytest.raises(ValueError):
        Failure(FailureDomain.POLICY, "structured", matched, source)


@pytest.mark.parametrize("domain", ["unknown", "not-a-domain", None, 7])
def test_a_runtime_error_without_a_usable_domain_is_unknown(domain: object) -> None:
    class Error:
        code = "internal"

    error = Error()
    error.domain = domain  # type: ignore[attr-defined]

    failure = failure_domains.of_runtime_error(error)

    assert (failure.domain, failure.method) == (FailureDomain.UNKNOWN, "fallback")


@pytest.mark.parametrize("domain", [str(d) for d in FailureDomain if d != "unknown"])
def test_a_runtime_error_domain_is_structured_evidence(domain: str) -> None:
    error = runtime_client.RuntimeClientError("internal", "m", remedy="r", domain=domain)

    failure = failure_domains.of_runtime_error(error)

    assert failure.domain == domain
    assert failure.method == "structured"
    assert "internal" in failure.matched and failure.source == "runtime"


def test_a_runtime_error_needs_its_domain() -> None:
    with pytest.raises(TypeError):
        runtime_client.RuntimeClientError("internal", "m", remedy="r")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("category", "domain"),
    [
        ("rate-limit", FailureDomain.INFRASTRUCTURE),
        ("capacity", FailureDomain.INFRASTRUCTURE),
        ("service-unavailable", FailureDomain.INFRASTRUCTURE),
        ("network", FailureDomain.INFRASTRUCTURE),
        ("runtime-crash", FailureDomain.INFRASTRUCTURE),
        ("resources", FailureDomain.INFRASTRUCTURE),
        ("service", FailureDomain.INFRASTRUCTURE),
        ("runtime-unavailable", FailureDomain.INFRASTRUCTURE),
        ("timeout", FailureDomain.TIMEOUT),
    ],
)
def test_a_structured_runtime_failure_category_names_its_domain(
    category: str, domain: FailureDomain
) -> None:
    failure = failure_domains.of_runtime_failure({"category": category, "error": "x"})

    assert failure.domain == domain and failure.method == "structured"


def test_a_runtime_failure_text_is_a_heuristic_and_never_a_code_defect() -> None:
    failure = failure_domains.of_runtime_failure({"error": "HTTP 429 from the provider"})

    assert (failure.domain, failure.method) == (FailureDomain.INFRASTRUCTURE, "heuristic")


@pytest.mark.parametrize(
    "failure",
    [
        None,
        {},
        "boom",
        {"error": ""},
        {"error": "something unexpected happened"},
        {"error": "assertion failed: tests failed, then connection reset"},
    ],
)
def test_an_unrecognised_runtime_failure_is_unknown(failure: object) -> None:
    result = failure_domains.of_runtime_failure(failure)

    assert (result.domain, result.method) == (FailureDomain.UNKNOWN, "fallback")


@pytest.mark.parametrize(
    ("classification", "domain"),
    [
        ("worktree-mismatch", FailureDomain.ENVIRONMENT_PREPARATION),
        ("model-mismatch", FailureDomain.ENVIRONMENT_PREPARATION),
        ("runtime-unavailable", FailureDomain.INFRASTRUCTURE),
        ("runtime-crash", FailureDomain.INFRASTRUCTURE),
        ("model-self-report-missing", FailureDomain.UNKNOWN),
        ("worker-exited-without-report", FailureDomain.UNKNOWN),
        (None, FailureDomain.UNKNOWN),
        ("something-new", FailureDomain.UNKNOWN),
    ],
)
def test_a_startup_classification_names_its_domain(
    classification: str | None, domain: FailureDomain
) -> None:
    failure = failure_domains.of_startup(classification, None)

    assert failure.domain == domain
    assert (failure.method == "fallback") == (domain == FailureDomain.UNKNOWN)


def test_a_startup_runtime_failure_keeps_its_own_category_domain() -> None:
    failure = failure_domains.of_startup(
        "timeout", {"method": "structured", "category": "timeout", "field": "kind", "value": "timeout"}
    )

    assert (failure.domain, failure.method) == (FailureDomain.TIMEOUT, "structured")


def test_qa_preparation_failed_on_a_confirmed_environment_is_environment_preparation() -> None:
    failure = failure_domains.of_qa_stages(_stages())

    assert failure is not None
    assert (failure.domain, failure.method) == (
        FailureDomain.ENVIRONMENT_PREPARATION,
        "structured",
    )
    assert failure.source == "qa_stages"


def test_qa_preparation_on_a_confirmed_project_defect_is_code_test() -> None:
    failure = failure_domains.of_qa_stages(_stages(category="project-defect", probe=None))

    assert failure is not None
    assert (failure.domain, failure.method) == (FailureDomain.CODE_TEST, "structured")


@pytest.mark.parametrize(
    "stages",
    [
        _stages(category="unknown"),
        _stages(category=None),
        _stages(category="infrastructure", probe=None),
        _stages(category="infrastructure", probe="pass"),
        _stages(category="infrastructure", checks=()),
        _stages(category="infrastructure", checks=("pass", "fail")),
        _stages(category="infrastructure", started="started"),
        _stages(category="project-defect", started="started"),
        _stages(category="something-new"),
    ],
)
def test_qa_preparation_without_independent_proof_is_unknown(stages: dict[str, object]) -> None:
    failure = failure_domains.of_qa_stages(stages)

    assert failure is not None
    assert (failure.domain, failure.method) == (FailureDomain.UNKNOWN, "fallback")


def test_a_failed_gate_after_green_preparation_is_code_test() -> None:
    failure = failure_domains.of_qa_stages(_stages(failed_stage="gate", started="started"))

    assert failure is not None
    assert (failure.domain, failure.method) == (FailureDomain.CODE_TEST, "structured")


def test_a_failed_gate_with_an_environment_sign_is_environment_preparation(tmp_path: Path) -> None:
    stages = _stages(failed_stage="gate", started="started")
    stages["stages"][-1]["diagnostics"] = "OSError: [Errno 30] Read-only file system: '/tmp/x'"  # type: ignore[index]

    failure = failure_domains.of_qa_stages(stages, repo=tmp_path)

    assert failure is not None
    assert (failure.domain, failure.method) == (
        FailureDomain.ENVIRONMENT_PREPARATION,
        "heuristic",
    )


@pytest.mark.parametrize("stages", [None, {}, "x", {"failed_stage": None}, {"failed_stage": "other"}])
def test_a_report_without_a_failed_qa_stage_has_no_qa_failure(stages: object) -> None:
    assert failure_domains.of_qa_stages(stages) is None


@pytest.mark.parametrize(
    "stages",
    [
        None,
        {},
        {"failed_stage": "preparation"},
        {"failed_stage": "preparation", "code_checks_started": "not_started", "stages": "x"},
        _stages(category="Infrastructure"),
        _stages(category=["infrastructure"]),  # type: ignore[arg-type]
    ],
)
def test_no_qa_input_without_structural_proof_ever_yields_code_test(stages: object) -> None:
    failure = failure_domains.of_qa_stages(stages)

    assert failure is None or failure.domain != FailureDomain.CODE_TEST


_DISPATCH = {
    "role": "developer",
    "resolved_model": "sonnet",
    "worktree": "/tmp/worker",
    "worker_attestation_required": True,
}


@pytest.mark.parametrize(
    ("status", "classification", "domain", "method"),
    [
        (
            {"state": "blocked", "model_self_report": {"match": False}},
            "model-mismatch",
            "environment_preparation",
            "structured",
        ),
        (
            {
                "state": "blocked",
                "runtime_failure": {"kind": "runtime-unavailable", "worker_started": False},
            },
            "runtime-unavailable",
            "infrastructure",
            "structured",
        ),
        (
            {"state": "dispatched"},
            "model-self-report-missing",
            "unknown",
            "fallback",
        ),
    ],
)
def test_a_startup_assessment_carries_its_failure_domain(
    status: dict[str, object], classification: str, domain: str, method: str
) -> None:
    assessment = assess_startup(_DISPATCH, status)

    assert assessment["classification"] == classification
    failure = assessment["failure"]
    assert isinstance(failure, dict)
    assert (failure["domain"], failure["method"]) == (domain, method)
    assert failure["source"] == "startup_assessment"


# -- the routing record of a retry carries its failure -------------------------------------------

_CLEAN_REVIEW: dict[str, object] = {
    "standards": {"severity": "none", "findings": []},
    "spec": {"severity": "none", "findings": []},
}


def _route(
    stage: str,
    report: JsonObject,
    *,
    explicit_category: object = None,
    explicit_by: str = "approver",
    pressure_recorded: bool = False,
) -> dict[str, object]:
    return cast(dict[str, object], decisions._retry_routing(
        stage,
        report,
        dispatch_candidate="a" * 40,
        current_candidate="a" * 40,
        explicit_category=explicit_category,
        explicit_by=explicit_by,
        pressure_recorded=pressure_recorded,
    ))


def test_a_review_finding_is_a_structured_code_defect() -> None:
    review = {**_CLEAN_REVIEW, "standards": {"severity": "error", "findings": [{"id": 1}]}}

    routing = _route("code-review", {"outcome": "completed", "review": review})

    assert routing["failure"] == {
        "domain": "code_test",
        "method": "structured",
        "matched": "review.standards.findings",
        "source": "report",
    }


def test_a_failed_check_names_the_check_that_failed() -> None:
    checks = [
        {"command": "a", "result": "pass", "evidence": "e"},
        {"command": "b", "result": "fail", "evidence": "e"},
    ]

    routing = _route("developer", {"outcome": "failed", "checks_run": checks})

    assert routing["failure"]["matched"] == "checks_run[1].result=fail"  # type: ignore[index]
    assert routing["failure"]["domain"] == "code_test"  # type: ignore[index]


def test_an_approver_naming_a_code_category_is_approver_evidence() -> None:
    routing = _route(
        "developer",
        {"outcome": "blocked", "checks_run": []},
        explicit_category="code",
        explicit_by="alice",
    )

    assert routing["failure"] == {
        "domain": "code_test",
        "method": "approver",
        "matched": "--reason-category code",
        "source": "alice",
    }


def test_a_classifier_hint_never_proves_a_code_defect() -> None:
    routing = _route(
        "developer",
        {"outcome": "blocked", "checks_run": []},
        explicit_category="code",
        explicit_by="classifier:plugin",
    )

    assert routing["failure"]["domain"] == "unknown"  # type: ignore[index]
    assert routing["failure"]["method"] == "fallback"  # type: ignore[index]


def test_a_blocked_report_with_no_evidence_is_unknown_not_code_test() -> None:
    routing = _route("developer", {"outcome": "blocked", "checks_run": []})

    assert routing["reason_category"] == "unknown"
    assert routing["failure"]["domain"] == "unknown"  # type: ignore[index]
    assert routing["failure"]["method"] == "fallback"  # type: ignore[index]


def test_a_text_that_mentions_a_code_defect_proves_nothing() -> None:
    report = {
        "outcome": "blocked",
        "checks_run": [],
        "blockers": {"status": "present", "details": "unit tests failed: assertion error"},
        "output": "the tests failed",
    }

    routing = _route("developer", report)

    assert routing["failure"]["domain"] == "unknown"  # type: ignore[index]


def test_a_tooling_blocker_is_a_structured_tooling_failure() -> None:
    blocker = {"tool": "hook", "command": "git commit", "message": "blocked"}

    routing = _route(
        "developer", {"outcome": "blocked", "checks_run": [], "tooling_blocker": blocker}
    )

    assert routing["failure"]["domain"] == "tooling"  # type: ignore[index]
    assert routing["failure"]["matched"] == "tooling_blocker"  # type: ignore[index]


def test_a_block_bypass_is_a_policy_failure_named_by_the_approver() -> None:
    routing = _route(
        "qa",
        {"outcome": "completed", "checks_run": []},
        explicit_category="block-bypass",
        explicit_by="alice",
    )

    assert routing["failure"] == {
        "domain": "policy",
        "method": "approver",
        "matched": "--reason-category block-bypass",
        "source": "alice",
    }


def test_context_pressure_needs_its_critical_observation() -> None:
    report = {"outcome": "blocked", "checks_run": []}

    named = _route("developer", report, explicit_category="context-pressure")
    observed = _route(
        "developer", report, explicit_category="context-pressure", pressure_recorded=True
    )

    assert named["failure"]["domain"] == "unknown"  # type: ignore[index]
    assert observed["failure"]["domain"] == "infrastructure"  # type: ignore[index]
    assert observed["failure"]["method"] == "structured"  # type: ignore[index]


def test_a_confirmed_qa_environment_failure_routes_with_its_proof() -> None:
    report = {"outcome": "blocked", "checks_run": [], "qa_stages": _stages()}

    routing = _route("qa", report)

    assert routing["reason_category"] == "verification-infrastructure"
    assert routing["failure"]["domain"] == "environment_preparation"  # type: ignore[index]
    assert routing["failure"]["method"] == "structured"  # type: ignore[index]


def test_a_narrowed_retry_without_a_tooling_blocker_proves_no_cause() -> None:
    report = {
        "outcome": "completed",
        "checks_run": [],
        "incomplete_items": [{"brief_item": "x", "reason": "y", "target_role": "qa"}],
    }

    routing = decisions._retry_routing(
        "qa",
        report,
        dispatch_candidate="a" * 40,
        current_candidate="a" * 40,
        explicit_category=None,
        narrowed=True,
    )

    assert routing["failure"]["domain"] == "unknown"


@pytest.mark.parametrize("category", ["transport", "verification-infrastructure"])
def test_an_approver_naming_an_operational_cause_of_a_blocked_run_is_infrastructure(
    category: str,
) -> None:
    routing = _route(
        "qa",
        {"outcome": "blocked", "checks_run": []},
        explicit_category=category,
        explicit_by="alice",
    )

    assert routing["failure"]["domain"] == "infrastructure"  # type: ignore[index]
    assert routing["failure"]["method"] == "approver"  # type: ignore[index]
