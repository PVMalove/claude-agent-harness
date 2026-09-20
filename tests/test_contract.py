#!/usr/bin/env python3
"""Characterisation tests pinning the portable orchestration contract (issue #223)."""

from __future__ import annotations

import ast
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.errors import HarnessError
from harness.orchestration import contract


class ContractErrorInvariantTests(unittest.TestCase):
    def test_contract_error_is_a_harness_error(self) -> None:
        self.assertTrue(issubclass(contract.ContractError, HarnessError))

    def test_every_raise_site_passes_a_non_empty_remedy(self) -> None:
        tree = ast.parse(Path(contract.__file__).read_text(encoding="utf-8"))
        sites = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ContractError"
        ]
        self.assertEqual(len(sites), 43)
        for site in sites:
            assert isinstance(site.exc, ast.Call)
            remedies = [keyword.value for keyword in site.exc.keywords if keyword.arg == "remedy"]
            self.assertEqual(len(remedies), 1, f"line {site.lineno} has no remedy")
            value = remedies[0]
            if isinstance(value, ast.Constant):
                self.assertTrue(isinstance(value.value, str) and value.value.strip(), f"line {site.lineno}")


class RejectSensitiveTests(unittest.TestCase):
    def test_accepts_nested_plain_data(self) -> None:
        contract.reject_sensitive({"a": [{"b": 1}, "x"], "input_tokens": 5}, "config")

    def test_rejects_non_string_key(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.reject_sensitive({1: "x"}, "config")
        self.assertEqual(ctx.exception.message, "config contains a non-string key")
        self.assertEqual(ctx.exception.remedy, "use only string keys in config")

    def test_rejects_secret_shaped_key_with_nested_location(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.reject_sensitive({"outer": {"password": "x"}}, "config")
        self.assertEqual(ctx.exception.message, "config.outer contains secret-shaped field 'password'")
        self.assertIn("remove the secret-shaped field 'password'", ctx.exception.remedy)

    def test_rejects_secret_key_inside_list_with_index_location(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.reject_sensitive({"items": [{}, {"api_key": "x"}]}, "config")
        self.assertEqual(ctx.exception.message, "config.items[1] contains secret-shaped field 'api_key'")


class LoadRoleManifestTests(unittest.TestCase):
    def _load(self, text: str | None) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        path = directory / "role.md"
        if text is not None:
            path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_parses_scalar_and_list_keys(self) -> None:
        path = self._load("---\nname: developer\nmode: write\nrequired_capabilities:\n  - a\n  - b\n---\nbody\n")
        self.assertEqual(
            contract.load_role_manifest(path),
            {"name": "developer", "mode": "write", "required_capabilities": ["a", "b"]},
        )

    def test_unreadable_manifest(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.load_role_manifest(self._load(None))
        self.assertEqual(ctx.exception.message, "role manifest 'role.md' cannot be read")
        self.assertIn("fix the file-system error above", ctx.exception.remedy)

    def test_missing_frontmatter(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.load_role_manifest(self._load("no frontmatter\n"))
        self.assertEqual(ctx.exception.message, "role manifest 'role.md' has no valid frontmatter")
        self.assertIn("start role.md with a '---'-delimited", ctx.exception.remedy)

    def test_orphan_list_item(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.load_role_manifest(self._load("---\n  - item\n---\n"))
        self.assertEqual(ctx.exception.message, "role manifest 'role.md' has an orphan list item")
        self.assertIn("put each '- item' line under a preceding 'key:' line", ctx.exception.remedy)

    def test_invalid_frontmatter_line(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.load_role_manifest(self._load("---\nBad Line\n---\n"))
        self.assertEqual(ctx.exception.message, "role manifest 'role.md' has invalid frontmatter")
        self.assertIn("use only 'key: value' or '  - item' lines", ctx.exception.remedy)


class ResolveRuntimeNameTests(unittest.TestCase):
    def test_no_runtimes(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.resolve_runtime_name({}, None)
        self.assertEqual(ctx.exception.message, "assignment plan has no runtime assignments")

    def test_single_runtime_needs_no_request(self) -> None:
        self.assertEqual(contract.resolve_runtime_name({"runtimes": {"claude": {}}}, None), "claude")

    def test_explicit_request_is_stripped(self) -> None:
        plan: contract.JsonObject = {"runtimes": {"claude": {}, "codex": {}}}
        self.assertEqual(contract.resolve_runtime_name(plan, " codex "), "codex")

    def test_unknown_explicit_request(self) -> None:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.resolve_runtime_name({"runtimes": {"claude": {}}}, "other")
        self.assertEqual(ctx.exception.message, "role is not assigned to runtime 'other'")
        self.assertIn("claude", ctx.exception.remedy)

    def test_multiple_runtimes_use_stripped_default(self) -> None:
        plan = {"runtimes": {"claude": {}, "codex": {}}, "default_runtime": " codex "}
        self.assertEqual(contract.resolve_runtime_name(plan, None), "codex")

    def test_multiple_runtimes_without_or_with_invalid_default(self) -> None:
        for extra in ({}, {"default_runtime": "nope"}, {"default_runtime": 3}):
            plan = {"runtimes": {"claude": {}, "codex": {}}, **extra}
            with self.assertRaises(contract.ContractError) as ctx:
                contract.resolve_runtime_name(plan, None)
            self.assertIn("multiple runtimes", ctx.exception.message)
            self.assertIn("claude, codex", ctx.exception.remedy)


def _config() -> contract.JsonObject:
    return {
        "provider_profiles": {"p": {"capabilities": ["backend-development"]}},
        "backend_zones": {"z": {"paths": ["src/**"]}},
        "assignment_plans": {
            "developer": {
                "zone": "z",
                "runtimes": {"claude": {"profiles": ["p"], "model": "claude-x", "effort": "high"}},
            },
        },
    }


def _role() -> dict[str, object]:
    return {"name": "developer", "mode": "write", "required_capabilities": ["backend-development"]}


class ResolveAssignmentTests(unittest.TestCase):
    def _fail(self, config: dict[str, object], role: dict[str, object], runtime: object = "claude") -> contract.ContractError:
        with self.assertRaises(contract.ContractError) as ctx:
            contract.resolve_assignment(config, role, "developer", "z", runtime)
        return ctx.exception

    def test_happy_path(self) -> None:
        result = contract.resolve_assignment(_config(), _role(), "developer", "z", "claude")
        self.assertEqual(result["profile_id"], "p")
        self.assertEqual(result["model"], "claude-x")
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["transport"], "in-process")
        self.assertEqual(result["zone"], {"paths": ["src/**"]})

    def test_read_only_role_gets_no_paths(self) -> None:
        role = {**_role(), "mode": "read-only"}
        result = contract.resolve_assignment(_config(), role, "developer", "z", None)
        self.assertEqual(result["zone"], {"paths": []})

    def test_invalid_transport(self) -> None:
        config = _config()
        config["assignment_plans"]["developer"]["transport"] = "smoke"
        error = self._fail(config, _role())
        self.assertEqual(error.message, "role 'developer' has an invalid transport")

    def test_write_paths_outside_zone(self) -> None:
        config = _config()
        config["assignment_plans"]["developer"]["write_paths"] = ["docs/x"]
        error = self._fail(config, _role())
        self.assertEqual(error.message, "role 'developer' write_paths must remain inside backend zone 'z'")

    def test_no_runtimes_object(self) -> None:
        config = _config()
        del config["assignment_plans"]["developer"]["runtimes"]
        error = self._fail(config, _role())
        self.assertEqual(error.message, "role 'developer' has no runtime assignments")

    def test_runtime_entry_not_an_object(self) -> None:
        config = _config()
        config["assignment_plans"]["developer"]["runtimes"]["claude"] = "x"
        error = self._fail(config, _role())
        self.assertEqual(error.message, "role 'developer' is not assigned to runtime 'claude'")

    def test_incompatible_profile(self) -> None:
        config = _config()
        config["provider_profiles"]["p"]["capabilities"] = ["other"]
        error = self._fail(config, _role())
        self.assertEqual(error.message, "provider profile 'p' is incompatible with role 'developer'")

    def test_missing_profile_list(self) -> None:
        config = _config()
        config["assignment_plans"]["developer"]["runtimes"]["claude"]["profiles"] = []
        error = self._fail(config, _role())
        self.assertEqual(error.message, "role 'developer' has no provider profile")

    def test_invalid_role_manifest_name(self) -> None:
        error = self._fail(_config(), {**_role(), "name": "other"})
        self.assertEqual(error.message, "role manifest 'developer' has invalid name or mode")

    def test_invalid_model_and_effort(self) -> None:
        config = _config()
        config["assignment_plans"]["developer"]["runtimes"]["claude"]["model"] = "a b"
        self.assertEqual(
            self._fail(config, _role()).message, "assignment model must be a CLI model ID or alias without spaces"
        )
        config = _config()
        config["assignment_plans"]["developer"]["runtimes"]["claude"]["effort"] = "turbo"
        self.assertTrue(self._fail(config, _role()).message.startswith("assignment effort must be one of:"))


class PolicyProblemTests(unittest.TestCase):
    FIELDS = {"a", "b"}

    def test_absent_key_is_valid(self) -> None:
        self.assertEqual(contract._policy_problem({}, "p", self.FIELDS), [])

    def test_non_object(self) -> None:
        self.assertEqual(contract._policy_problem({"p": 1}, "p", self.FIELDS), ["orchestration p must be an object"])

    def test_unknown_field(self) -> None:
        self.assertEqual(
            contract._policy_problem({"p": {"zz": 1}}, "p", self.FIELDS),
            ["orchestration p has unknown field(s): zz"],
        )

    def test_bool_and_low_values_are_not_positive_integers(self) -> None:
        problems = contract._policy_problem({"p": {"a": True, "b": 0}}, "p", self.FIELDS)
        self.assertEqual(
            sorted(problems),
            ["orchestration p.a must be a positive integer", "orchestration p.b must be a positive integer"],
        )

    def test_minimum_zero_allows_zero(self) -> None:
        self.assertEqual(contract._policy_problem({"p": {"a": 0}}, "p", self.FIELDS, minimum=0), [])
        self.assertEqual(
            contract._policy_problem({"p": {"a": -1}}, "p", self.FIELDS, minimum=0),
            ["orchestration p.a must be a non-negative integer"],
        )

    def test_boolean_fields(self) -> None:
        self.assertEqual(contract._policy_problem({"p": {"flag": True}}, "p", self.FIELDS, booleans={"flag"}), [])
        self.assertEqual(
            contract._policy_problem({"p": {"flag": 1}}, "p", self.FIELDS, booleans={"flag"}),
            ["orchestration p.flag must be a boolean"],
        )


class ContextWindowPolicyTests(unittest.TestCase):
    def _problems(self, policy: dict[str, object]) -> list[str]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        config = {**_config(), "concurrency_budget": 1, "verification_commands": ["x"], "context_package_policy": policy}
        path = directory / "orchestration.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return contract.health_problems(path, directory / "missing-roles")

    def test_max_tokens_must_fit_window_minus_reserve(self) -> None:
        message = "orchestration context_package_policy.max_tokens must fit inside context_window_tokens minus reserved_prompt_tokens"
        self.assertIn(message, self._problems({"max_tokens": 900, "context_window_tokens": 1000, "reserved_prompt_tokens": 200}))
        self.assertNotIn(message, self._problems({"max_tokens": 800, "context_window_tokens": 1000, "reserved_prompt_tokens": 200}))

    def test_bool_is_not_an_integer_for_the_window_check(self) -> None:
        message = "orchestration context_package_policy.max_tokens must fit inside context_window_tokens minus reserved_prompt_tokens"
        self.assertNotIn(message, self._problems({"max_tokens": True, "context_window_tokens": 1, "reserved_prompt_tokens": 5}))


if __name__ == "__main__":
    unittest.main()
