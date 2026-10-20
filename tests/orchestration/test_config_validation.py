"""Public configuration CLI behavior, with real parsers, manifests and storage."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.console.configuration import default_document
from harness.json_types import JsonObject

PACKAGE = Path(__file__).resolve().parents[2]
COORDINATOR = PACKAGE / "harness/orchestration/coordinator.py"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".harness").mkdir(parents=True)
    shutil.copytree(
        PACKAGE / "harness/orchestration/roles",
        root / ".harness/orchestration/roles",
    )
    (root / ".harness/project.json").write_text(
        json.dumps(default_document(root, "project.json")), encoding="utf-8"
    )
    shutil.copyfile(
        PACKAGE / "harness/orchestration.example.json",
        root / ".harness/orchestration.json",
    )
    return root


def run_cli(project: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(COORDINATOR),
            "config",
            "validate",
            "--repo",
            str(project),
            *flags,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def read_config(project: Path) -> JsonObject:
    value: JsonObject = json.loads(
        (project / ".harness/orchestration.json").read_bytes()
    )
    return value


def write_config(project: Path, value: JsonObject) -> None:
    (project / ".harness/orchestration.json").write_text(
        json.dumps(value), encoding="utf-8"
    )


def test_runtime_normalizes_config_enum_and_reports_warning_without_writing(
    project: Path,
) -> None:
    authored = read_config(project)
    authored["assignment_plans"]["architect"]["runtimes"]["claude"]["effort"] = " HIGH "
    write_config(project, authored)
    before = (project / ".harness/orchestration.json").read_bytes()

    result = run_cli(project, "--json")

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["valid"] is True
    assert (
        output["document"]["assignment_plans"]["architect"]["runtimes"]["claude"][
            "effort"
        ]
        == "high"
    )
    assert (
        output["diagnostics"][0]["pointer"]
        == "/assignment_plans/architect/runtimes/claude/effort"
    )
    assert output["diagnostics"][0]["severity"] == "warning"
    assert (project / ".harness/orchestration.json").read_bytes() == before


def test_partial_policy_is_validated_without_discarding_authored_values(
    project: Path,
) -> None:
    write_config(
        project,
        {
            "approval_policy": " MANUAL-ALL ",
            "retry_policy": {"max_developer_retries": 0},
        },
    )

    result = run_cli(project, "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["document"]["approval_policy"] == "manual_all"
    assert output["document"]["retry_policy"]["max_developer_retries"] == 0


def test_duplicate_json_keys_are_fatal_before_normalization(project: Path) -> None:
    path = project / ".harness/orchestration.json"
    path.write_text('{"approval_policy":"manual_all","approval_policy":"auto"}')

    result = run_cli(project, "--json")

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["valid"] is False
    assert "duplicate" in output["diagnostics"][0]["message"]
    assert output["document"] is None


def test_fix_preview_and_apply_use_a_backup_without_losing_authored_values(
    project: Path,
) -> None:
    authored = read_config(project)
    authored["assignment_plans"]["architect"]["runtimes"]["claude"]["effort"] = " HIGH "
    write_config(project, authored)
    before = (project / ".harness/orchestration.json").read_bytes()

    preview = run_cli(project, "--fix", "--dry-run", "--json")

    assert preview.returncode == 0, preview.stderr
    assert "HIGH" in json.loads(preview.stdout)["diff"]
    assert (project / ".harness/orchestration.json").read_bytes() == before
    assert not (project / ".harness/config_versions").exists()

    applied = run_cli(project, "--fix", "--json")

    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (
        read_config(project)["assignment_plans"]["architect"]["runtimes"]["claude"][
            "effort"
        ]
        == "high"
    )
    backup_id = json.loads(applied.stdout)["backup_version"]
    assert (
        project / ".harness/config_versions" / backup_id / "orchestration.json"
    ).read_bytes() == before


def test_schema_diagnostics_aggregate_exact_nested_pointers(project: Path) -> None:
    authored = read_config(project)
    authored["assignment_plans"]["architect"]["runtimes"]["claude"]["effort"] = "hgh"
    authored["concurrency_budget"] = False
    write_config(project, authored)

    result = run_cli(project, "--json")

    assert result.returncode == 2
    diagnostics = json.loads(result.stdout)["diagnostics"]
    by_pointer = {entry["pointer"]: entry for entry in diagnostics}
    assert "/concurrency_budget" in by_pointer
    assert (
        by_pointer["/assignment_plans/architect/runtimes/claude/effort"][
            "received_redacted"
        ]
        == "hgh"
    )
    assert (
        "high"
        in by_pointer["/assignment_plans/architect/runtimes/claude/effort"]["expected"][
            "enum"
        ]
    )


@pytest.mark.parametrize("value", ["hgh", None, ["high"], {"value": "high"}])
def test_unknown_or_nonstring_effort_is_never_fixed(
    project: Path, value: object
) -> None:
    authored = read_config(project)
    authored["assignment_plans"]["architect"]["runtimes"]["claude"]["effort"] = value
    write_config(project, authored)
    before = (project / ".harness/orchestration.json").read_bytes()

    result = run_cli(project, "--fix", "--json")

    assert result.returncode == 2
    assert json.loads(result.stdout)["document"] is None
    assert (project / ".harness/orchestration.json").read_bytes() == before
    assert not (project / ".harness/config_versions").exists()


def test_canonical_mode_refuses_normalizable_spelling(project: Path) -> None:
    authored = read_config(project)
    authored["approval_policy"] = " MANUAL-ALL "
    write_config(project, authored)

    result = run_cli(project, "--canonical", "--json")

    assert result.returncode == 2
    entry = json.loads(result.stdout)["diagnostics"][0]
    assert entry["pointer"] == "/approval_policy"
    assert entry["suggestion"] == "manual_all"


def test_fallback_cycle_is_fatal(project: Path) -> None:
    authored = read_config(project)
    authored["provider_profiles"]["claude-profile"]["fallback"] = ["claude-profile"]
    write_config(project, authored)

    result = run_cli(project, "--json")

    assert result.returncode == 2
    assert "cycle" in result.stdout


def test_secret_diagnostics_do_not_echo_authored_values(project: Path) -> None:
    write_config(project, {"secret": "private-test-value"})

    result = run_cli(project, "--fix", "--json")

    assert result.returncode == 2
    assert "private-test-value" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["document"] is None


def test_ids_and_model_names_keep_their_authored_spelling(project: Path) -> None:
    authored = read_config(project)
    authored["assignment_plans"]["architect"]["runtimes"]["claude"]["model"] = (
        "My.Model"
    )
    write_config(project, authored)

    result = run_cli(project, "--json")

    assert result.returncode == 0
    assert (
        json.loads(result.stdout)["document"]["assignment_plans"]["architect"][
            "runtimes"
        ]["claude"]["model"]
        == "My.Model"
    )


@pytest.mark.parametrize(
    "raw", [b"{", b"[" * 70 + b"0" + b"]" * 70, b'{"concurrency_budget":NaN}']
)
def test_malformed_nonfinite_or_deep_json_fails_safely(
    project: Path, raw: bytes
) -> None:
    (project / ".harness/orchestration.json").write_bytes(raw)

    result = run_cli(project, "--json")

    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["valid"] is False


def test_corrupt_installed_schema_returns_diagnostics_without_a_traceback(
    project: Path,
) -> None:
    (project / ".harness/orchestration/orchestration.schema.json").write_text("{")

    result = run_cli(project, "--json")

    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["valid"] is False
    assert output["diagnostics"][0]["code"] == "config-schema"


def test_partial_policy_fix_is_accepted_by_the_storage_validator(project: Path) -> None:
    write_config(project, {"approval_policy": " MANUAL-ALL "})

    result = run_cli(project, "--fix", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_config(project) == {"approval_policy": "manual_all"}


@pytest.mark.parametrize(
    "field",
    [
        "developer_verification_commands",
        "communication_policy",
        "attention_policy",
        "qa_preparation",
        "extensions",
    ],
)
def test_direct_contract_and_cli_both_reject_optional_null(
    project: Path, field: str
) -> None:
    from harness.orchestration.contract import configuration_problems

    authored = read_config(project)
    authored[field] = None
    write_config(project, authored)
    assert configuration_problems(authored, project / ".harness/orchestration/roles")
    assert run_cli(project, "--json").returncode == 2


def test_health_rejects_duplicate_json_keys(project: Path) -> None:
    from harness.orchestration.contract import health_problems

    path = project / ".harness/orchestration.json"
    path.write_text('{"approval_policy":"auto","approval_policy":"manual_all"}')
    assert any(
        "duplicate" in problem
        for problem in health_problems(path, project / ".harness/orchestration/roles")
    )


def test_incomplete_installed_registry_returns_safe_diagnostics(
    project: Path, tmp_path: Path
) -> None:
    package = tmp_path / "installed" / "harness"
    shutil.copytree(
        PACKAGE / "harness", package, ignore=shutil.ignore_patterns("__pycache__")
    )
    registry = package / "orchestration/system_fields.v1.json"
    document = json.loads(registry.read_bytes())
    del document["fields"]["config.approval_policy"]
    registry.write_text(json.dumps(document))
    result = subprocess.run(
        [
            sys.executable,
            str(package / "orchestration/coordinator.py"),
            "config",
            "validate",
            "--repo",
            str(project),
            "--json",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["valid"] is False
    assert output["diagnostics"][0]["code"] == "config-contract"


def test_unresolved_installed_schema_reference_returns_diagnostics(
    project: Path,
) -> None:
    (project / ".harness/orchestration/orchestration.schema.json").write_text(
        '{"$ref":"#/definitions/missing"}'
    )
    result = run_cli(project, "--json")
    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["valid"] is False


def test_overlapping_alias_rule_is_diagnosed_without_guessing(
    project: Path, tmp_path: Path
) -> None:
    package = tmp_path / "installed" / "harness"
    shutil.copytree(
        PACKAGE / "harness", package, ignore=shutil.ignore_patterns("__pycache__")
    )
    registry = package / "orchestration/system_fields.v1.json"
    document = json.loads(registry.read_bytes())
    values = document["fields"]["config.approval_policy"]["values"]
    values["manual-all"] = values["manual_all"]
    registry.write_text(json.dumps(document))
    authored = read_config(project)
    authored["approval_policy"] = " MANUAL-ALL "
    write_config(project, authored)
    before = (project / ".harness/orchestration.json").read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(package / "orchestration/coordinator.py"),
            "config",
            "validate",
            "--repo",
            str(project),
            "--fix",
            "--json",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "config-ambiguous"
    assert (project / ".harness/orchestration.json").read_bytes() == before
