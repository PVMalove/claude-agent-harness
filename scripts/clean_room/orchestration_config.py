"""Проверка конфига оркестрации в health: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    HARNESS,
    fail_output,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Проверка конфига оркестрации в health.

    Читает из контекста: `orchestration_config`, `orchestration_project`, `orchestration_root`,
    `valid_orchestration`.
    """
    orchestration_config = ctx.orchestration_config
    orchestration_project = ctx.orchestration_project
    orchestration_root = ctx.orchestration_root
    valid_orchestration = ctx.valid_orchestration
    in_process_plan = json.loads(json.dumps(valid_orchestration))
    in_process_plan["assignment_plans"]["qa"]["transport"] = "in-process"
    orchestration_config.write_text(
        json.dumps(in_process_plan, indent=2) + "\n", encoding="utf-8"
    )
    run_ok(HARNESS + ["health", str(orchestration_project)])

    invalid_transport = json.loads(json.dumps(valid_orchestration))
    invalid_transport["assignment_plans"]["developer"]["transport"] = "carrier-pigeon"
    orchestration_config.write_text(
        json.dumps(invalid_transport, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "transport must be one of" not in output:
        sys.exit("health accepted an unknown role transport")

    missing_review_assignment = json.loads(json.dumps(valid_orchestration))
    del missing_review_assignment["assignment_plans"]["code-review"]
    orchestration_config.write_text(
        json.dumps(missing_review_assignment, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "must include code-review for the mandatory high-risk role gate" not in output:
        sys.exit(
            "health allowed orchestration config without the code-review role gate"
        )

    role_path = orchestration_root / "roles" / "code-review.md"
    original_role = role_path.read_text(encoding="utf-8")
    role_path.write_text(original_role.replace("  - retry-dlq\n", ""), encoding="utf-8")
    if "  - retry-dlq\n" in role_path.read_text(encoding="utf-8"):
        sys.exit("clean-room test could not remove retry-dlq trigger")
    orchestration_config.write_text(
        json.dumps(valid_orchestration, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "code-review role manifest is missing required risk trigger" not in output:
        sys.exit(
            "health allowed code-review without the complete high-risk trigger contract"
        )
    role_path.write_text(original_role, encoding="utf-8")

    role_path.write_text(
        original_role.replace("mode: read-only", "mode: write"), encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "code-review role manifest must remain read-only" not in output:
        sys.exit("health allowed code-review with a write mode")
    role_path.write_text(original_role, encoding="utf-8")

    role_path.write_text(
        original_role.replace(
            "  - code-review\nrisk_triggers:", "  - backend-development\nrisk_triggers:"
        ),
        encoding="utf-8",
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if (
        "code-review role manifest must require the code-review capability"
        not in output
    ):
        sys.exit("health allowed code-review without its required capability")
    role_path.write_text(original_role, encoding="utf-8")

    invalid_role = json.loads(json.dumps(valid_orchestration))
    invalid_role["assignment_plans"]["unknown-role"] = invalid_role["assignment_plans"][
        "developer"
    ]
    orchestration_config.write_text(
        json.dumps(invalid_role, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "unknown role" not in output:
        sys.exit("health did not explain unknown orchestration role")

    invalid_profile = json.loads(json.dumps(valid_orchestration))
    invalid_profile["assignment_plans"]["developer"]["runtimes"]["codex"][
        "profiles"
    ] = ["missing-profile"]
    orchestration_config.write_text(
        json.dumps(invalid_profile, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "unknown provider profile" not in output:
        sys.exit("health did not explain unknown provider profile")

    incompatible = json.loads(json.dumps(valid_orchestration))
    incompatible["provider_profiles"]["backend-default"]["capabilities"] = [
        "independent-verification"
    ]
    orchestration_config.write_text(
        json.dumps(incompatible, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "incompatible capability" not in output:
        sys.exit("health did not explain incompatible provider capability")

    invalid_zone = json.loads(json.dumps(valid_orchestration))
    invalid_zone["assignment_plans"]["developer"]["zone"] = "missing-zone"
    orchestration_config.write_text(
        json.dumps(invalid_zone, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "unknown backend zone" not in output:
        sys.exit("health did not explain unknown backend zone")

    policy_override = json.loads(json.dumps(valid_orchestration))
    policy_override["assignment_plans"]["developer"]["mode"] = "read-only"
    orchestration_config.write_text(
        json.dumps(policy_override, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "cannot override role manifest" not in output:
        sys.exit("health allowed an assignment to override role policy")

    credential_field = json.loads(json.dumps(valid_orchestration))
    credential_field["provider_profiles"]["backend-default"]["api_key"] = (
        "environment-only"
    )
    orchestration_config.write_text(
        json.dumps(credential_field, indent=2) + "\n", encoding="utf-8"
    )
    output = fail_output(HARNESS + ["health", str(orchestration_project)])
    if "credentials" not in output:
        sys.exit("health did not reject credential-shaped provider profile data")
