"""Stage evidence ``stage-evidence.v1``: one typed record of what a verification stage did.

Every verification stage -- a gate-runner command, a ``scripts/verify.py`` stage or, later, a stage
of the Rust QA executor -- is recorded in this schema (ADR 0023, section 5). The Rust twin lives in
``native/harness-runtime/crates/contracts/src/stage_evidence.rs``; one golden fixture
(``contracts/tests/fixtures/stage-evidence.v1.json``) is read by the tests of both sides, and the
closed vocabularies below are checked against the ``systemFields.v1`` registry, so the two writers
cannot drift apart.

The module depends only on the standard library and :mod:`harness.errors`: the ``gate_runner``
package is also shipped as a standalone resource.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ..errors import HarnessError

SCHEMA = "stage-evidence.v1"
# The exact values of the registry fields ``report.check_result``, ``verification_plan.selection``
# and ``verification_plan.stage_kind``.
RESULTS = ("pass", "fail", "not_run")
SELECTIONS = ("full", "focused", "failed-only")
KINDS = ("lint", "typecheck", "pytest", "clean-room", "custom")

DOCUMENT_KEYS = frozenset({"schema", "stages"})
STAGE_KEYS = frozenset(
    {
        "stage_id",
        "kind",
        "selection",
        "candidate_sha",
        "plan_sha256",
        "image_digest",
        "environment_fingerprint",
        "result",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "failed_nodeids",
    }
)
REF_KEYS = frozenset({"path", "sha256"})
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class StageEvidenceError(HarnessError):
    """A stage-evidence document breaks the ``stage-evidence.v1`` schema."""


def _refuse(where: str, problem: str) -> StageEvidenceError:
    return StageEvidenceError(
        f"{where}: {problem}",
        remedy="write the document exactly as stage-evidence.v1 describes it "
        "(see contracts/tests/fixtures/stage-evidence.v1.json)",
    )


def _object(value: object, where: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _refuse(where, "must be an object")
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing or unknown:
        raise _refuse(
            where,
            "must have exactly the keys "
            + ", ".join(sorted(keys))
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unknown: {', '.join(unknown)}" if unknown else ""),
        )
    return cast(Mapping[str, object], value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _refuse(where, "must be a non-empty string")
    return value


def _member(value: object, where: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _refuse(where, f"must be one of: {', '.join(allowed)}")
    return value


def _digest(value: object, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _refuse(where, "must be a lower-case hex digest")
    return value


def _optional_digest(value: object, where: str) -> str | None:
    return None if value is None else _digest(value, where, _SHA256)


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse(where, "must be an integer")
    return value


@dataclass(frozen=True)
class FileRef:
    """A stored output stream: where it lies (relative to the evidence root) and its sha256."""

    path: str
    sha256: str

    @classmethod
    def from_json(cls, value: object, where: str) -> FileRef:
        """Validate and read one stream reference."""
        data = _object(value, where, REF_KEYS)
        return cls(
            _text(data["path"], f"{where}.path"),
            _digest(data["sha256"], f"{where}.sha256", _SHA256),
        )

    def to_json(self) -> dict[str, object]:
        """The JSON form: exactly ``path`` and ``sha256``."""
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class StageEvidence:
    """The evidence of one stage. A stage that was never reached has no exit code and no streams."""

    stage_id: str
    kind: str
    selection: str
    candidate_sha: str
    plan_sha256: str | None
    image_digest: str | None
    environment_fingerprint: str | None
    result: str
    exit_code: int | None
    stdout: FileRef | None
    stderr: FileRef | None
    duration_ms: int
    failed_nodeids: tuple[str, ...]

    @classmethod
    def from_json(cls, value: object, where: str) -> StageEvidence:
        """Validate one stage record: exact keys, closed vocabularies and cross-field rules."""
        data = _object(value, where, STAGE_KEYS)
        exit_code = (
            None
            if data["exit_code"] is None
            else _integer(data["exit_code"], f"{where}.exit_code")
        )
        duration = _integer(data["duration_ms"], f"{where}.duration_ms")
        if duration < 0:
            raise _refuse(f"{where}.duration_ms", "must not be negative")
        nodeids = data["failed_nodeids"]
        if not isinstance(nodeids, list) or not all(
            isinstance(item, str) and item for item in nodeids
        ):
            raise _refuse(
                f"{where}.failed_nodeids", "must be a list of non-empty strings"
            )
        stage = cls(
            stage_id=_text(data["stage_id"], f"{where}.stage_id"),
            kind=_member(data["kind"], f"{where}.kind", KINDS),
            selection=_member(data["selection"], f"{where}.selection", SELECTIONS),
            candidate_sha=_digest(
                data["candidate_sha"], f"{where}.candidate_sha", _COMMIT
            ),
            plan_sha256=_optional_digest(data["plan_sha256"], f"{where}.plan_sha256"),
            image_digest=_optional_digest(
                data["image_digest"], f"{where}.image_digest"
            ),
            environment_fingerprint=_optional_digest(
                data["environment_fingerprint"], f"{where}.environment_fingerprint"
            ),
            result=_member(data["result"], f"{where}.result", RESULTS),
            exit_code=exit_code,
            stdout=None
            if data["stdout"] is None
            else FileRef.from_json(data["stdout"], f"{where}.stdout"),
            stderr=None
            if data["stderr"] is None
            else FileRef.from_json(data["stderr"], f"{where}.stderr"),
            duration_ms=duration,
            failed_nodeids=tuple(cast(list[str], nodeids)),
        )
        stage._check_consistency(where)
        return stage

    def _check_consistency(self, where: str) -> None:
        """A result must agree with its exit code, its streams and its failed nodeids."""
        ran = self.exit_code is not None
        if ran != (self.stdout is not None) or ran != (self.stderr is not None):
            raise _refuse(
                where,
                "stdout and stderr are present exactly when the stage has an exit code",
            )
        if self.result == "pass" and self.exit_code != 0:
            raise _refuse(f"{where}.result", "pass needs exit_code 0")
        if self.result == "fail" and (self.exit_code is None or self.exit_code == 0):
            raise _refuse(f"{where}.result", "fail needs a non-zero exit_code")
        if self.result != "fail" and self.failed_nodeids:
            raise _refuse(
                f"{where}.failed_nodeids", "only a failed stage lists failed nodeids"
            )
        if self.result == "not_run" and self.exit_code == 0:
            raise _refuse(
                f"{where}.result", "not_run cannot carry the exit code of a success"
            )

    def to_json(self) -> dict[str, object]:
        """The JSON form with every key present (``null`` for an absent value)."""
        return {
            "stage_id": self.stage_id,
            "kind": self.kind,
            "selection": self.selection,
            "candidate_sha": self.candidate_sha,
            "plan_sha256": self.plan_sha256,
            "image_digest": self.image_digest,
            "environment_fingerprint": self.environment_fingerprint,
            "result": self.result,
            "exit_code": self.exit_code,
            "stdout": None if self.stdout is None else self.stdout.to_json(),
            "stderr": None if self.stderr is None else self.stderr.to_json(),
            "duration_ms": self.duration_ms,
            "failed_nodeids": list(self.failed_nodeids),
        }


@dataclass(frozen=True)
class StageEvidenceDocument:
    """The evidence document of one verification run: its stages in execution order."""

    stages: tuple[StageEvidence, ...]

    @classmethod
    def from_json(cls, value: object) -> StageEvidenceDocument:
        """Validate a whole document."""
        data = _object(value, "stage evidence", DOCUMENT_KEYS)
        if data["schema"] != SCHEMA:
            raise _refuse("stage evidence.schema", f"must be {SCHEMA!r}")
        stages = data["stages"]
        if not isinstance(stages, list):
            raise _refuse("stage evidence.stages", "must be a list")
        return cls(
            tuple(
                StageEvidence.from_json(item, f"stages[{index}]")
                for index, item in enumerate(stages)
            )
        )

    def to_json(self) -> dict[str, object]:
        """The JSON form of the document."""
        return {
            "schema": SCHEMA,
            "stages": [stage.to_json() for stage in self.stages],
        }

    def dumps(self) -> str:
        """Canonical text: two-space indent, insertion order of the schema, trailing newline."""
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n"


def parse(text: str) -> StageEvidenceDocument:
    """Read a document from its JSON text."""
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise _refuse("stage evidence", f"is not valid JSON ({exc})") from exc
    return StageEvidenceDocument.from_json(value)
