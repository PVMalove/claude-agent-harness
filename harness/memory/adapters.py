"""Read-only, bounded projections of permanent archives and selected ledger records.

This module intentionally has no orchestration imports: memory ships independently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from harness.gate_runner.gate_runner import sanitise

from .policy import Policy

STATE = ".harness/orchestration/state"
SELECTOR = STATE + "/ledger.json"
RECORD_KINDS = {"batches", "dispatches", "dispatch-status"}
MAX_RETAINED_BYTES = 16 * 1024


def classify(relative: str) -> str:
    """Reserve sensitive namespaces before the generic Markdown fallback."""
    parts = Path(relative).parts
    if parts[:4] == (".harness", ".sandboxes", "memory", "snapshot"):
        if len(parts) == 6 and parts[4] == "records":
            match = re.fullmatch(
                r"(ticket|pull_request|completion_report)-[1-9][0-9]*-[a-f0-9]{64}\.json",
                parts[5],
            )
            if match:
                return (
                    "completion_report"
                    if match[1] == "completion_report"
                    else "task_archive"
                )
        return ""
    if parts[:2] == ("docs", "tasks"):
        if (
            len(parts) >= 4
            and parts[2].startswith("issue-")
            and relative.endswith(".md")
        ):
            if len(parts) == 4 and parts[3].startswith("issue-"):
                return "task_archive"
            if len(parts) == 5 and parts[3] in {"tickets", "artifacts"}:
                return "task_archive"
        return ""
    if parts[:3] == (".harness", "orchestration", "state"):
        if len(parts) == 7 and parts[3] == "generations" and relative.endswith(".json"):
            if parts[5] == "reports":
                return "qa_finding"
            if parts[5] in RECORD_KINDS:
                return "ledger"
        return ""
    if Path(relative).suffix.lower() != ".md":
        return ""
    return (
        "adr"
        if "adr" in [p.lower() for p in parts[:-1]]
        or Path(relative).stem.lower().startswith("adr")
        else "glossary"
    )


def selected_generation(repo: Path) -> str:
    """Validate only the selector, with no migrations, writes or graph traversal."""
    from .sources import raw_bytes, safe_source

    path = safe_source(repo, SELECTOR)
    if not path.exists():
        return ""
    try:
        value = json.loads(raw_bytes(repo, SELECTOR))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("memory ledger selector is invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "generation", "selected_at"}
        or type(value["version"]) is not int
        or value["version"] != 3
        or not isinstance(value["selected_at"], str)
        or not isinstance(value["generation"], str)
        or re.fullmatch(r"generation-[A-Za-z0-9_-]+", value["generation"]) is None
    ):
        raise ValueError("memory ledger selector is invalid or unsupported")
    relative = STATE + "/generations/" + value["generation"]
    if not safe_source(repo, relative).is_dir():
        raise ValueError("memory selected ledger generation is missing")
    return relative


def scalar(value: object, default: str = "") -> str:
    """Only explicit short scalar text is eligible for projection."""
    return value[:2048] if isinstance(value, str) else default


def project_json(
    raw: bytes, kind: str, *, include_qa: bool = False
) -> tuple[str, str, str, str, str] | None:
    """Project known fields before baseline sanitization; never serialize raw JSON."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("memory source must contain valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ValueError("memory JSON source must be an object")
    if kind == "qa_finding" and value.get("role") != "qa":
        return None
    lessons = value.get("lessons")
    if kind == "completion_report" and (
        not isinstance(lessons, list)
        or not lessons
        or not all(isinstance(item, str) and item.strip() for item in lessons)
    ):
        return None
    fields = (
        ("ticket", "role", "outcome")
        if kind in {"qa_finding", "completion_report"}
        else ("batch_id", "dispatch_id", "ticket", "role", "state")
    )
    retained = [
        f"{key}: {scalar(value[key])}"
        for key in fields
        if isinstance(value.get(key), str)
    ]
    if kind == "completion_report" and isinstance(lessons, list):
        retained.extend(scalar(item) for item in lessons[:20])
    if kind == "qa_finding" or (include_qa and value.get("role") == "qa"):
        for key in ("output", "risks", "blockers"):
            item = value.get(key)
            values = item[:20] if isinstance(item, list) else [item]
            retained.extend(scalar(part) for part in values if isinstance(part, str))
        checks = value.get("checks_run")
        if isinstance(checks, list):
            for check in checks[:20]:
                if isinstance(check, dict):
                    retained.extend(
                        scalar(check.get(key)) for key in ("result", "evidence")
                    )
    status = scalar(
        value.get("status"),
        scalar(
            value.get(
                "outcome" if kind in {"qa_finding", "completion_report"} else "state"
            ),
            "unknown",
        ),
    )
    if kind == "completion_report" and status.lower() not in {
        "superseded",
        "заменён",
        "заменен",
    }:
        status = "не подтверждено человеком"
    date = scalar(
        value.get("date"),
        scalar(value.get("created_at"), scalar(value.get("updated_at"), "unknown")),
    )
    superseded = scalar(value.get("superseded_by"))
    title = (
        kind
        + " "
        + scalar(
            value.get("ticket"),
            scalar(value.get("dispatch_id"), scalar(value.get("batch_id"))),
        )
    )
    # Artifact paths and URLs can embed machine state, full-log references or credentials.
    body = (
        "\n".join(retained)
        .encode("utf-8")[:MAX_RETAINED_BYTES]
        .decode("utf-8", errors="ignore")
    )
    return title, status, date, superseded, body


def baseline(text: str) -> str:
    """Apply existing secret rules plus remove absolute artifact references."""
    text = re.sub(
        r"(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s<>]+|https?://[^\s<>]+", "[artifact]", text
    )
    text = re.sub(r"(?<!\w)(?:glpat-|github_pat_)[A-Za-z0-9_-]+", "[REDACTED]", text)
    return sanitise(text)


def sanitized(value: str, policy: Policy, *, apply_baseline: bool = True) -> str:
    """Share policy redaction while preserving opt-in baseline for local prose."""
    if apply_baseline:
        value = baseline(value)
    for rule in policy.redact_rules:
        value = re.sub(rule, "[REDACTED]", value)
    return value
