"""The batch contract of ``ledger validate`` is a precondition of ``harness-runtime migrate``.

ADR 0020, section 5, step 1: a state that the 1.x ``ledger validate`` rejects is refused with exit
code 2 and nothing changes. Every case runs both commands on the same state, so the Rust port in
``crates/state/src/policy.rs`` cannot drift from ``history._validate_batch_integrity`` unnoticed:
a state is built with every sealed record kind a batch carries, it passes both commands, and then
one rule at a time is broken.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core.constants import DispatchState
from harness.orchestration.core.utils import sealed
from harness.orchestration.ledger.lifecycle import _canonical
from harness.orchestration.ledger.recovery_chain import event_audit
from harness.orchestration.workflow import startup

from .runtime_store_support import (
    BATCH_ID,
    DISPATCH_IDS,
    SNAPSHOT_COMMIT,
    STATE_REL,
    build_state,
    coordinator,
    load as load_object,
    migrate,
    tree,
)

FIRST, SECOND = DISPATCH_IDS
MOMENT = "2026-10-02T00:00:00+00:00"
AUTO = "policy:auto"
HEAD = "5" * 40
Edit = Callable[[JsonObject], None]


def load(path: Path) -> JsonObject:
    return dict(load_object(path))


def _hash(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reseal(record: JsonObject) -> JsonObject:
    return sealed(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )


def _generation(repo: Path) -> Path:
    """The selected generation, read from the pointer: a broken state must stay reachable."""
    state = repo / STATE_REL
    return state / "generations" / str(load(state / "ledger.json")["generation"])


def _write(path: Path, value: JsonObject) -> None:
    path.write_text(_canonical(value), encoding="utf-8", newline="\n")


def edit_batch(repo: Path, edit: Edit) -> None:
    path = _generation(repo) / "batches" / f"{BATCH_ID}.json"
    batch = load(path)
    edit(batch)
    _write(path, batch)


def edit_plan(repo: Path, edit: Edit) -> None:
    path = _generation(repo) / "plans" / f"{BATCH_ID}.json"
    plan = load(path)
    edit(plan)
    _write(path, plan)


def write_audit(repo: Path, decision: JsonObject) -> None:
    audit: JsonObject = {
        "audit_id": f"audit-{uuid.uuid4()}",
        "at": MOMENT,
        "action": "transition",
        "details": {"path": f"batches/{BATCH_ID}.json", "decision": decision},
    }
    audit["record_sha256"] = _hash(audit)
    _write(_generation(repo) / "audit" / f"{audit['audit_id']}.json", audit)


def _carried(audit: JsonObject) -> JsonObject:
    """The sealed record an audit record of a recovery event or of startup evidence carries."""
    evidence = audit["details"].get("decision", {}).get("evidence", {})
    carried: JsonObject = (
        evidence.get("recovery_event") or evidence.get("startup") or {}
    )
    return carried


def rewrite_audit(repo: Path, old_sha: str, decision: JsonObject) -> None:
    """Point the audit record of a sealed record at its edited copy."""
    for path in (_generation(repo) / "audit").glob("*.json"):
        audit = load(path)
        carried = _carried(audit)
        if carried.get("record_sha256") == old_sha:
            audit["details"]["decision"] = decision
            del audit["record_sha256"]
            audit["record_sha256"] = _hash(audit)
            _write(path, audit)
            return
    raise AssertionError("no audit record names the sealed record")


def drop_audit(repo: Path, record_sha: str) -> None:
    for path in (_generation(repo) / "audit").glob("*.json"):
        if _carried(load(path)).get("record_sha256") == record_sha:
            path.unlink()
            return
    raise AssertionError("no audit record names the sealed record")


def edit_event(repo: Path, index: int, edit: Edit) -> None:
    """Edit one recovery event, seal it again and keep its audit record in step."""
    seen: list[JsonObject] = []

    def apply(batch: JsonObject) -> None:
        event = batch["recovery_events"][index]
        seen.append(dict(event))
        edit(event)
        batch["recovery_events"][index] = _reseal(event)
        seen.append(batch["recovery_events"][index])

    edit_batch(repo, apply)
    rewrite_audit(repo, seen[0]["record_sha256"], event_audit(seen[1]))


def edit_point(repo: Path, edit: Edit) -> None:
    """Edit the startup evidence, seal it again and keep its audit record in step."""
    seen: list[JsonObject] = []

    def apply(batch: JsonObject) -> None:
        point = batch["startup_evidence"][0]
        seen.append(dict(point))
        edit(point)
        batch["startup_evidence"][0] = _reseal(point)
        seen.append(batch["startup_evidence"][0])

    edit_batch(repo, apply)
    rewrite_audit(repo, seen[0]["record_sha256"], startup.audit(seen[1]))


def edit_sealed(
    field: str, edit: Edit, *, seal: bool = True, index: int | None = None
) -> Edit:
    """An edit of a sealed batch field (a record, or the ``index``-th record of a list)."""

    def apply(batch: JsonObject) -> None:
        record = batch[field] if index is None else batch[field][index]
        edit(record)
        if seal:
            record = _reseal(record)
        if index is None:
            batch[field] = record
        else:
            batch[field][index] = record

    return apply


def _event(sequence: int, kind: str, approver: str, **evidence: object) -> JsonObject:
    return sealed(
        {
            "sequence": sequence,
            "kind": kind,
            "before_state": "active",
            "state": "paused",
            "next_action": "developer" if kind == "rewind" else None,
            "dispatch_id": SECOND if kind == "rewind" else FIRST,
            "snapshot_commit": SNAPSHOT_COMMIT,
            "transition_digest": None,
            "approved_by": approver,
            "approved_at": MOMENT,
            "note": "an example",
            "evidence": dict(evidence),
        }
    )


def _auto(
    sequence: int, kind: str, dispatch_id: str | None, evidence: JsonObject
) -> JsonObject:
    return sealed(
        {
            "sequence": sequence,
            "kind": kind,
            "dispatch_id": dispatch_id,
            "approved_by": AUTO,
            "approved_at": MOMENT,
            "rationale": "policy",
            "evidence": evidence,
        }
    )


def rich_state(repo: Path) -> None:
    """A state whose one batch carries every kind of sealed record the batch contract checks."""
    build_state(repo, second_status=DispatchState.CANCELLED)
    brief = load(_generation(repo) / "dispatches" / f"{FIRST}.json")
    worktree = load(_generation(repo) / "plans" / f"{BATCH_ID}.json")["worktree"]
    pause = _event(1, "pause", AUTO)
    resume = _event(2, "resume-stop", "operator")
    progress = {
        "source_dispatch_id": SECOND,
        "snapshot_commit": SNAPSHOT_COMMIT,
        "head_commit": HEAD,
        "checkout": worktree,
        "files": {"a/x.py": "6" * 64},
        "index_sha256": "7" * 64,
        "worktree_sha256": "8" * 64,
    }
    rewind = _event(
        3,
        "rewind",
        "operator",
        superseded_dispatches=[SECOND],
        writer_start_commit=HEAD,
        preserved_progress=progress,
    )
    scope = {
        key: brief[key]
        for key in (
            "definition_of_done",
            "dependencies",
            "write_paths",
            "prohibited_changes",
        )
    }
    point = sealed(
        {
            "dispatch_id": FIRST,
            "commit_sha": SNAPSHOT_COMMIT,
            "brief_sha256": _hash(brief),
            "scope_sha256": _hash(scope),
            "worktree": worktree,
            "recorded_at": MOMENT,
        }
    )
    pressure = sealed(
        {
            "pressure_id": "context-pressure-0001",
            "dispatch_id": FIRST,
            "observed_tokens": 90,
            "context_limit": 100,
            "warning_threshold": 80,
            "level": "warning",
            "recorded_at": MOMENT,
            "source": "probe",
            "action_required": False,
            "required_worker_action": None,
        }
    )
    carried = sealed(
        {
            "item_id": "finding-1",
            "source": {"kind": "coordinator-finding"},
            "summary": "An example finding.",
            "files": [],
            "expected_evidence": "A test.",
            "attached_at": MOMENT,
            "attached_by": "operator",
        }
    )
    reported = _auto(
        4,
        "decision",
        FIRST,
        {
            "decision": "accept",
            "report_sha256": None,
            "route_preview": None,
            "reason_category": None,
            "basis": "review",
            "accepted_risks": [],
            "commit_plan_sha256": None,
            "bug_ticket": None,
            "carried_item_ids": [],
        },
    )
    stop = sealed(
        {
            "category": "integrity-failure",
            "reason": "stale",
            "detected_at": MOMENT,
            "detected_by": AUTO,
            "evidence": {},
        }
    )

    def apply(batch: JsonObject) -> None:
        entry = batch["dispatches"][0]
        entry["decision"] = {
            "approved_by": AUTO,
            "routing": {"route": "fix-forward", "reason_category": "code"},
        }
        reported["evidence"]["report_sha256"] = entry["report_sha256"]
        decisions = [
            _auto(
                1,
                "batch-approve",
                None,
                {
                    "plan_sha256": "9" * 64,
                    "scope_preflight_status": "clean",
                    "definition_of_done_items": 1,
                },
            ),
            _auto(
                2,
                "dispatch",
                FIRST,
                {
                    "transition_digest": None,
                    "brief_sha256": entry["brief_sha256"],
                    "lifted_milestones": [],
                    "route_preview": None,
                    "reason_category": None,
                    "report_sha256": None,
                },
            ),
            _auto(
                3,
                "continuation",
                FIRST,
                {
                    "trigger": "context",
                    "checkpoint_id": None,
                    "continuations_spent": 1,
                    "max_continuations": 2,
                },
            ),
            _reseal({**reported, "sequence": 4}),
            _auto(
                5, "carry-over", FIRST, {"report_sha256": None, "carried_item_ids": []}
            ),
        ]
        decisions[3] = _reseal(
            {
                **decisions[3],
                "evidence": {
                    **decisions[3]["evidence"],
                    "report_sha256": entry["report_sha256"],
                },
            }
        )
        batch["dispatches"][1].update(
            {"state": "cancelled", "cancellation": {"runtime_stopped": True}}
        )
        batch["coordinator_approval"] = {"approved_by": AUTO, "approved_at": MOMENT}
        batch["coordinator_decisions"] = [
            {"dispatch_id": FIRST, "decision": "continue", "approved_by": AUTO},
            {"dispatch_id": FIRST, "decision": "carry-over", "approved_by": AUTO},
        ]
        batch["auto_decisions"] = decisions
        batch["auto_stop"] = stop
        batch["auto_report"] = sealed(
            {
                "schema_version": 1,
                "batch_id": BATCH_ID,
                "ticket": "#1",
                "branch": "feature/issue-1-example",
                "outcome": "stopped",
                "stop": stop,
                "candidate_commit": None,
                "decisions": [],
                "accepted_risks": [],
                "findings": [],
                "retries": {},
                "budget": {},
                "commit_plan": [],
                "dod_coverage": [],
                "review": None,
                "qa": None,
                "next_human_action": "look",
                "recorded_at": MOMENT,
            }
        )
        batch["recovery_events"] = [pause, resume, rewind]
        batch["manual_recovery"] = True
        batch["startup_evidence"] = [point]
        batch["context_pressure"] = [pressure]
        batch["carried_items"] = [carried]
        batch["needs_attention"] = True
        batch.update(
            attention_reason="stale-dispatch",
            attention_since=MOMENT,
            last_safe_action="none",
            recommended_human_action="look",
            attention_events=[{"event": "raised", "at": MOMENT}],
            attention_open_keys=["stale-dispatch"],
            attention_acknowledged=[],
        )

    edit_batch(repo, apply)
    for event in (pause, resume, rewind):
        write_audit(repo, event_audit(event))
    write_audit(repo, startup.audit(point))
    dispatch_path = _generation(repo) / "dispatches" / f"{SECOND}.json"
    second = load(dispatch_path)
    second["role"] = "developer"
    _write(dispatch_path, second)

    def rebind(batch: JsonObject) -> None:
        batch["dispatches"][1]["brief_sha256"] = _hash(second)

    edit_batch(repo, rebind)


def first_event(edit: Edit) -> Callable[[Path], None]:
    return lambda repo: edit_event(repo, 0, edit)


def batch_edit(edit: Edit) -> Callable[[Path], None]:
    return lambda repo: edit_batch(repo, edit)


def _drop_first_audit(repo: Path) -> None:
    drop_audit(
        repo,
        load(_generation(repo) / "batches" / f"{BATCH_ID}.json")["recovery_events"][0][
            "record_sha256"
        ],
    )


def _drop_startup_audit(repo: Path) -> None:
    drop_audit(
        repo,
        load(_generation(repo) / "batches" / f"{BATCH_ID}.json")["startup_evidence"][0][
            "record_sha256"
        ],
    )


def _drop_from_both(field: str) -> Callable[[Path], None]:
    def apply(repo: Path) -> None:
        edit_batch(repo, lambda batch: batch.pop(field))
        edit_plan(repo, lambda plan: plan.pop(field))

    return apply


def _rewind(edit: Edit) -> Callable[[Path], None]:
    return lambda repo: edit_event(repo, 2, edit)


CASES: dict[str, Callable[[Path], None]] = {
    # recovery events
    "event-tampered": batch_edit(
        lambda b: b["recovery_events"][0].update(note="edited")
    ),
    "event-sequence": first_event(lambda e: e.update(sequence=7)),
    "event-kind-unknown": first_event(lambda e: e.update(kind="teleport")),
    "pause-approved-by-a-human": first_event(
        lambda e: e.update(approved_by="operator")
    ),
    "resume-approved-by-a-policy": lambda repo: edit_event(
        repo, 1, lambda e: e.update(approved_by="policy:other")
    ),
    "approver-blank": lambda repo: edit_event(
        repo, 1, lambda e: e.update(approved_by=" ")
    ),
    "event-without-audit": _drop_first_audit,
    "event-diverged-from-its-dispatch": first_event(
        lambda e: e.update(snapshot_commit="9" * 40)
    ),
    "event-names-an-unknown-dispatch": first_event(
        lambda e: e.update(dispatch_id="dispatch-ffffffff")
    ),
    "event-supersedes-unknown-evidence": lambda repo: edit_event(
        repo,
        1,
        lambda e: e["evidence"].update(superseded_dispatches=["dispatch-ffffffff"]),
    ),
    "runtime-upgrade-invalid": lambda repo: edit_event(
        repo,
        1,
        lambda e: e["evidence"].update(
            runtime_upgrade={"from": "a", "to": "b", "original": "c"}
        ),
    ),
    "manual-recovery-flag-diverged": batch_edit(
        lambda b: b.update(manual_recovery=False)
    ),
    "environmental-restart-diverged": batch_edit(
        lambda b: b.update(
            environmental_restart={"dispatch_id": FIRST, "snapshot_commit": "9" * 40}
        )
    ),
    "rewind-head-diverged": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(head_commit="9" * 40)
    ),
    "rewind-file-outside-the-scope": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(
            files={"b/outside.py": "6" * 64}
        )
    ),
    "rewind-file-path-not-relative": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(
            files={"a/../b.py": "6" * 64}
        )
    ),
    "rewind-source-not-superseded": _rewind(
        lambda e: e["evidence"].update(superseded_dispatches=[])
    ),
    "rewind-by-a-reported-dispatch": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(source_dispatch_id=FIRST)
    ),
    "rewind-index-hash-malformed": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(index_sha256="xyz")
    ),
    "rewind-progress-has-an-extra-key": _rewind(
        lambda e: e["evidence"]["preserved_progress"].update(extra=1)
    ),
    # startup evidence
    "startup-tampered": batch_edit(
        lambda b: b["startup_evidence"][0].update(commit_sha="9" * 40)
    ),
    "startup-commit-diverged": lambda repo: edit_point(
        repo, lambda p: p.update(commit_sha="9" * 40)
    ),
    "startup-scope-diverged": lambda repo: edit_point(
        repo, lambda p: p.update(scope_sha256="0" * 64)
    ),
    "startup-brief-diverged": lambda repo: edit_point(
        repo, lambda p: p.update(brief_sha256="0" * 64)
    ),
    "startup-worktree-diverged": lambda repo: edit_point(
        repo, lambda p: p.update(worktree="/elsewhere")
    ),
    "startup-without-audit": _drop_startup_audit,
    # the immutable plan
    "goal-differs-from-the-plan": batch_edit(lambda b: b.update(goal="another goal")),
    "supersedes-differs-from-the-plan": batch_edit(
        lambda b: b.update(supersedes="batch-ffffffff")
    ),
    "scope-dropped-from-the-batch": batch_edit(lambda b: b.pop("allowed_paths")),
    "approval-policy-dropped-from-the-batch": batch_edit(
        lambda b: b.pop("approval_policy")
    ),
    "required-field-lacking-in-both": _drop_from_both("branch"),
    # context pressure, carried items, attention
    "pressure-tampered": batch_edit(
        lambda b: b["context_pressure"][0].update(observed_tokens=1)
    ),
    "pressure-level-wrong": batch_edit(
        edit_sealed("context_pressure", lambda r: r.update(level="critical"), index=0)
    ),
    "pressure-tokens-negative": batch_edit(
        edit_sealed(
            "context_pressure",
            lambda r: r.update(observed_tokens=-1, level="ok"),
            index=0,
        )
    ),
    "pressure-threshold-above-limit": batch_edit(
        edit_sealed(
            "context_pressure",
            lambda r: r.update(warning_threshold=200, level="ok"),
            index=0,
        )
    ),
    "pressure-source-unknown": batch_edit(
        edit_sealed("context_pressure", lambda r: r.update(source="claim"), index=0)
    ),
    "pressure-has-an-extra-key": batch_edit(
        edit_sealed("context_pressure", lambda r: r.update(extra=1), index=0)
    ),
    "carried-item-tampered": batch_edit(
        lambda b: b["carried_items"][0].update(summary="edited")
    ),
    "carried-item-lacks-a-key": batch_edit(
        edit_sealed("carried_items", lambda r: r.pop("files"), index=0)
    ),
    "route-unknown": batch_edit(
        lambda b: b["dispatches"][0].update(
            decision={"routing": {"route": "teleport"}, "approved_by": AUTO}
        )
    ),
    "reason-category-unknown": batch_edit(
        lambda b: b["dispatches"][0].update(
            decision={"routing": {"reason_category": "lunch"}, "approved_by": AUTO}
        )
    ),
    "attention-flag-not-boolean": batch_edit(lambda b: b.update(needs_attention="yes")),
    "attention-without-a-reason": batch_edit(lambda b: b.pop("attention_reason")),
    "attention-reason-unlisted": batch_edit(
        lambda b: b.update(attention_reason="boredom")
    ),
    "attention-event-unknown": batch_edit(
        lambda b: b.update(attention_events=[{"event": "x", "at": MOMENT}])
    ),
    "attention-keys-blank": batch_edit(lambda b: b.update(attention_open_keys=[""])),
    # automatic decisions
    "auto-decision-out-of-sequence": batch_edit(
        lambda b: b["auto_decisions"].reverse()
    ),
    "auto-decision-kind-unknown": batch_edit(
        edit_sealed("auto_decisions", lambda r: r.update(kind="lunch"), index=0)
    ),
    "auto-decision-evidence-keys": batch_edit(
        edit_sealed("auto_decisions", lambda r: r.update(evidence={}), index=1)
    ),
    "auto-decision-approver": batch_edit(
        edit_sealed(
            "auto_decisions", lambda r: r.update(approved_by="operator"), index=1
        )
    ),
    "auto-decision-tampered": batch_edit(
        lambda b: b["auto_decisions"][2].update(rationale="edited")
    ),
    "auto-approval-not-the-batch-approval": batch_edit(
        lambda b: b.update(coordinator_approval={"approved_by": "operator"})
    ),
    "auto-dispatch-brief-diverged": batch_edit(
        lambda b: b["auto_decisions"].__setitem__(
            1,
            _reseal(
                {
                    **b["auto_decisions"][1],
                    "evidence": {
                        **b["auto_decisions"][1]["evidence"],
                        "brief_sha256": "0" * 64,
                    },
                }
            ),
        )
    ),
    "auto-continuation-not-recorded": batch_edit(
        lambda b: b.update(coordinator_decisions=[])
    ),
    "auto-stop-reason-unlisted": batch_edit(
        edit_sealed("auto_stop", lambda r: r.update(reason="boredom"))
    ),
    "auto-stop-detector-blank": batch_edit(
        edit_sealed("auto_stop", lambda r: r.update(detected_by=""))
    ),
    "auto-report-for-another-batch": batch_edit(
        edit_sealed("auto_report", lambda r: r.update(batch_id="batch-ffffffff"))
    ),
    "auto-report-stop-diverged": batch_edit(
        edit_sealed(
            "auto_report", lambda r: r["stop"].update(reason="stale", evidence={"x": 1})
        )
    ),
}


def _validate(repo: Path) -> int:
    return coordinator(repo, "ledger", "validate").returncode


def test_a_batch_with_every_sealed_record_kind_passes_both_commands(
    tmp_path: Path,
) -> None:
    rich_state(tmp_path)

    assert coordinator(tmp_path, "ledger", "validate").returncode == 0
    result = migrate(tmp_path)

    assert result.returncode == 0, result.stderr


def test_a_batch_planned_before_the_approval_policy_passes_both_commands(
    tmp_path: Path,
) -> None:
    build_state(tmp_path)
    for drop in (edit_batch, edit_plan):
        drop(tmp_path, lambda record: record.pop("approval_policy"))

    assert _validate(tmp_path) == 0
    assert migrate(tmp_path).returncode == 0


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_state_ledger_validate_rejects_is_refused_by_migrate_and_nothing_changes(
    case: str, tmp_path: Path
) -> None:
    rich_state(tmp_path)
    CASES[case](tmp_path)
    before = tree(tmp_path / STATE_REL)

    validated = coordinator(tmp_path, "ledger", "validate")
    result = migrate(tmp_path)

    assert validated.returncode != 0, f"ledger validate accepts the {case} state"
    assert result.returncode == 2, result.stderr
    # Both name the same defect: Rust words it as Python does, then names the record.
    reason = (
        validated.stderr.splitlines()[0].removeprefix("ERROR: ").split(": batch-")[0]
    )
    assert reason in result.stderr.splitlines()[0]
    assert result.stdout == ""
    assert "REMEDY:" in result.stderr
    assert tree(tmp_path / STATE_REL) == before
    assert not (tmp_path / ".harness/orchestration/state-snapshots").exists()
