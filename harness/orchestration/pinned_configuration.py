"""Approval-bound config context for evidence ingress of an existing dispatch."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

from harness.json_types import JsonObject
from harness.orchestration.config_boundary import NORMALIZATION_VERSION
from harness.orchestration.core.system_fields import CONTRACT_VERSION
from harness.orchestration.core.utils import CoordinatorError, _canonical
from harness.orchestration.contract import reject_sensitive

SNAPSHOT_FIELDS = frozenset(
    {
        "contract",
        "normalizer",
        "source_sha256",
        "canonical_sha256",
        "config",
        "project",
        "role",
    }
)
TRANSITION_FIELD = "configuration_sha256"


def snapshot(config: JsonObject, project: JsonObject, role: JsonObject) -> JsonObject:
    body: JsonObject = {
        "contract": CONTRACT_VERSION,
        "normalizer": NORMALIZATION_VERSION,
        "config": dict(config),
        "project": project,
        "role": role,
    }
    reject_sensitive(body, "configuration snapshot")
    canonical = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    return copy.deepcopy(
        {
            **body,
            "source_sha256": getattr(config, "source_sha256", canonical),
            "canonical_sha256": canonical,
        }
    )


def read_snapshot(dispatch: JsonObject) -> JsonObject:
    """Fail closed rather than reconstructing an approved policy from current files."""
    policy = dispatch.get("orchestration_policy")
    value = policy.get("configuration") if isinstance(policy, dict) else None
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise CoordinatorError(
            "dispatch has no usable pinned configuration",
            remedy="capture evidence without acceptance; restore the approved immutable snapshot before retrying",
        )
    reject_sensitive(value, "configuration snapshot")
    valid = (
        value["contract"] == CONTRACT_VERSION
        and value["normalizer"] == NORMALIZATION_VERSION
    )
    valid = valid and all(
        isinstance(value[key], dict) for key in ("config", "project", "role")
    )
    valid = valid and all(
        isinstance(value[key], str) and re.fullmatch("[0-9a-f]{64}", value[key])
        for key in ("source_sha256", "canonical_sha256")
    )
    body = {
        key: item
        for key, item in value.items()
        if key not in {"source_sha256", "canonical_sha256"}
    }
    digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    transition = dispatch.get("transition")
    if (
        not valid
        or digest != value["canonical_sha256"]
        or not isinstance(transition, dict)
        or transition.get(TRANSITION_FIELD) != digest
        or value["role"].get("name") != dispatch.get("role")
    ):
        raise CoordinatorError(
            "dispatch pinned configuration failed integrity or version validation",
            remedy="restore the approved snapshot and its approval binding; do not replace it with live config",
        )
    return copy.deepcopy(value)


def capture_unaccepted(
    root: Path, dispatch: JsonObject, payload: JsonObject, error: CoordinatorError
) -> Path:
    """Preserve safe input without a report/checkpoint record or lifecycle decision."""
    from harness.orchestration.core.utils import _safe_id
    from harness.orchestration.ledger.lifecycle import LifecycleLedger

    reject_sensitive(payload, "unaccepted evidence")
    ledger = LifecycleLedger(root)
    dispatch_id = _safe_id(dispatch.get("dispatch_id"), "dispatch")
    record: JsonObject = {
        "dispatch_id": dispatch_id,
        "accepted": False,
        "input": payload,
        "diagnostic": error.message,
        "remedy": error.remedy,
    }
    identifier = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
    path = (
        ledger.records_root()
        / "unaccepted-evidence"
        / f"{dispatch_id}-{identifier}.json"
    )
    if path.parent.is_symlink():
        raise CoordinatorError(
            "unaccepted evidence directory is a symbolic link",
            remedy="restore a real evidence directory inside the selected ledger",
        )
    ledger.write_immutable(path, record, artifact=True)
    return path
