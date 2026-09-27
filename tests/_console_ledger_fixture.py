"""A real lifecycle-ledger state for the console's Reports tests: records are written through
`LifecycleLedger` itself, so every write lands in the generation's append-only audit exactly as
the coordinator's would, and a report's date and a batch's state changes come from that audit.

Batch `batch-flow` (ticket #101) walks developer -> code-review -> qa to `completed`, with a
developer risk escalation, a risk assessment and coordinator decisions between the roles. Batch
`batch-stuck` (ticket #202) ends `blocked` on a developer report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.ledger.lifecycle import (
    JsonObject,
    JsonValue,
    LifecycleLedger,
)

LONG_OUTPUT = (
    "Реализован раздел отчётов: список completion reports с фильтрами, чтение по секциям и "
    "хронология батча. " * 6
).strip()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Batch:
    def __init__(self, ledger: LifecycleLedger, batch_id: str, ticket: str) -> None:
        self.ledger = ledger
        self.root = ledger.records_root()
        self.record: JsonObject = {
            "batch_id": batch_id,
            "created_at": _now(),
            "state": "planned",
            "ticket": ticket,
            "coordinator_approval": {
                "approved_by": "coordinator",
                "approved_at": _now(),
            },
            "dispatches": [],
        }
        ledger.write_immutable(
            self.root / "plans" / f"{batch_id}.json", {"batch_id": batch_id}
        )
        ledger.write_immutable(self.root / "batches" / f"{batch_id}.json", self.record)

    @property
    def batch_id(self) -> str:
        value = self.record["batch_id"]
        assert isinstance(value, str)
        return value

    def _list(self, key: str) -> list[JsonValue]:
        value = self.record.setdefault(key, [])
        assert isinstance(value, list)
        return value

    def save(self, state: str | None = None) -> None:
        if state is not None:
            self.record["state"] = state
        self.ledger.replace(
            self.root / "batches" / f"{self.batch_id}.json", self.record
        )

    def dispatch(self, dispatch_id: str, role: str, *, purpose: str = "work") -> None:
        self.ledger.write_immutable(
            self.root / "dispatches" / f"{dispatch_id}.json",
            {
                "dispatch_id": dispatch_id,
                "batch_id": self.batch_id,
                "role": role,
                "purpose": purpose,
                "ticket": self.record["ticket"],
                "state": "approved",
                "coordinator_approval": {
                    "approved_by": "coordinator",
                    "approved_at": _now(),
                },
                "created_at": _now(),
            },
        )
        self._list("dispatches").append(
            {"dispatch_id": dispatch_id, "role": role, "state": "dispatched"}
        )
        self.save("active")

    def report(
        self,
        dispatch_id: str,
        role: str,
        outcome: str,
        *,
        output: str,
        risks: str = "нет",
        blockers: str = "нет",
        review: JsonObject | None = None,
    ) -> None:
        report: JsonObject = {
            "dispatch_id": dispatch_id,
            "ticket": self.record["ticket"],
            "role": role,
            "outcome": outcome,
            "output": output,
            "commit_sha": "a" * 40,
            "changed_files": ["harness/console/reports.py"],
            "checks_run": [
                {
                    "command": "make test",
                    "result": "passed",
                    "evidence": "412 passed",
                }
            ],
            "risks": risks,
            "blockers": blockers,
            "next_coordinator_action": f"решить судьбу отчёта {role}",
            "report_language": "ru",
        }
        if review is not None:
            report["review"] = review
        self.ledger.write_immutable(
            self.root / "reports" / f"{dispatch_id}.json", report
        )
        for entry in self._list("dispatches"):
            if isinstance(entry, dict) and entry.get("dispatch_id") == dispatch_id:
                entry["state"] = "reported"
        self.save("awaiting-approval")

    def decide(
        self, dispatch_id: str, decision: str, next_role: str | None = None
    ) -> None:
        entry: JsonObject = {
            "dispatch_id": dispatch_id,
            "decision": decision,
            "approved_by": "coordinator",
            "approved_at": _now(),
            "note": "none",
        }
        if next_role is not None:
            entry["next_role"] = next_role
        self._list("coordinator_decisions").append(entry)


def build_reports_fixture(repo: Path) -> None:
    ledger = LifecycleLedger(repo / STATE_REL)
    ledger.ensure()

    flow = _Batch(ledger, "batch-flow", "#101")
    flow.save("awaiting-approval")
    flow.dispatch("dispatch-dev", "developer")
    flow.report("dispatch-dev", "developer", "completed", output=LONG_OUTPUT)
    flow._list("risk_escalations").append(
        {
            "dispatch_id": "dispatch-dev",
            "candidate_commit": "a" * 40,
            "triggers": ["schema"],
        }
    )
    flow.decide("dispatch-dev", "accept", next_role="code-review")
    ledger.write_immutable(
        flow.root / "risk-assessments" / "risk-1.json",
        {
            "risk_assessment_id": "risk-1",
            "batch_id": flow.batch_id,
            "matched_triggers": ["schema"],
            "review_required": True,
            "created_at": _now(),
        },
    )
    flow._list("risk_assessments").append(
        {
            "risk_assessment_id": "risk-1",
            "matched_triggers": ["schema"],
            "review_required": True,
        }
    )
    flow.save()
    flow.dispatch("dispatch-review", "code-review", purpose="review")
    flow.report(
        "dispatch-review",
        "code-review",
        "completed",
        output="Ревью без блокирующих находок [high] не найдено",
        review={
            "candidate_commit": "a" * 40,
            "scope": ["harness/console/reports.py"],
            "standards": {
                "severity": "low",
                "findings": [
                    {
                        "severity": "low",
                        "summary": "имя функции",
                        "evidence": "reports.py:10",
                    }
                ],
                "risks": "нет",
                "blockers": "нет",
            },
            "spec": {
                "severity": "none",
                "findings": [],
                "risks": "нет",
                "blockers": "нет",
            },
        },
    )
    flow.decide("dispatch-review", "accept", next_role="qa")
    flow.save()
    flow.dispatch("dispatch-qa", "qa")
    flow.report("dispatch-qa", "qa", "completed", output="QA lane зелёный")
    flow.decide("dispatch-qa", "accept")
    flow.save("completed")

    stuck = _Batch(ledger, "batch-stuck", "#202")
    stuck.save("awaiting-approval")
    stuck.dispatch("dispatch-stuck", "developer")
    stuck.report(
        "dispatch-stuck",
        "developer",
        "blocked",
        output="Не хватает доступа к трекеру",
        blockers="нет токена трекера в окружении",
    )
    stuck.decide("dispatch-stuck", "block")
    stuck.save("blocked")
