#!/usr/bin/env python3
"""Delivery statistics for one finished epic and every ticket under it.

Reads only local evidence: coding-agent session transcripts, this repository's git history, and the
issue tracker CLI.  Nothing is sent anywhere.  Every number the tool cannot source is reported as
missing rather than as zero, and figures that can only be attributed approximately say so.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Protocol, cast

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] delivery_stats requires Python {}+ (found {}).\n".format(
            ".".join(map(str, MIN_PYTHON)), sys.version.split()[0]
        )
    )
    raise SystemExit(1)

# `harness/bin/harness`'s package_files() copies this file verbatim into target projects as
# `.harness/reporting/delivery_stats.py` -- a different directory name than the source tree's
# `harness/`. Alias `harness` to whichever of the two this file actually lives under so
# `from harness...` resolves the same way in both places. See docs/adr/0018.
_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

# Keep these explicit re-exports for callers of the installed delivery_stats.py script.
from harness.errors import HarnessError, print_and_exit  # noqa: I001
from harness.reporting.baseline import (
    _provider_delta as _provider_delta,  # noqa: PLC0414
    baseline_snapshot,
    compare_baseline,
    load_baseline as load_baseline,  # noqa: PLC0414
    save_baseline as save_baseline,  # noqa: PLC0414
)
from harness.reporting.common import (
    BASELINE_SCHEMA_VERSION as BASELINE_SCHEMA_VERSION,  # noqa: PLC0414
    CLAUDE_FIELDS,
    CODEX_FIELDS,
    MISSING as MISSING,  # noqa: PLC0414
    JsonObject,
    StatsError as StatsError,  # noqa: PLC0414
    _int,
)
from harness.reporting.cost import estimate_cost as estimate_cost  # noqa: PLC0414
from harness.reporting.cost import load_rates as load_rates  # noqa: PLC0414
from harness.reporting.terminal import render_terminal as render_terminal  # noqa: PLC0414


class _LedgerInstance(Protocol):
    def records_root_lenient(self) -> Path | None: ...


class LedgerClass(Protocol):
    """The slice of LifecycleLedger this module uses. The class itself is loaded by file path from
    the analysed repo (see _load_ledger_class), so it cannot be imported and named statically."""

    def __call__(self, root: Path) -> _LedgerInstance: ...

    def read_record_lenient(self, path: Path) -> JsonObject | None: ...


# Pseudo-models the runtime writes for locally generated messages; never billed.
NON_BILLABLE_MODELS = {"<synthetic>"}
# Matches coordinator.py's own default STATE_REL: the backend-orchestration ledger this project's
# coordinator writes, read here only for structural metrics -- never re-validated as strictly as the
# coordinator itself does, since a missing or unreadable record must degrade to "missing", not abort.
ORCHESTRATION_STATE_REL = Path(".harness/orchestration/state")
CONTINUATION_DECISIONS = {"continue", "continue-automatic"}


def _run(command: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git(repo: Path, *arguments: str) -> str:
    code, out, err = _run(["git", "-C", str(repo), *arguments])
    if code != 0:
        raise StatsError(
            f"git {' '.join(arguments)} failed: {err or out or 'unknown error'}",
            remedy=f"inspect the git error above and fix the repository state before retrying 'git {' '.join(arguments)}'",
        )
    return out


def _read_jsonl(path: Path) -> Iterator[JsonObject]:
    """Yield the JSON objects of a transcript, skipping records the writer left truncated."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _same_path(
    recorded: object, repo: Path, _cache: dict[tuple[str, str], bool] | None = None
) -> bool:
    """Whether a recorded working directory is this repository.

    A string comparison is not enough: Windows records an 8.3 short path ("RUNNER~1") for the same
    directory the caller resolved, and case differs freely. Resolution is cached because the same
    handful of directories repeat across every record of a session.
    """
    if _cache is None:
        _cache = {}
    if not isinstance(recorded, str) or not recorded:
        return False
    key = (recorded, str(repo))
    hit = _cache.get(key)
    if hit is None:
        if recorded.replace("\\", "/").lower() == str(repo).replace("\\", "/").lower():
            hit = True
        else:
            try:
                hit = Path(recorded).resolve() == repo
            except OSError:
                hit = False
        _cache[key] = hit
    return hit


# --------------------------------------------------------------------------------------- scope


def _project_config(repo: Path) -> JsonObject:
    path = repo / ".harness/project.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StatsError(
            ".harness/project.json is not valid JSON",
            remedy="fix the JSON syntax in .harness/project.json",
        ) from exc
    return value if isinstance(value, dict) else {}


def _gh(repo: Path, *arguments: str) -> JsonObject | list[JsonObject] | None:
    code, out, err = _run(["gh", *arguments], cwd=repo)
    if code == 127:
        raise StatsError(
            "the gh CLI is required to resolve an epic and is not available",
            remedy="install the GitHub CLI (gh) and ensure it is on PATH, or pass --tickets to run offline",
        )
    if code != 0:
        raise StatsError(
            f"gh {' '.join(arguments)} failed: {err or out or 'unknown error'}",
            remedy=f"inspect the gh error above and fix authentication/permissions before retrying 'gh {' '.join(arguments)}'",
        )
    try:
        return cast(JsonObject | list[JsonObject], json.loads(out)) if out else None
    except ValueError as exc:
        raise StatsError(
            "gh returned output that is not valid JSON",
            remedy=f"retry 'gh {' '.join(arguments)}'; if it keeps failing, check the gh CLI version",
        ) from exc


ISSUE_BRANCH = re.compile(r"^[a-z]+/issue-(\d+)-")


def ticket_of_branch(branch: object) -> int | None:
    if not isinstance(branch, str):
        return None
    match = ISSUE_BRANCH.match(branch)
    return int(match.group(1)) if match else None


def resolve_scope(repo: Path, epic: int) -> JsonObject:
    """Epic plus every ticket linked under it.

    Scope is a set of ticket numbers, not a set of live branches: an epic is usually measured after
    its work merged, and merged issue branches are normally deleted. Everything downstream matches on
    the ticket number encoded in a branch name, which survives that deletion in pull requests and in
    session transcripts alike.
    """
    parent = cast(
        JsonObject,
        _gh(
            repo,
            "issue",
            "view",
            str(epic),
            "--json",
            "number,title,state,createdAt,closedAt,url",
        ),
    )
    children = cast(
        JsonObject, _gh(repo, "issue", "view", str(epic), "--json", "subIssues") or {}
    )
    nodes = ((children.get("subIssues") or {}).get("nodes")) or []
    tickets = [
        {
            "number": parent["number"],
            "title": parent["title"],
            "state": parent["state"],
            "role": "epic",
        }
    ]
    for node in nodes:
        tickets.append(
            {
                "number": node["number"],
                "title": node.get("title", ""),
                "state": node.get("state", ""),
                "role": "child",
            }
        )
    return {
        "epic": {
            "number": parent["number"],
            "title": parent["title"],
            "state": parent["state"],
            "url": parent.get("url", ""),
            "created_at": parent.get("createdAt"),
            "closed_at": parent.get("closedAt"),
        },
        "tickets": tickets,
        "numbers": {ticket["number"] for ticket in tickets},
    }


def offline_scope(epic: int, tickets: str) -> JsonObject:
    """Scope stated by the caller instead of read from the tracker.

    This is the offline mode: no tracker CLI is contacted, so ticket titles and states are unknown
    and reported as such rather than guessed. Code volume then comes from local refs only.
    """
    numbers = []
    for chunk in tickets.replace(",", " ").split():
        if not chunk.isdigit():
            raise StatsError(
                f"--tickets expects issue numbers, got {chunk!r}",
                remedy="pass --tickets as a comma/space-separated list of issue numbers only",
            )
        numbers.append(int(chunk))
    if epic not in numbers:
        numbers.insert(0, epic)
    return {
        "epic": {
            "number": epic,
            "title": MISSING,
            "state": MISSING,
            "url": "",
            "created_at": None,
            "closed_at": None,
        },
        "tickets": [
            {
                "number": number,
                "title": MISSING,
                "state": MISSING,
                "role": "epic" if number == epic else "child",
            }
            for number in numbers
        ],
        "numbers": set(numbers),
        "offline": True,
    }


def pull_requests(repo: Path, numbers: set[int]) -> list[JsonObject]:
    """Pull requests whose head branch belongs to a ticket in scope.

    GitHub keeps a merged pull request's diffstat after its branch is deleted, so this is the
    durable source for code volume; local refs are only a fallback for work with no pull request.
    """
    listed = cast(
        list[JsonObject],
        _gh(
            repo,
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "number,headRefName,state,additions,deletions,changedFiles,mergedAt,url",
        )
        or [],
    )
    matched = []
    for entry in listed:
        ticket = ticket_of_branch(entry.get("headRefName"))
        if ticket in numbers:
            matched.append({**entry, "ticket": ticket})
    return matched


# ------------------------------------------------------------------------------------ git volume


def git_volume(
    repo: Path, prs: list[JsonObject], extra_branches: set[str], base: str
) -> JsonObject:
    """Code volume per ticket, from pull requests first and local refs only where none exists."""
    entries = []
    totals = {
        "commits": 0,
        "insertions": 0,
        "deletions": 0,
        "files": 0,
        "pull_requests": 0,
    }
    covered: set[str] = set()

    adr: set[str] = set()
    for pr in prs:
        detail = cast(
            JsonObject,
            _gh(repo, "pr", "view", str(pr["number"]), "--json", "commits,files") or {},
        )
        commit_list = detail.get("commits") or []
        count = len(commit_list)
        oids = {item.get("oid") for item in commit_list if item.get("oid")}
        for item in detail.get("files") or []:
            path = item.get("path", "")
            if _is_adr(path) and _added_by(repo, path) in oids:
                adr.add(path)
        entries.append(
            {
                "source": "pull-request",
                "ticket": pr["ticket"],
                "branch": pr["headRefName"],
                "pull_request": pr["number"],
                "state": pr.get("state"),
                "status": "ok",
                "commits": count,
                "insertions": _int(pr.get("additions")),
                "deletions": _int(pr.get("deletions")),
                "files": _int(pr.get("changedFiles")),
            }
        )
        covered.add(pr["headRefName"])
        totals["pull_requests"] += 1
        totals["commits"] += count
        totals["insertions"] += _int(pr.get("additions"))
        totals["deletions"] += _int(pr.get("deletions"))
        totals["files"] += _int(pr.get("changedFiles"))

    for branch in sorted(extra_branches - covered):
        ref = branch if _ref_exists(repo, branch) else f"origin/{branch}"
        if not _ref_exists(repo, ref):
            entries.append(
                {
                    "source": "branch",
                    "branch": branch,
                    "ticket": ticket_of_branch(branch),
                    "status": MISSING,
                    "reason": "ветка удалена, pull request не найден",
                }
            )
            continue
        try:
            merge_base = _git(repo, "merge-base", base, ref)
            numstat = _git(repo, "diff", "--numstat", merge_base, ref)
            commits = [
                line
                for line in _git(repo, "rev-list", f"{merge_base}..{ref}").splitlines()
                if line
            ]
        except StatsError:
            entries.append(
                {
                    "source": "branch",
                    "branch": branch,
                    "ticket": ticket_of_branch(branch),
                    "status": MISSING,
                    "reason": "не сравнить с базовой веткой",
                }
            )
            continue
        insertions = deletions = 0
        files: set[str] = set()
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
            files.add(path)
        entries.append(
            {
                "source": "branch",
                "ticket": ticket_of_branch(branch),
                "branch": branch,
                "status": "ok",
                "commits": len(commits),
                "insertions": insertions,
                "deletions": deletions,
                "files": len(files),
            }
        )
        totals["commits"] += len(commits)
        totals["insertions"] += insertions
        totals["deletions"] += deletions
        totals["files"] += len(files)
        for path in _added_adr(repo, base, ref):
            adr.add(path)
    return {
        "base": base,
        "entries": entries,
        "totals": totals,
        "adr_added": sorted(adr),
    }


def _is_adr(path: str) -> bool:
    return (
        path.startswith("docs/adr/")
        and path.endswith(".md")
        and not path.endswith("template.md")
    )


def _added_by(repo: Path, path: str) -> str | None:
    """The commit that first added a path.

    A pull request that only edits an existing decision record must not be counted as adding one, so
    membership in the pull request's own commits is what decides it. This undercounts a squash-merged
    pull request, whose original commit IDs no longer exist — an undercount being the honest failure
    here, never an invented record.
    """
    # --all: the adding commit may live on a branch that is not the current HEAD, which is the
    # normal case when the epic is measured from a different worktree or before its merge.
    code, out, _ = _run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            path,
        ]
    )
    if code != 0 or not out:
        return None
    return out.splitlines()[-1].strip()


def _added_adr(repo: Path, base: str, ref: str) -> list[str]:
    try:
        merge_base = _git(repo, "merge-base", base, ref)
        names = _git(repo, "diff", "--name-only", "--diff-filter=A", merge_base, ref)
    except StatsError:
        return []
    return [path for path in names.splitlines() if _is_adr(path)]


def _local_issue_branches(repo: Path) -> set[str]:
    names: set[str] = set()
    for scope in ("refs/heads", "refs/remotes/origin"):
        code, out, _ = _run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", scope]
        )
        if code != 0:
            continue
        for name in out.splitlines():
            names.add(
                name.split("origin/", 1)[-1] if scope.endswith("origin") else name
            )
    return {name for name in names if ticket_of_branch(name) is not None}


def _ref_exists(repo: Path, ref: str) -> bool:
    code, _, _ = _run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ]
    )
    return code == 0


# --------------------------------------------------------------------------------- claude usage


CWD_PROBE_TRANSCRIPTS = 5
CWD_PROBE_RECORDS = 200


def _slug_variants(repo: Path) -> list[str]:
    """Directory names the runtime may have used for one project path.

    The naming scheme is not a published contract and has changed: it used to replace only path
    separators, and now also folds characters such as "_" into "-". A project renamed by that change
    keeps its old directory, so both spellings have to be considered.
    """
    text = str(repo)
    variants = [re.sub(r"[\\/:]", "-", text), re.sub(r"[^A-Za-z0-9-]", "-", text)]
    seen = []
    for variant in variants:
        if variant not in seen:
            seen.append(variant)
    return seen


def _has_transcripts(directory: Path) -> bool:
    """Whether a directory holds session data, rather than only leftovers such as memory/."""
    try:
        return any(
            path.is_file() and path.stat().st_size > 0
            for path in directory.glob("*.jsonl")
        )
    except OSError:
        return False


def _records_repo(directory: Path, repo: Path) -> bool:
    """Whether this directory's transcripts were recorded in this repository.

    Probes several transcripts, newest first, and several records of each: the working directory is
    not on every record, so inspecting only the first one misses it almost always.
    """
    try:
        transcripts = sorted(
            directory.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return False
    for transcript in transcripts[:CWD_PROBE_TRANSCRIPTS]:
        for index, record in enumerate(_read_jsonl(transcript)):
            if index >= CWD_PROBE_RECORDS:
                break
            if _same_path(record.get("cwd"), repo):
                return True
    return False


def claude_project_dirs(home: Path, repo: Path) -> list[Path]:
    """Every transcript directory belonging to this repository.

    A directory qualifies only when it actually contains transcripts: an empty directory left behind
    by a renaming, whose name still matches the expected slug, must not shadow the real one. More
    than one directory can qualify at once, and all of them count — otherwise an epic that spans a
    rename silently loses the half recorded under the older name.
    """
    root = home / ".claude/projects"
    if not root.is_dir():
        return []
    try:
        candidates = [path for path in sorted(root.iterdir()) if path.is_dir()]
    except OSError:
        return []
    named = {root / slug for slug in _slug_variants(repo)}
    found = []
    for candidate in candidates:
        if not _has_transcripts(candidate):
            continue
        if candidate in named or _records_repo(candidate, repo):
            found.append(candidate)
    return found


def _claude_transcripts(directory: Path) -> list[tuple[Path, bool]]:
    """Every transcript belonging to one project directory: top-level session logs, then -- nested
    one level deeper under each session's own directory -- its subagent transcripts at
    <session-uuid>/subagents/*.jsonl. Each entry is (path, is_subagent): a subagent transcript's own
    "sessionId" field replays its *parent's* session id (confirmed against real Claude Code output
    on disk), so it must never be used as that transcript's own grouping key -- the transcript's own
    filename (path.stem) is, and is_subagent tells the caller which key to use.
    """
    main = [(path, False) for path in sorted(directory.glob("*.jsonl"))]
    sub = [(path, True) for path in sorted(directory.glob("*/subagents/*.jsonl"))]
    return main + sub


def _is_billable_turn(record: JsonObject) -> bool:
    """Whether a record is an API turn with a priceable model, not a locally generated notice."""
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and isinstance(message, dict)
        and message.get("model") not in NON_BILLABLE_MODELS
        and not record.get("isApiErrorMessage")
    )


def _usage_complete(usage: object) -> bool:
    """Whether a turn's usage dict has every Claude field, each a real (non-bool) int."""
    return isinstance(usage, dict) and not any(
        not isinstance(usage.get(field), int) or isinstance(usage.get(field), bool)
        for field in CLAUDE_FIELDS
    )


def _turn_input(usage: JsonObject) -> int:
    return sum(_int(usage.get(f)) for f in CLAUDE_FIELDS[:3])


def claude_usage(project_dirs: list[Path], numbers: set[int]) -> JsonObject:
    """Token usage of every Claude Code turn recorded on a branch belonging to a ticket in scope."""
    if not project_dirs:
        return {
            "status": MISSING,
            "reason": "нет транскриптов Claude Code для этого репозитория",
        }
    branches_seen: set[str] = set()
    models: JsonObject = {}
    sessions: set[str] = set()
    sidechain = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    thinking = 0
    first: datetime | None = None
    last: datetime | None = None
    quota: JsonObject | None = None
    turns = 0
    synthetic = 0
    incomplete_telemetry = False

    session_stats: dict[str, JsonObject] = {}

    transcripts = [
        entry for directory in project_dirs for entry in _claude_transcripts(directory)
    ]
    for transcript, is_subagent in transcripts:
        for record in _read_jsonl(transcript):
            branch = record.get("gitBranch")
            if not isinstance(branch, str) or ticket_of_branch(branch) not in numbers:
                continue
            branches_seen.add(branch)
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not _is_billable_turn(record):
                # Locally generated notices ("you've hit your session limit"), not API turns: their
                # usage is all zeros and no rate card can ever price them. Counting them would add a
                # pseudo-model to the breakdown and to the unpriced list for no reason.
                synthetic += 1
                continue
            if not isinstance(usage, dict) or not _usage_complete(usage):
                incomplete_telemetry = True
                continue
            turns += 1
            # A subagent's transcript replays the parent's sessionId inside its JSON records, so the
            # record alone cannot identify the subagent. Using the filename (transcript.stem) is the
            # established convention here, as the tool generating the logs (e.g. Claude Code) does not
            # currently emit a distinct subagentId field.
            session_id = (
                transcript.stem
                if is_subagent
                else (record.get("sessionId") or transcript.stem)
            )
            sessions.add(session_id)

            turn_input = _turn_input(usage)
            s_bucket = session_stats.setdefault(
                session_id,
                {
                    "branch": branch,
                    "kind": "subagent" if is_subagent else "main",
                    "turns": 0,
                    "total_input": 0,
                    "max_input": 0,
                },
            )
            s_bucket["turns"] += 1
            s_bucket["total_input"] += turn_input
            s_bucket["max_input"] = max(s_bucket["max_input"], turn_input)

            moment = _moment(record.get("timestamp"))
            if moment:
                first = moment if first is None or moment < first else first
                last = moment if last is None or moment > last else last
            bucket = models.setdefault(
                message.get("model") or "unknown",
                {field: 0 for field in CLAUDE_FIELDS} | {"turns": 0},
            )
            bucket["turns"] += 1
            for field in CLAUDE_FIELDS:
                bucket[field] += _int(usage.get(field))
            details = usage.get("output_tokens_details")
            if isinstance(details, dict):
                thinking += _int(details.get("thinking_tokens"))
            if record.get("isSidechain"):
                sidechain["turns"] += 1
                sidechain["input_tokens"] += sum(
                    _int(usage.get(f)) for f in CLAUDE_FIELDS[:3]
                )
                sidechain["output_tokens"] += _int(usage.get("output_tokens"))
            limits = record.get("quotaLimits")
            if isinstance(limits, dict) and limits:
                quota = limits

    if incomplete_telemetry:
        return {
            "status": MISSING,
            "reason": "неполная telemetry Claude Code на ветках этих тикетов",
        }
    if not turns:
        return {
            "status": MISSING,
            "reason": "нет ходов Claude Code на ветках этих тикетов",
        }
    return {
        "status": "ok",
        "attribution": "exact",
        "sources": [str(directory) for directory in project_dirs],
        "branches": sorted(branches_seen),
        "models": models,
        "turns": turns,
        "sessions": len(sessions),
        "thinking_tokens": thinking,
        "non_billable_turns": synthetic,
        "sidechain": sidechain,
        "session_stats": sorted(
            [{"id": k} | v for k, v in session_stats.items()],
            key=lambda x: x["total_input"],
            reverse=True,
        ),
        "first_activity": first.isoformat() if first else None,
        "last_activity": last.isoformat() if last else None,
        "quota": quota if quota else MISSING,
    }


def live_probe(project_dirs: list[Path], branch: str) -> JsonObject:
    """Live, branch-scoped snapshot of every session/subagent transcript recorded so far -- turn
    count, the largest single-turn input this identity has sent, and the input size of its most
    recent turn. Deliberately NOT epic-scoped and not a replacement for claude_usage(): this answers
    "what is active on this branch right now", not "what did a finished epic cost".
    """
    if not project_dirs:
        return {
            "status": MISSING,
            "reason": "нет транскриптов Claude Code для этого репозитория",
        }
    sessions: dict[str, JsonObject] = {}
    for directory in project_dirs:
        for transcript, is_subagent in _claude_transcripts(directory):
            for record in _read_jsonl(transcript):
                if record.get("gitBranch") != branch:
                    continue
                if not _is_billable_turn(record):
                    continue
                usage = record["message"].get("usage")
                if not isinstance(usage, dict) or not _usage_complete(usage):
                    continue
                session_id = (
                    transcript.stem
                    if is_subagent
                    else (record.get("sessionId") or transcript.stem)
                )
                turn_input = _turn_input(usage)
                bucket = sessions.setdefault(
                    session_id,
                    {
                        "kind": "subagent" if is_subagent else "main",
                        "turns": 0,
                        "max_input": 0,
                        "last_input": 0,
                    },
                )
                bucket["turns"] += 1
                bucket["last_input"] = turn_input
                bucket["max_input"] = max(bucket["max_input"], turn_input)
    if not sessions:
        return {"status": MISSING, "reason": f"нет ходов Claude Code на ветке {branch}"}
    return {
        "status": "ok",
        "branch": branch,
        "sessions": [{"id": k} | v for k, v in sessions.items()],
    }


# ---------------------------------------------------------------------------------- codex usage


def codex_usage(
    sessions_root: Path | None,
    repo: Path,
    window: tuple[datetime | None, datetime | None],
) -> JsonObject:
    """Codex records no branch, only a working directory and a timestamp.

    Work is therefore attributed to the epic by repository plus the activity window of its issue
    branches.  That is an estimate and the report must present it as one.
    """
    if sessions_root is None or not sessions_root.is_dir():
        return {"status": MISSING, "reason": "на этой машине нет сессий Codex"}
    start, end = window
    if start is None or end is None:
        return {
            "status": MISSING,
            "reason": "нет окна активности, к которому можно отнести работу Codex",
        }
    models: JsonObject = {}
    sessions: set[str] = set()
    rate_limits: JsonObject | None = None
    turns = 0
    incomplete_telemetry = False

    for transcript in sorted(sessions_root.rglob("rollout-*.jsonl")):
        current_model = "unknown"
        in_repo = False
        counted: set[int | str] = set()
        pending: list[tuple[str, JsonObject]] = []
        transcript_incomplete = False
        for record in _read_jsonl(transcript):
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            cwd = payload.get("cwd") or record.get("cwd")
            if _same_path(cwd, repo):
                in_repo = True
            if payload.get("model"):
                current_model = payload["model"]
            if payload.get("type") != "token_count":
                continue
            moment = _moment(record.get("timestamp"))
            if moment is None or not (start <= moment <= end):
                continue
            ordinal = record.get("ordinal")
            if ordinal is not None:
                if ordinal in counted:
                    continue
                counted.add(ordinal)
            info = payload.get("info")
            info = info if isinstance(info, dict) else {}
            delta = info.get("last_token_usage")
            if not isinstance(delta, dict) or any(
                not isinstance(delta.get(field), int)
                or isinstance(delta.get(field), bool)
                for field in CODEX_FIELDS
            ):
                transcript_incomplete = True
                continue
            pending.append((current_model, delta))
            limits = record.get("rate_limits") or payload.get("rate_limits")
            if isinstance(limits, dict) and limits:
                rate_limits = limits
        if not in_repo:
            continue
        if transcript_incomplete:
            incomplete_telemetry = True
            continue
        if not pending:
            continue
        sessions.add(transcript.stem)
        for model, delta in pending:
            turns += 1
            bucket = models.setdefault(
                model,
                {field: 0 for field in CODEX_FIELDS}
                | {"reasoning_output_tokens": 0, "turns": 0},
            )
            bucket["turns"] += 1
            for field in CODEX_FIELDS:
                bucket[field] += _int(delta.get(field))
            bucket["reasoning_output_tokens"] += _int(
                delta.get("reasoning_output_tokens")
            )

    if incomplete_telemetry:
        return {
            "status": MISSING,
            "reason": "неполная telemetry Codex по этому репозиторию внутри окна",
        }
    if not turns:
        return {
            "status": MISSING,
            "reason": "нет ходов Codex по этому репозиторию внутри окна",
        }
    return {
        "status": "ok",
        "attribution": "estimated",
        "attribution_note": (
            "В логах Codex нет ветки: сюда попадает работа по этому репозиторию внутри окна "
            "активности эпика, включая посторонние задачи того же периода."
        ),
        "models": models,
        "turns": turns,
        "sessions": len(sessions),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "rate_limits": rate_limits if rate_limits else MISSING,
    }


# --------------------------------------------------------------------------- orchestration ledger


def _load_ledger_class(repo: Path) -> LedgerClass | None:
    """Import LifecycleLedger from the analyzed repo's own .harness/orchestration/ledger/lifecycle.py
    (or the pre-package .harness/orchestration/ledger.py), when present there.

    backend-orchestration is an optional capability, independent of this reporting module (which
    ships in the always-installed base suite): the repository this delivery_stats.py copy is
    itself deployed in may never have installed it, while --repo (the project being analyzed) can
    be a different, unrelated project that has -- so ledger.py cannot be imported as a sibling of
    this file (--repo is a separate checkout, outside this harness installation's own package tree).
    Loaded by file path under a private name and never registered in sys.modules, so this never
    collides with, or is shadowed by, an already-imported "ledger" module belonging to a different
    repository's copy within the same process. Returns None when that module is not present or
    fails to import -- a caller then falls back to ``_fallback_records_root``/``_fallback_read_record``
    below, which implement the identical tolerant algorithm inline for a repo whose ledger state
    was written directly (e.g. by a test fixture, or a harness install that never carried the
    optional backend-orchestration Python source alongside its data).

    The loaded module is registered in ``sys.modules`` under its private name for the duration of
    ``exec_module``: ledger.py declares several ``@dataclass`` records, and the dataclass machinery
    looks its own module up via ``sys.modules[cls.__module__]`` while processing the class body, so
    an unregistered module fails with an unrelated-looking AttributeError."""
    orchestration = repo / ".harness" / "orchestration"
    # ledger.py became the ledger/ package's lifecycle.py; a project installed from an older
    # harness still carries the flat module, so accept either layout.
    ledger_path = next(
        (
            path
            for path in (
                orchestration / "ledger" / "lifecycle.py",
                orchestration / "ledger.py",
            )
            if path.is_file()
        ),
        None,
    )
    if ledger_path is None:
        return None
    module_name = f"_delivery_stats_ledger_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, ledger_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001
        return None
    finally:
        sys.modules.pop(module_name, None)
    return cast(LedgerClass | None, getattr(module, "LifecycleLedger", None))


def _fallback_records_root(root: Path) -> Path | None:
    """LifecycleLedger.records_root_lenient()'s own tolerant pointer-resolution algorithm,
    inlined for the rare case ``_load_ledger_class`` finds no ledger.py module at all. Kept in
    lockstep with the real method (never checks the pointer schema or version, never runs
    per-record validation) by tests/test_ledger.py's coverage of that method and
    tests/test_delivery_stats.py's coverage of this fallback against the same fixtures."""
    pointer_path = root / "ledger.json"
    if not pointer_path.is_file():
        return root if root.is_dir() else None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generation = pointer.get("generation") if isinstance(pointer, dict) else None
    if not isinstance(generation, str):
        return None
    candidate = root / "generations" / generation
    return candidate if candidate.is_dir() else None


def _fallback_read_record(path: Path) -> JsonObject | None:
    """LifecycleLedger.read_record_lenient()'s own algorithm, inlined for the same fallback."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lenient_records_root(root: Path, ledger_cls: LedgerClass | None) -> Path | None:
    if ledger_cls is None:
        return _fallback_records_root(root)
    try:
        return ledger_cls(root).records_root_lenient()
    except Exception:  # noqa: BLE001
        # ledger_cls came from a dynamically loaded module (_load_ledger_class): an incompatible
        # or old LifecycleLedger (e.g. missing records_root_lenient()) must degrade to the same
        # fallback as no module at all, never propagate out of orchestration_metrics().
        return _fallback_records_root(root)


def _lenient_read_record(
    path: Path, ledger_cls: LedgerClass | None
) -> JsonObject | None:
    if ledger_cls is None:
        return _fallback_read_record(path)
    try:
        return ledger_cls.read_record_lenient(path)
    except Exception:  # noqa: BLE001
        # Same rationale as _lenient_records_root above.
        return _fallback_read_record(path)


def _dispatch_record(
    root: Path, dispatch_id: object, ledger_cls: LedgerClass | None
) -> JsonObject | None:
    if not isinstance(dispatch_id, str):
        return None
    return _lenient_read_record(root / "dispatches" / f"{dispatch_id}.json", ledger_cls)


def _developer_write_paths(
    root: Path, batch: JsonObject, ledger_cls: LedgerClass | None
) -> list[str] | None:
    """The declared zone a code-review diff is checked against: the most recent developer
    dispatch's own recorded write_paths (retries share the same zone, so the latest is enough)."""
    for entry in reversed(batch.get("dispatches", [])):
        if not isinstance(entry, dict) or entry.get("role") != "developer":
            continue
        dispatch = _dispatch_record(root, entry.get("dispatch_id"), ledger_cls)
        paths = dispatch.get("write_paths") if dispatch else None
        if isinstance(paths, list) and paths:
            return paths
    return None


def _empty_ticket_orchestration() -> JsonObject:
    return {
        "batches": [],
        "worker_sessions": [],
        "qa_decided": 0,
        "qa_failed": 0,
        "review_scope": [],
    }


def _accumulate_batch_orchestration(
    root: Path, batch: JsonObject, bucket: JsonObject, ledger_cls: LedgerClass | None
) -> None:
    bucket["batches"].append(batch.get("batch_id"))
    restarts_by_dispatch: dict[str, list[JsonObject]] = {}
    for decision in batch.get("coordinator_decisions", []):
        if (
            not isinstance(decision, dict)
            or decision.get("decision") not in CONTINUATION_DECISIONS
        ):
            continue
        dispatch_id = decision.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            continue
        restarts_by_dispatch.setdefault(dispatch_id, []).append(
            {
                "decision": decision.get("decision"),
                "reason": decision.get("note") or MISSING,
                "approved_at": decision.get("approved_at", MISSING),
            }
        )
    developer_paths = _developer_write_paths(root, batch, ledger_cls)
    for entry in batch.get("dispatches", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("dispatch_id"), str):
            continue
        dispatch_id = entry["dispatch_id"]
        restarts = restarts_by_dispatch.get(dispatch_id, [])
        bucket["worker_sessions"].append(
            {
                "dispatch_id": dispatch_id,
                "role": entry.get("role", MISSING),
                "sessions": 1 + len(restarts),
                "restarts": restarts,
            }
        )
        role = entry.get("role")
        decision = entry.get("decision")
        if role == "qa" and isinstance(decision, dict):
            bucket["qa_decided"] += 1
            if decision.get("decision") != "accept":
                bucket["qa_failed"] += 1
        if role == "code-review" and developer_paths is not None:
            dispatch = _dispatch_record(root, dispatch_id, ledger_cls)
            scope = dispatch.get("review_scope") if dispatch else None
            if isinstance(scope, list) and scope:
                out_of_scope = [
                    path
                    for path in scope
                    if not any(
                        fnmatchcase(str(path).replace("\\", "/"), pattern)
                        for pattern in developer_paths
                    )
                ]
                bucket["review_scope"].append(
                    {
                        "dispatch_id": dispatch_id,
                        "files_total": len(scope),
                        "files_out_of_scope": len(out_of_scope),
                        "out_of_scope_files": out_of_scope,
                        "share": round(len(out_of_scope) / len(scope), 4),
                    }
                )


def _finalize_ticket_orchestration(bucket: JsonObject) -> JsonObject:
    bucket["qa_failure_rate"] = (
        round(bucket["qa_failed"] / bucket["qa_decided"], 4)
        if bucket["qa_decided"]
        else MISSING
    )
    if not bucket["review_scope"]:
        bucket["review_scope"] = MISSING
    return bucket


def orchestration_metrics(
    repo: Path, numbers: set[int], state_dir: Path | None = None
) -> JsonObject:
    """Structural metrics from the backend-orchestration ledger, per ticket in scope: worker
    sessions a dispatch spanned with each session's coordinator-recorded compaction/restart reason,
    the share of a code-review diff outside the developer's declared write-path zone, and QA failure
    rate. Every figure here comes from the coordinator's own hash-linked records, never a role's
    free-text self-report; an absent or unreadable ledger reports the whole metric as missing."""
    ledger_cls = _load_ledger_class(repo)
    root_dir = state_dir if state_dir is not None else repo / ORCHESTRATION_STATE_REL
    root = _lenient_records_root(root_dir, ledger_cls)
    if root is None:
        return {
            "status": MISSING,
            "reason": "оркестрационный ledger недоступен для этого репозитория",
        }
    batches_dir = root / "batches"
    if not batches_dir.is_dir():
        return {"status": MISSING, "reason": "в ledger нет записей batches"}
    tickets: dict[str, JsonObject] = {}
    for path in sorted(batches_dir.glob("batch-*.json")):
        batch = _lenient_read_record(path, ledger_cls)
        if batch is None:
            continue
        ticket_number = ticket_of_branch(batch.get("branch"))
        if ticket_number not in numbers:
            continue
        bucket = tickets.setdefault(str(ticket_number), _empty_ticket_orchestration())
        _accumulate_batch_orchestration(root, batch, bucket, ledger_cls)
    if not tickets:
        return {
            "status": MISSING,
            "reason": "в ledger нет batch-записей для тикетов этой области",
        }
    return {
        "status": "ok",
        "tickets": {
            number: _finalize_ticket_orchestration(bucket)
            for number, bucket in tickets.items()
        },
    }




# -------------------------------------------------------------------------------------- summary


def cache_split(claude: JsonObject) -> str | JsonObject:
    if claude.get("status") != "ok":
        return MISSING
    fresh = sum(bucket["input_tokens"] for bucket in claude["models"].values())
    write = sum(
        bucket["cache_creation_input_tokens"] for bucket in claude["models"].values()
    )
    read = sum(
        bucket["cache_read_input_tokens"] for bucket in claude["models"].values()
    )
    total = fresh + write + read
    if total == 0:
        return MISSING
    return {
        "total_input": total,
        "fresh": fresh,
        "cache_write": write,
        "cache_read": read,
        "fresh_percent": round(fresh * 100 / total, 3),
        "cache_write_percent": round(write * 100 / total, 3),
        "cache_read_percent": round(read * 100 / total, 3),
    }



def build_report(args: argparse.Namespace) -> JsonObject:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise StatsError(
            f"not a git repository: {repo}",
            remedy="pass --repo pointing at a real Git checkout",
        )
    config = _project_config(repo)
    base = args.base or config.get("base_branch") or "main"

    if args.tickets:
        scope = offline_scope(args.epic, args.tickets)
    else:
        scope = resolve_scope(repo, args.epic)
    numbers = scope["numbers"]

    home = Path(args.home).expanduser() if args.home else Path.home()
    if args.claude_projects:
        project_dirs = [Path(item) for item in args.claude_projects]
    else:
        project_dirs = claude_project_dirs(home, repo)
    claude = claude_usage(project_dirs, numbers)
    window = (
        _moment(claude.get("first_activity")),
        _moment(claude.get("last_activity")),
    )
    codex_root = (
        Path(args.codex_sessions) if args.codex_sessions else home / ".codex/sessions"
    )
    codex = codex_usage(codex_root, repo, window)

    rates_path = (
        Path(args.rates) if args.rates else repo / ".harness/reporting/rates.json"
    )
    cost = estimate_cost(claude, codex, load_rates(rates_path))

    prs = [] if scope.get("offline") else pull_requests(repo, numbers)
    local = {
        name
        for name in _local_issue_branches(repo)
        if ticket_of_branch(name) in numbers
    }
    volume = git_volume(repo, prs, local | set(claude.get("branches") or []), base)
    if (
        not prs
        and volume["totals"]["insertions"] == 0
        and claude.get("status") != "ok"
        and codex.get("status") != "ok"
    ):
        raise StatsError(
            f"epic #{args.epic}: no pull request, branch or session recorded for it or its sub-issues",
            remedy=f"verify epic #{args.epic}'s sub-issue numbers and that its PRs/branches/session logs exist locally, or pass --tickets explicitly",
        )
    tickets = scope["tickets"]
    for ticket in tickets:
        ticket["branches"] = sorted(
            {
                entry["branch"]
                for entry in volume["entries"]
                if entry.get("ticket") == ticket["number"]
            }
        )
    orchestration_state = (
        Path(args.orchestration_state_dir) if args.orchestration_state_dir else None
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": repo.name,
        "epic": scope["epic"],
        "tickets": tickets,
        "tickets_closed": sum(
            1 for t in tickets if str(t.get("state", "")).upper() == "CLOSED"
        ),
        "tickets_total": len(tickets),
        "claude": claude,
        "codex": codex,
        "cache": cache_split(claude),
        "cost": cost,
        "volume": volume,
        "adr_added": len(volume["adr_added"]),
        "orchestration": orchestration_metrics(repo, numbers, orchestration_state),
    }




def main_live_probe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="delivery_stats.py live-probe",
        description="Live snapshot of active Claude Code sessions/subagents on one branch.",
    )
    parser.add_argument("--repo", default=".", help="target project root")
    parser.add_argument("--branch", required=True, help="git branch to probe")
    parser.add_argument("--home", help="home directory holding agent session logs")
    parser.add_argument(
        "--claude-projects",
        action="append",
        help="explicit transcript directory; repeatable when a project has more than one",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    home = Path(args.home).expanduser() if args.home else Path.home()
    project_dirs = (
        [Path(p) for p in args.claude_projects]
        if args.claude_projects
        else claude_project_dirs(home, repo)
    )
    print(
        json.dumps(
            live_probe(project_dirs, args.branch),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        # The report is authored in Russian; a legacy console code page would mangle it.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    if sys.argv[1:2] == ["live-probe"]:
        return main_live_probe(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Delivery statistics for one epic and its tickets."
    )
    parser.add_argument("--repo", default=".", help="target project root")
    parser.add_argument("--epic", type=int, required=True, help="epic issue number")
    parser.add_argument(
        "--base",
        help="base ref for diffing issue branches; defaults to project base_branch",
    )
    parser.add_argument(
        "--tickets",
        help="offline mode: comma-separated issue numbers instead of asking the tracker for sub-issues",
    )
    parser.add_argument(
        "--rates", help="rate card JSON; defaults to .harness/reporting/rates.json"
    )
    parser.add_argument(
        "--orchestration-state-dir",
        help="backend-orchestration ledger state directory; defaults to .harness/orchestration/state under --repo",
    )
    parser.add_argument("--home", help="home directory holding agent session logs")
    parser.add_argument(
        "--claude-projects",
        action="append",
        help="explicit transcript directory; repeatable when a project has more than one",
    )
    parser.add_argument("--codex-sessions", help="explicit Codex sessions directory")
    parser.add_argument(
        "--baseline", help="versioned baseline JSON saved by --save-baseline"
    )
    parser.add_argument(
        "--save-baseline", help="write this report's comparable baseline JSON to a path"
    )
    parser.add_argument("--html", help="write a standalone HTML dashboard to this path")
    parser.add_argument(
        "--json", action="store_true", help="print the full report as JSON"
    )
    args = parser.parse_args()

    try:
        report = build_report(args)
        current_baseline = baseline_snapshot(report)
        if args.baseline:
            report["comparison"] = compare_baseline(
                load_baseline(Path(args.baseline)), current_baseline
            )
        if args.save_baseline:
            save_baseline(current_baseline, Path(args.save_baseline))
        if args.html:
            from harness.reporting.render_html import write_dashboard

            destination = Path(args.html)
            write_dashboard(report, destination)
            report["html"] = str(destination)
    except HarnessError as exc:
        return print_and_exit(exc)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_terminal(report))
        if args.html:
            print(f"HTML: {report['html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
