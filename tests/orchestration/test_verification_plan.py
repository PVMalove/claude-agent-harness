"""VerificationPlan: план проверки из ImpactSet и политика `verification_plan_policy` (ADR 0023).

Ключ политики, JSON schema и пример конфига согласованы, `batch create` замораживает политику
только при её наличии, а `build_plan` строит стадии по реальному git-репозиторию и графу импортов.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import unittest
from pathlib import Path

import pytest

from harness.configuration_schema import value_problems
from harness.orchestration import contract, verification_plan
from harness.orchestration.core.constants import (
    VERIFICATION_PLAN_SELECTIONS,
    VERIFICATION_STAGE_KINDS,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from tests.orchestration import test_coordinator as coordinator_tests
from tests.repo_map.test_change_impact import (
    BASE_FILES,
    RegexBackend,
    UnavailableBackend,
    _commit,
    _git,
    _init,
    _write,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "harness" / "orchestration.example.json"
SCHEMA = ROOT / "harness" / "orchestration" / "orchestration.schema.json"
ROLES = ROOT / "harness" / "orchestration" / "roles"

POLICY: JsonObject = {
    "max_related_tests": 3,
    "stages": [
        {
            "stage_id": "lint",
            "kind": "lint",
            "argv": ["make", "lint"],
            "timeout_seconds": 600,
        },
        {
            "stage_id": "typecheck",
            "kind": "typecheck",
            "argv": ["make", "typecheck"],
            "timeout_seconds": 900,
        },
        {
            "stage_id": "tests",
            "kind": "pytest",
            "argv": ["python", "-m", "pytest", "-q"],
            "timeout_seconds": 1800,
        },
    ],
}


def _example() -> JsonObject:
    data: JsonObject = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def _with(**changes: object) -> JsonObject:
    policy = copy.deepcopy(POLICY)
    policy.update(changes)
    return policy


def _stage(**changes: object) -> JsonObject:
    return {**copy.deepcopy(POLICY["stages"][2]), **changes}


INVALID_POLICIES: dict[str, object] = {
    "not an object": ["lint"],
    "missing max_related_tests": {"stages": POLICY["stages"]},
    "zero max_related_tests": _with(max_related_tests=0),
    "non-integer max_related_tests": _with(max_related_tests="3"),
    "missing stages": {"max_related_tests": 3},
    "empty stages": _with(stages=[]),
    "stage not an object": _with(stages=["make lint"]),
    "missing stage_id": _with(
        stages=[{k: v for k, v in _stage().items() if k != "stage_id"}]
    ),
    "empty stage_id": _with(stages=[_stage(stage_id="")]),
    "unknown kind": _with(stages=[_stage(kind="docs")]),
    "kind in another case": _with(stages=[_stage(kind="Pytest")]),
    "empty argv": _with(stages=[_stage(argv=[])]),
    "argv not strings": _with(stages=[_stage(argv=["pytest", 1])]),
    "zero timeout": _with(stages=[_stage(timeout_seconds=0)]),
    "unknown stage field": _with(stages=[_stage(selection="full")]),
    "unknown policy field": _with(retries=1),
    "no pytest stage": _with(
        stages=[_stage(kind="lint"), _stage(stage_id="t2", kind="custom")]
    ),
}


def _policy_problems(policy: object) -> list[str]:
    config = {**_example(), "verification_plan_policy": policy}
    return [
        problem
        for problem in contract.configuration_problems(config, ROLES)
        if "verification_plan_policy" in problem
    ]


class VerificationPlanPolicyContractTests(unittest.TestCase):
    def test_a_valid_policy_has_no_problems(self) -> None:
        self.assertEqual(_policy_problems(POLICY), [])

    def test_a_config_without_the_policy_stays_valid(self) -> None:
        example = _example()
        example.pop("verification_plan_policy", None)

        self.assertEqual(contract.configuration_problems(example, ROLES), [])

    def test_the_example_ships_a_valid_policy(self) -> None:
        self.assertIn("verification_plan_policy", _example())
        self.assertEqual(contract.health_problems(EXAMPLE, ROLES), [])

    def test_the_validator_and_the_schema_reject_the_same_invalid_policies(
        self,
    ) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        node = schema["properties"]["verification_plan_policy"]

        self.assertEqual(value_problems(POLICY, node, schema), [])
        for case, policy in INVALID_POLICIES.items():
            with self.subTest(case=case):
                self.assertNotEqual(_policy_problems(policy), [])
                self.assertNotEqual(value_problems(policy, node, schema), [])

    def test_duplicate_stage_ids_are_rejected(self) -> None:
        policy = _with(stages=[_stage(), _stage(kind="custom")])

        self.assertNotEqual(_policy_problems(policy), [])

    def test_every_kind_of_the_registry_vocabulary_is_a_valid_stage(self) -> None:
        for kind in VERIFICATION_STAGE_KINDS:
            with self.subTest(kind=kind):
                stages = [_stage(stage_id="other", kind=kind), _stage()]
                self.assertEqual(_policy_problems(_with(stages=stages)), [])


class VerificationPlanBatchTests(unittest.TestCase):
    """`batch create` against a real repository, ledger and project config."""

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def _create(
        self, policy: JsonObject | None, repo_map_policy: JsonObject | None = None
    ) -> JsonObject:
        config: JsonObject = {"access_policy": {"defaults": {"mode": "inherit"}}}
        if policy is not None:
            config["verification_plan_policy"] = policy
        if repo_map_policy is not None:
            config["repo_map_policy"] = repo_map_policy
        (self.fx.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return self.fx._create_batch()

    def test_the_policy_is_frozen_in_the_batch(self) -> None:
        batch = self._create(POLICY)

        self.assertEqual(batch["verification_plan_policy"], POLICY)

    def test_a_batch_without_the_policy_keeps_its_old_shape(self) -> None:
        batch = self._create(None)

        self.assertNotIn("verification_plan_policy", batch)
        self.assertNotIn("repo_map_policy", batch)

    def test_the_repo_map_policy_is_frozen_beside_the_plan_policy(self) -> None:
        batch = self._create(POLICY, {"deny_paths": ["docs/*"]})

        self.assertEqual(batch["repo_map_policy"], {"deny_paths": ["docs/*"]})

    def test_a_missing_repo_map_policy_is_frozen_as_an_empty_section(self) -> None:
        batch = self._create(POLICY)

        self.assertEqual(batch["repo_map_policy"], {})


def _plan(
    repo: Path,
    base: str,
    *,
    role: str = "developer",
    policy: JsonObject = POLICY,
    backend: object | None = None,
    runtime_images: dict[str, str] | None = None,
) -> JsonObject:
    return verification_plan.build_plan(
        repo,
        policy,
        base=base,
        candidate=_git(repo, "rev-parse", "HEAD"),
        role=role,
        backend=backend or RegexBackend(repo),  # type: ignore[arg-type]
        runtime_images=runtime_images,
    )


def _stages(plan: JsonObject) -> dict[str, JsonObject]:
    stages = plan["stages"]
    assert isinstance(stages, list)
    return {stage["stage_id"]: stage for stage in stages}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init(root)
    _write(
        root,
        {
            **BASE_FILES,
            "uv.lock": "lock = 1\n",
            "pyproject.toml": '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 88\n',
        },
    )
    _commit(root, "base")
    return root


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def test_a_known_impact_gives_a_focused_pytest_stage(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    candidate = _commit(repo, "change core")

    plan = _plan(repo, base)
    stages = _stages(plan)

    assert plan["base_sha"] == base
    assert plan["candidate_sha"] == candidate
    assert list(stages) == ["lint", "typecheck", "tests"]
    tests = stages["tests"]
    assert tests["selection"] == "focused"
    assert tests["nodeids"] == ["tests/test_app.py"]
    assert tests["argv"] == ["python", "-m", "pytest", "-q", "tests/test_app.py"]
    assert tests["timeout_seconds"] == 1800
    for name in ("lint", "typecheck"):
        assert stages[name]["selection"] == "full"
        assert stages[name]["nodeids"] == []
        assert stages[name]["argv"] == ["make", name]
    assert plan["impact"]["unknown"] == []


@pytest.mark.parametrize("name", ["lint", "typecheck", "tests"])
def test_every_stage_carries_the_environment_fingerprint(repo: Path, name: str) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    stage = _stages(_plan(repo, base))[name]

    assert re.fullmatch(r"[0-9a-f]{64}", stage["environment_fingerprint"])
    assert stage["kind"] in VERIFICATION_STAGE_KINDS
    assert stage["selection"] in VERIFICATION_PLAN_SELECTIONS


def _touch_conftest(repo: Path) -> None:
    _write(repo, {"tests/conftest.py": "import pytest\n"})


def _touch_pyproject(repo: Path) -> None:
    _write(repo, {"pyproject.toml": '[project]\nname = "demo"\nversion = "2"\n'})


def _touch_lock(repo: Path) -> None:
    _write(repo, {"uv.lock": "lock = 2\n"})


def _rename_referenced_module(repo: Path) -> None:
    _git(repo, "mv", "pkg/core.py", "pkg/kernel.py")


def _delete_referenced_module(repo: Path) -> None:
    _git(repo, "rm", "-q", "pkg/core.py")


@pytest.mark.parametrize(
    "change",
    [
        _touch_conftest,
        _touch_pyproject,
        _touch_lock,
        _rename_referenced_module,
        _delete_referenced_module,
    ],
)
def test_an_unknown_impact_widens_the_pytest_stage_to_full(
    repo: Path, change: object
) -> None:
    base = _base(repo)
    change(repo)  # type: ignore[operator]
    _commit(repo, "unknown change")

    plan = _plan(repo, base)
    tests = _stages(plan)["tests"]

    assert tests["selection"] == "full"
    assert tests["nodeids"] == []
    assert tests["argv"] == ["python", "-m", "pytest", "-q"]
    assert plan["impact"]["unknown"]


def test_related_tests_above_the_policy_threshold_widen_to_full_without_an_error(
    repo: Path,
) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n", "pkg/standalone.py": "OTHER = 3\n"})
    _commit(repo, "change two")

    within = _plan(repo, base, policy=_with(max_related_tests=2))
    above = _plan(repo, base, policy=_with(max_related_tests=1))

    assert _stages(within)["tests"]["selection"] == "focused"
    assert _stages(above)["tests"]["selection"] == "full"
    assert above["impact"]["related_tests"] == [
        "tests/test_app.py",
        "tests/test_standalone.py",
    ]
    assert [item["code"] for item in above["impact"]["unknown"]] == [
        "related-tests-over-threshold"
    ]


def test_an_unavailable_parser_widens_to_full(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    plan = _plan(repo, base, backend=UnavailableBackend())

    assert _stages(plan)["tests"]["selection"] == "full"
    assert [item["code"] for item in plan["impact"]["unknown"]] == ["graph-degraded"]


def test_qa_always_runs_the_full_pytest_stage(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    plan = _plan(repo, base, role="qa")

    assert _stages(plan)["tests"]["selection"] == "full"
    assert plan["impact"]["related_tests"] == ["tests/test_app.py"]


def test_an_empty_diff_selects_no_tests_instead_of_the_whole_suite(
    repo: Path,
) -> None:
    plan = _plan(repo, _base(repo))

    tests = _stages(plan)["tests"]
    assert tests["selection"] == "focused"
    assert tests["nodeids"] == []


def test_an_unsupported_role_is_refused(repo: Path) -> None:
    with pytest.raises(CoordinatorError):
        _plan(repo, _base(repo), role="architect")


def test_the_plan_digest_is_stable_and_follows_the_plan(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    first = _plan(repo, base)
    second = _plan(repo, base)
    qa = _plan(repo, base, role="qa")

    assert verification_plan.plan_sha256(first) == verification_plan.plan_sha256(second)
    assert re.fullmatch(r"[0-9a-f]{64}", verification_plan.plan_sha256(first))
    assert verification_plan.plan_sha256(first) != verification_plan.plan_sha256(qa)


def test_the_environment_fingerprint_follows_the_lock_and_tool_sections(
    repo: Path,
) -> None:
    def fingerprint() -> str:
        return verification_plan.environment_fingerprint(
            repo, _git(repo, "rev-parse", "HEAD")
        )

    start = fingerprint()
    _write(
        repo,
        {
            "pyproject.toml": '[project]\nname = "demo"\nversion = "2"\n\n[tool.ruff]\nline-length = 88\n'
        },
    )
    _commit(repo, "project section only")
    after_project_section = fingerprint()
    _write(
        repo,
        {
            "pyproject.toml": '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 100\n'
        },
    )
    _commit(repo, "tool section")
    after_tool_section = fingerprint()
    _write(repo, {"uv.lock": "lock = 2\n"})
    _commit(repo, "lock")
    after_lock = fingerprint()

    assert after_project_section == start
    assert after_tool_section != start
    assert after_lock != after_tool_section


IMAGES = {"worker": "sha256:" + "a" * 64, "proxy": "sha256:" + "b" * 64}


def test_the_environment_fingerprint_follows_the_pinned_runtime_images(
    repo: Path,
) -> None:
    head = _git(repo, "rev-parse", "HEAD")

    def fingerprint(images: dict[str, str] | None) -> str:
        return verification_plan.environment_fingerprint(repo, head, images)

    rebuilt = {**IMAGES, "worker": "sha256:" + "c" * 64}

    assert fingerprint(IMAGES) != fingerprint(None)
    assert fingerprint(IMAGES) != fingerprint(rebuilt)
    assert fingerprint(IMAGES) == fingerprint(dict(reversed(IMAGES.items())))
    assert fingerprint(None) == verification_plan.environment_fingerprint(repo, head)


def test_a_plan_pins_the_runtime_images_in_every_stage_fingerprint(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")

    plain = _stages(_plan(repo, base))
    pinned = _stages(_plan(repo, base, runtime_images=IMAGES))

    for name, stage in pinned.items():
        assert (
            stage["environment_fingerprint"] != plain[name]["environment_fingerprint"]
        )


def test_building_a_plan_does_not_touch_the_repository(repo: Path) -> None:
    base = _base(repo)
    _write(repo, {"pkg/core.py": "VALUE = 2\n"})
    _commit(repo, "change core")
    before = subprocess.check_output(["git", "-C", str(repo), "status", "--short"])

    _plan(repo, base)

    assert (
        subprocess.check_output(["git", "-C", str(repo), "status", "--short"]) == before
    )


if __name__ == "__main__":
    unittest.main()
