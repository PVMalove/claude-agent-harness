"""Verification profiles by batch scope (issue #756).

`verification_profiles` maps a batch scope to its developer and review commands. The contract,
the JSON schema and the example config must agree on the key, and `batch create` must freeze the
commands of the first profile whose paths cover every allowed path of the batch.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.configuration_schema import value_problems
from harness.orchestration import contract, coordinator
from harness.orchestration.core.utils import JsonObject
from tests.orchestration import test_coordinator as coordinator_tests

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "harness" / "orchestration.example.json"
SCHEMA = ROOT / "harness" / "orchestration" / "orchestration.schema.json"
ROLES = ROOT / "harness" / "orchestration" / "roles"

CONSOLE = {
    "paths": ["harness/console/**", "tests/console/**"],
    "developer": ["make lint", "pytest -q -n 8 tests/console"],
    "review": ["make lint", "pytest -q -n 8 tests/console"],
}
INVALID_PROFILES: dict[str, object] = {
    "not a list": {"paths": ["src/**"], "developer": [], "review": []},
    "entry not an object": ["src/**"],
    "missing paths": [{"developer": [], "review": []}],
    "empty paths": [{"paths": [], "developer": [], "review": []}],
    "missing developer": [{"paths": ["src/**"], "review": []}],
    "missing review": [{"paths": ["src/**"], "developer": []}],
    "developer not strings": [{"paths": ["src/**"], "developer": [1], "review": []}],
    "unknown field": [
        {"paths": ["src/**"], "developer": [], "review": [], "qa": ["make verify"]}
    ],
}


def _example() -> JsonObject:
    data: JsonObject = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def _profile_problems(profiles: object) -> list[str]:
    config = {**_example(), "verification_profiles": profiles}
    return [
        problem
        for problem in contract.configuration_problems(config, ROLES)
        if "verification_profiles" in problem
    ]


class VerificationProfileContractTests(unittest.TestCase):
    def test_the_example_ships_a_valid_console_profile(self) -> None:
        example = _example()

        self.assertEqual(example["verification_profiles"], [CONSOLE])
        self.assertEqual(contract.health_problems(EXAMPLE, ROLES), [])

    def test_a_config_without_profiles_stays_valid(self) -> None:
        example = _example()
        del example["verification_profiles"]

        self.assertEqual(contract.configuration_problems(example, ROLES), [])

    def test_the_validator_rejects_every_invalid_profile_by_name(self) -> None:
        for case, profiles in INVALID_PROFILES.items():
            with self.subTest(case=case):
                self.assertNotEqual(_profile_problems(profiles), [])

    def test_a_profile_path_must_be_repo_relative(self) -> None:
        for path in ("/abs/**", "../outside/**", "src//x", "src\\x"):
            with self.subTest(path=path):
                problems = _profile_problems(
                    [{"paths": [path], "developer": [], "review": []}]
                )
                self.assertNotEqual(problems, [])

    def test_brief_validation_accepts_only_the_profile_of_the_batch_scope(
        self,
    ) -> None:
        orchestration = {
            "paths": ["harness/orchestration/**"],
            "developer": ["make lint", "pytest -q tests/orchestration"],
            "review": ["make lint"],
        }
        config = {
            "verification_commands": ["make verify"],
            "developer_verification_commands": ["make full"],
            "verification_profiles": [CONSOLE, orchestration],
        }

        console_scope = contract.accepted_verification_commands(
            config, "developer", "work", allowed_paths=["harness/console/**"]
        )
        no_scope = contract.accepted_verification_commands(config, "developer", "work")
        review = contract.accepted_verification_commands(
            config, "code-review", "work", allowed_paths=["harness/console/**"]
        )

        self.assertIn(CONSOLE["developer"], console_scope)
        self.assertNotIn(orchestration["developer"], console_scope)
        self.assertEqual(no_scope, [["make full"]])
        self.assertIn(CONSOLE["review"], review)

    def test_an_empty_scope_never_selects_a_profile(self) -> None:
        config = {"verification_profiles": [CONSOLE]}

        self.assertIsNone(contract.verification_profile_for(config, []))
        self.assertEqual(
            contract.verification_profile_for(config, ["harness/console/**"]), CONSOLE
        )

    def test_the_schema_rejects_the_same_invalid_profiles(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        node = schema["properties"]["verification_profiles"]

        self.assertEqual(value_problems([CONSOLE], node, schema), [])
        for case, profiles in INVALID_PROFILES.items():
            with self.subTest(case=case):
                self.assertNotEqual(value_problems(profiles, node, schema), [])


class VerificationProfileBatchTests(unittest.TestCase):
    """`batch create` against a real repository, ledger and project config."""

    GENERAL_DEVELOPER = ["make full-developer"]
    GENERAL_REVIEW = ["make full-review"]

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def _configure(self, profiles: list[JsonObject] | None) -> None:
        config: JsonObject = {
            "access_policy": {"defaults": {"mode": "inherit"}},
            "developer_verification_commands": self.GENERAL_DEVELOPER,
            "review_verification_commands": self.GENERAL_REVIEW,
        }
        if profiles is not None:
            config["verification_profiles"] = profiles
        (self.fx.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def _create(self, allowed_paths: list[str]) -> JsonObject:
        worktree = self.fx.tmp / "worktree"
        branch = "feature/issue-756-profiles"
        coordinator_tests._git(
            self.fx.repo, "worktree", "add", "-b", branch, str(worktree), "master"
        )
        return coordinator.create_batch(
            coordinator_tests._ns(
                repo=str(self.fx.repo),
                state_dir=str(self.fx.state_dir),
                ticket="#756",
                goal=None,
                branch=branch,
                worktree=str(worktree),
                allowed_path=allowed_paths,
                integration_ref="master",
                definition_of_done=["freeze the profile commands"],
                prohibited_change=["secrets"],
                required_gate=None,
                dependency=None,
                expected_file=["harness/console/app.py"],
                expected_service=["console"],
                expected_changed_lines=10,
                expected_context_tokens=None,
            )
        )

    def _commands(self, batch: JsonObject) -> tuple[object, object]:
        return (
            batch["developer_verification_commands"],
            batch["review_verification_commands"],
        )

    def test_a_profile_covering_every_allowed_path_is_frozen_in_the_batch(
        self,
    ) -> None:
        self._configure([CONSOLE])

        batch = self._create(["harness/console/**", "tests/console/test_app.py"])

        self.assertEqual(
            self._commands(batch), (CONSOLE["developer"], CONSOLE["review"])
        )

    def test_the_first_covering_profile_wins(self) -> None:
        wider = {
            "paths": ["harness/**"],
            "developer": ["make lint"],
            "review": ["make lint"],
        }
        self._configure([CONSOLE, wider])

        batch = self._create(["harness/console/**"])

        self.assertEqual(
            self._commands(batch), (CONSOLE["developer"], CONSOLE["review"])
        )

    def test_partial_coverage_keeps_the_general_commands(self) -> None:
        self._configure([CONSOLE])

        batch = self._create(["harness/console/**", "harness/orchestration/**"])

        self.assertEqual(
            self._commands(batch), (self.GENERAL_DEVELOPER, self.GENERAL_REVIEW)
        )

    def test_without_profiles_the_general_commands_are_frozen(self) -> None:
        self._configure(None)

        batch = self._create(["harness/console/**"])

        self.assertEqual(
            self._commands(batch), (self.GENERAL_DEVELOPER, self.GENERAL_REVIEW)
        )


if __name__ == "__main__":
    unittest.main()
