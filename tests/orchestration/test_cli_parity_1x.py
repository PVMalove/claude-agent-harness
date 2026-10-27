"""The golden test of CLI parity across the cutover (ADR 0020, section 5, step 5).

The last 1.x release writes a state with a batch in every ``DispatchState`` that the cutover admits
(``release_1x``). Its own CLI answers ``dispatch status``, ``batch auto-report``, ``batch
decision-packet`` and ``qa status``; then ``harness-runtime migrate`` imports the state, and the
CLI of this tree answers the same four commands through the real runtime. The test compares the
whole canonical-JSON answers. It excludes only the fields that a ``migration-repair`` audit record
of the store names for the record it repaired and the closed list of fields the CLI added after
1.5.1 (``ADDED_AFTER_1X``). As the plan of the test allows, the elapsed-time fields that
``test_runtime_store_read`` drops are not compared by value: ``silent_seconds`` is masked, so its key
stays compared, and ``startup_elapsed_seconds`` is dropped from both answers. The report address is
cut to its name. Any other added, missing or changed field fails the test.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from harness.orchestration.ledger.lifecycle import JsonObject, JsonValue

from .release_1x import ROOT, STATE_REL, build_1x_state, cli_1x
from .runtime_store_support import ServingRuntime, coordinator_with, migrate

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)

REPAIR_RS = ROOT / "native/harness-runtime/crates/state/src/repair.rs"
#: The fields each repair rule may change; ``repair.rs`` is the source, and the guard below fails
#: when it changes.
RULE_FIELDS = {
    "781-risk-reassessment-pending-next": ("risk_reassessment_pending_next",)
}
#: The closed list of fields that the CLI added after 1.5.1, per command; ``*`` is every list item.
#: They are dropped from the 2.0.0 answer only, so a 1.x answer that has one still fails the test.
ADDED_AFTER_1X: dict[str, tuple[tuple[str, ...], ...]] = {
    "dispatch status": (
        ("dispatches", "*", "adapter_receipt"),
        ("dispatches", "*", "worker"),
        ("dispatches", "*", "startup", "deadline_exceeded"),
        ("dispatches", "*", "startup", "failure"),
        ("dispatches", "*", "startup", "observation"),
        ("dispatches", "*", "startup", "startup_timeout_seconds"),
    ),
    "batch decision-packet": (("route_preview", "retry", "failure"),),
}
#: The elapsed-time fields of ``dispatch status``; their values depend on the clock.
#: ``silent_seconds`` is in both answers and is masked; ``startup_elapsed_seconds`` is dropped.
MASKED_ELAPSED = "silent_seconds"
DROPPED_ELAPSED = "startup_elapsed_seconds"


def _commands(ids: dict[str, str]) -> dict[str, list[str]]:
    return {
        "dispatch status": ["dispatch", "status"],
        "batch auto-report": ["batch", "auto-report", "--batch", ids["auto"]],
        "batch decision-packet": [
            "batch",
            "decision-packet",
            "--batch",
            ids["reported"],
        ],
        "qa status": ["qa", "status"],
    }


def _json(stdout: str) -> JsonObject:
    answer: JsonObject = json.loads(stdout)
    return answer


def _comparable(name: str, answer: JsonObject) -> JsonObject:
    """The answer without the elapsed time and with the report address cut to its name."""
    comparable: JsonObject = json.loads(json.dumps(answer))
    if name == "dispatch status":
        entries = comparable["dispatches"]
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            startup = entry["startup"]
            assert isinstance(startup, dict)
            if entry.get(MASKED_ELAPSED) is not None:
                entry[MASKED_ELAPSED] = "<elapsed>"
            startup.pop(DROPPED_ELAPSED, None)
    if name == "batch decision-packet":
        report = comparable["report"]
        assert isinstance(report, str)
        comparable["report"] = report.rsplit("/reports/", 1)[1]
    return comparable


def _without(value: JsonValue, path: tuple[str, ...]) -> None:
    """Drop the field at ``path`` from ``value`` in place, where it exists."""
    head, rest = path[0], path[1:]
    if head == "*":
        items = value if isinstance(value, list) else []
        for item in items:
            _without(item, rest)
        return
    if not isinstance(value, dict) or head not in value:
        return
    if rest:
        _without(value[head], rest)
    else:
        del value[head]


def _without_added(name: str, answer: JsonObject) -> JsonObject:
    """The 2.0.0 answer without the fields of the closed list ``ADDED_AFTER_1X``."""
    for path in ADDED_AFTER_1X.get(name, ()):
        _without(answer, path)
    return answer


def _repairs(repo: Path) -> dict[str, tuple[str, ...]]:
    """The batch ids the import repaired, with the fields the rule of each record may change."""
    with sqlite3.connect(
        f"file:{repo / STATE_REL / 'runtime.db'}?mode=ro", uri=True
    ) as store:
        rows = store.execute(
            "SELECT details_json FROM audit WHERE action = 'migration-repair'"
        ).fetchall()
    found: dict[str, tuple[str, ...]] = {}
    for (details,) in rows:
        record = json.loads(details)
        batch_id = Path(record["path"]).stem
        assert record["path"].split("/")[-2] == "batches", record
        found[batch_id] = RULE_FIELDS[record["rule"]]
    return found


def _without_repairs(
    value: JsonValue, repairs: dict[str, tuple[str, ...]]
) -> JsonValue:
    """``value`` without the repaired fields of an object that names a repaired batch."""
    if isinstance(value, list):
        return [_without_repairs(item, repairs) for item in value]
    if not isinstance(value, dict):
        return value
    fields = repairs.get(str(value.get("batch_id")), ())
    return {
        key: _without_repairs(item, repairs)
        for key, item in value.items()
        if key not in fields
    }


def _differences(before: JsonValue, after: JsonValue, at: str = "") -> list[str]:
    """The paths at which two answers differ, for the failure message."""
    if isinstance(before, dict) and isinstance(after, dict):
        found: list[str] = []
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                found.append(f"{at}/{key}")
            else:
                found.extend(_differences(before[key], after[key], f"{at}/{key}"))
        return found
    if (
        isinstance(before, list)
        and isinstance(after, list)
        and len(before) == len(after)
    ):
        return [
            path
            for index, (one, other) in enumerate(zip(before, after, strict=True))
            for path in _differences(one, other, f"{at}/{index}")
        ]
    return [] if before == after else [f"{at}: {before!r} != {after!r}"]


def test_the_rule_fields_are_the_ones_repair_rs_names() -> None:
    source = REPAIR_RS.read_text(encoding="utf-8")
    rules = dict(re.findall(r'pub const (RULE_\w+): &str = "([^"]+)";', source))
    assert set(rules.values()) == set(RULE_FIELDS)
    for constant, rule in rules.items():
        names = re.search(rf"{constant} => Some\(&\[([^\]]*)\]\)", source)
        assert names is not None, constant
        assert tuple(re.findall(r'"([^"]+)"', names.group(1))) == RULE_FIELDS[rule]


def test_cli_1x_before_the_import_and_cli_2_after_it_give_the_same_answers(
    release_1x: Path, tmp_path: Path
) -> None:
    repo, ids = build_1x_state(release_1x, tmp_path)
    commands = _commands(ids)
    before: dict[str, JsonObject] = {}
    for name, command in commands.items():
        result = cli_1x(release_1x, repo, *command)
        assert result.returncode == 0, (name, result.stderr)
        before[name] = _comparable(name, _json(result.stdout))
    migrated = migrate(repo)
    assert migrated.returncode == 0, migrated.stderr
    repairs = _repairs(repo)
    assert set(repairs) == {ids["repair_781"]}

    runtime = ServingRuntime(repo)
    try:
        after: dict[str, JsonObject] = {}
        for name, command in commands.items():
            result = coordinator_with(repo, runtime, *command)
            assert result.returncode == 0, (name, result.stderr)
            after[name] = _without_added(name, _comparable(name, _json(result.stdout)))
    finally:
        runtime.stop()

    differences = {
        name: sorted(
            {
                re.sub(r"/\d+(?=/|:|$)", "/*", path)
                for path in _differences(
                    _without_repairs(before[name], repairs),
                    _without_repairs(after[name], repairs),
                )
            }
        )
        for name in commands
    }
    assert differences == {name: [] for name in commands}
