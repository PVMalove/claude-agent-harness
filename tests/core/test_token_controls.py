#!/usr/bin/env python3
"""Focused regressions for bounded orchestration cost controls."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.orchestration import contract, coordinator
from harness.orchestration.core import config, constants
from harness.orchestration.ledger import LifecycleLedger
from harness.orchestration.workflow import batch, context_package, reports


class TokenControlTests(unittest.TestCase):
    """Набор тестов для контроля лимитов токенов и расходов оркестрации."""

    def _scope_args(self, **overrides: object) -> argparse.Namespace:
        """Создать аргументы командной строки с оценками объема задачи."""
        values: dict[str, object] = {
            "expected_file": ["services/orders/checkout.py"],
            "expected_service": ["orders"],
            "expected_changed_lines": 120,
            "expected_context_tokens": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_preflight_accepts_a_bounded_ticket_and_records_conservative_context_floor(
        self,
    ) -> None:
        """Проверить, что предварительная проверка принимает задачу с ограниченным объемом и фиксирует нижний порог токенов."""
        result = batch._scope_preflight(
            {},
            "#1",
            "orders",
            ["protect checkout lock"],
            ["none"],
            self._scope_args(),
        )

        self.assertEqual(result["status"], "pass")
        self.assertGreaterEqual(result["expected_context_tokens"], 4_400)

    def test_preflight_rejects_a_ticket_that_exceeds_file_budget_before_dispatch(
        self,
    ) -> None:
        """Проверить отклонение задачи, превышающей бюджет файлов, до отправки диспетчером."""
        with self.assertRaisesRegex(
            coordinator.CoordinatorError, "expected_files=13 exceeds 12"
        ):
            batch._scope_preflight(
                {},
                "#372",
                "orders",
                ["one"],
                ["none"],
                self._scope_args(
                    expected_file=[
                        f"services/orders/file-{index}.py" for index in range(13)
                    ]
                ),
            )

    def test_preflight_requires_declared_scope_estimates_by_default(self) -> None:
        """Проверить обязательность указания предварительных оценок объема задачи по умолчанию."""
        with self.assertRaisesRegex(coordinator.CoordinatorError, "--expected-file"):
            batch._scope_preflight(
                {},
                "#1",
                "orders",
                ["one"],
                ["none"],
                self._scope_args(
                    expected_file=[], expected_service=[], expected_changed_lines=None
                ),
            )

    def test_continuation_count_is_scoped_to_one_dispatch(self) -> None:
        """Проверить, что подсчет продолжений изолирован в рамках одного запуска диспетчера."""
        batch = {
            "coordinator_decisions": [
                {"dispatch_id": "dispatch-a", "decision": "continue"},
                {"dispatch_id": "dispatch-a", "decision": "continue-automatic"},
                {"dispatch_id": "dispatch-b", "decision": "continue-automatic"},
            ]
        }
        self.assertEqual(reports._continuation_counts(batch, "dispatch-a"), (2, 1))

    def test_context_package_policy_defaults_symbol_graph_depth_to_two(self) -> None:
        """Проверить значение глубины графа символов по умолчанию (равное 2) в политике пакета контекста."""
        self.assertEqual(config._context_package_policy({})["symbol_graph_depth"], 2)

    def test_context_package_policy_honours_a_configured_symbol_graph_depth(
        self,
    ) -> None:
        """Проверить применение настроенной глубины графа символов из политики пакета контекста."""
        policy = config._context_package_policy(
            {"context_package_policy": {"symbol_graph_depth": 4}}
        )
        self.assertEqual(policy["symbol_graph_depth"], 4)

    def test_context_package_policy_defaults_max_related_tests_to_twenty_five(
        self,
    ) -> None:
        """Проверить значение максимума связанных тестов по умолчанию (25) в политике пакета контекста."""
        self.assertEqual(config._context_package_policy({})["max_related_tests"], 25)

    def test_context_package_policy_honours_a_configured_max_related_tests(
        self,
    ) -> None:
        """Проверить применение настроенного максимума связанных тестов из политики пакета контекста."""
        policy = config._context_package_policy(
            {"context_package_policy": {"max_related_tests": 10}}
        )
        self.assertEqual(policy["max_related_tests"], 10)

    def test_context_package_policy_defaults_section_index_min_tokens(self) -> None:
        """Проверить значение порога токенов по умолчанию для индекса разделов markdown."""
        self.assertEqual(
            config._context_package_policy({})["section_index_min_tokens"], 20_000
        )

    def test_persist_context_package_passes_the_section_index_threshold_to_the_builder(
        self,
    ) -> None:
        """Проверить передачу порога индекса разделов в функцию сборки пакета контекста."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp)
            (repo / ".harness").mkdir()
            (repo / ".harness" / "project.json").write_text("{}", encoding="utf-8")
            batch = {
                "batch_id": "batch-1",
                "base_commit": "0" * 40,
                "context_packages": [],
            }
            with (
                mock.patch.object(
                    config,
                    "_config",
                    return_value={
                        "context_package_policy": {"section_index_min_tokens": 1234}
                    },
                ),
                mock.patch.object(
                    context_package,
                    "build_context_package",
                    side_effect=RuntimeError("stop after capturing the call"),
                ) as build,
                self.assertRaises(RuntimeError),
            ):
                context_package._persist_context_package(
                    repo,
                    repo,
                    LifecycleLedger(repo),
                    batch,
                    role="shared",
                    snapshot="1" * 40,
                    inclusion_reason="test",
                )

        self.assertEqual(build.call_args.kwargs["section_index_min_tokens"], 1234)

    def test_persist_context_package_rejects_a_token_override_above_policy(
        self,
    ) -> None:
        """Проверить отклонение переопределения лимита токенов, превышающего значение из политики."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp)
            (repo / ".harness").mkdir()
            (repo / ".harness" / "project.json").write_text("{}", encoding="utf-8")
            batch = {
                "batch_id": "batch-1",
                "base_commit": "0" * 40,
                "context_packages": [],
            }
            above_default_policy = (
                constants.DEFAULT_CONTEXT_PACKAGE_POLICY["max_tokens"] + 1
            )
            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "exceeds the configured"
            ):
                context_package._persist_context_package(
                    repo,
                    repo,
                    LifecycleLedger(repo),
                    batch,
                    role="shared",
                    snapshot="1" * 40,
                    inclusion_reason="test",
                    max_package_tokens=above_default_policy,
                )

    def test_tokens_fields_are_not_secrets_but_session_token_is(self) -> None:
        """Проверить, что поля токенов учета не маскируются как секреты, в отличие от session_token."""
        self.assertIsNone(constants.SENSITIVE_KEY.search("input_tokens"))
        self.assertIsNotNone(constants.SENSITIVE_KEY.search("session_token"))

    def test_invalid_reasoning_effort_is_rejected_before_dispatch(self) -> None:
        """Проверить отклонение некорректного значения reasoning_effort до отправки задачи."""
        with self.assertRaisesRegex(contract.ContractError, "assignment effort"):
            contract._valid_effort("highда")

    def test_communication_policy_defaults_to_english_protocol_and_russian_reports(
        self,
    ) -> None:
        """Проверить, что политика коммуникации по умолчанию требует английский для агентов и русский для отчетов."""
        self.assertEqual(
            config._communication_policy({}),
            {"agent_to_agent_language": "en", "coordinator_report_language": "ru"},
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "English.*Russian"):
            config._communication_policy(
                {
                    "communication_policy": {
                        "agent_to_agent_language": "ru",
                        "coordinator_report_language": "en",
                    }
                },
            )

    def test_pre_approval_batch_shape_remains_usable_without_state_edit(self) -> None:
        """Проверить совместимость структуры батча до утверждения без изменения состояния."""
        batch_id = "batch-372-abc"
        legacy_fields = {
            "batch_id": batch_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "base_commit": "a" * 40,
            "integration_ref": "main",
            "branch_start_commit": "a" * 40,
            "ticket": "#372",
            "branch": "feature/issue-372-legacy",
            "worktree": "C:/worktree",
            "zone": "repository",
            "definition_of_done": ["preserve legacy batch"],
            "prohibited_changes": ["secrets"],
            "developer_verification_commands": ["test"],
            "verification_commands": ["test"],
            "required_gates": ["none"],
            "dependencies": ["none"],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            LifecycleLedger(root).ensure()
            records = LifecycleLedger(root).records_root()
            (records / "batches" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields),
                encoding="utf-8",
            )
            (records / "plans" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields),
                encoding="utf-8",
            )

            coordinator._validate_batch_integrity(root, legacy_fields)

    def test_pre_approval_shape_rejects_one_sided_approval_policy(self) -> None:
        """Проверить отклонение одностороннего указания политики утверждения между планом и батчем."""
        batch_id = "batch-372-abc"
        legacy_fields = {
            "batch_id": batch_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "base_commit": "a" * 40,
            "integration_ref": "main",
            "branch_start_commit": "a" * 40,
            "ticket": "#372",
            "branch": "feature/issue-372-legacy",
            "worktree": "C:/worktree",
            "zone": "repository",
            "definition_of_done": ["preserve legacy batch"],
            "prohibited_changes": ["secrets"],
            "developer_verification_commands": ["test"],
            "verification_commands": ["test"],
            "required_gates": ["none"],
            "dependencies": ["none"],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            LifecycleLedger(root).ensure()
            records = LifecycleLedger(root).records_root()
            (records / "batches" / f"{batch_id}.json").write_text(
                json.dumps({**legacy_fields, "approval_policy": "manual_all"}),
                encoding="utf-8",
            )
            (records / "plans" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                coordinator.CoordinatorError, "batch record is incomplete"
            ):
                coordinator._validate_batch_integrity(
                    root, {**legacy_fields, "approval_policy": "manual_all"}
                )


if __name__ == "__main__":
    unittest.main()
