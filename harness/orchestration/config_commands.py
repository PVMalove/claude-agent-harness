"""Configuration CLI handlers; no lifecycle mutation or terminal interaction."""

from __future__ import annotations

import argparse

from harness.configuration import Bundle, read_config_bundle
from harness.configuration_store import ConfigurationStore, json_bytes
from harness.configuration import bundle_diff
from harness.diagnostics import Diagnostic
from harness.json_types import JsonObject
from harness.orchestration.config_boundary import inspect_configuration
from harness.orchestration.core.utils import _repo


def validate_config(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    try:
        bundle = read_config_bundle(repo)
    except (ValueError, OSError) as exc:
        return {
            "valid": False,
            "document": None,
            "diagnostics": [
                Diagnostic(
                    "config-read",
                    "orchestration.json",
                    (),
                    str(exc),
                    "readable coherent configuration",
                ).to_dict()
            ],
        }
    result = inspect_configuration(
        repo, bundle["orchestration.json"], canonical=args.canonical
    )
    output = result.to_dict()
    if args.dry_run and not args.fix:
        output["valid"] = False
        output["diagnostics"].append(
            Diagnostic(
                "config-options", "orchestration.json", (), "--dry-run requires --fix"
            ).to_dict()
        )
    if args.fix and result.valid:
        # Persist only the authored document after normalization, never injected defaults.
        changes: Bundle = {"orchestration.json": json_bytes(result.document)}
        output["diff"] = bundle_diff(
            {"orchestration.json": bundle["orchestration.json"]},
            changes,
            "current",
            "canonical",
        )
        if (
            not args.dry_run
            and changes["orchestration.json"] != bundle["orchestration.json"]
        ):
            try:
                backup = ConfigurationStore(repo).apply(changes, expected=bundle)
                output["backup_version"] = backup.id
            except (ValueError, OSError) as exc:
                output["valid"] = False
                output["diagnostics"].append(
                    Diagnostic(
                        "config-write", "orchestration.json", (), str(exc)
                    ).to_dict()
                )
    return output
