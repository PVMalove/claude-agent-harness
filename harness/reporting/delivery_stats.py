#!/usr/bin/env python3
"""Delivery statistics for one finished epic and every ticket under it.

Reads only local evidence: coding-agent session transcripts, this repository's git history, and the
issue tracker CLI.  Nothing is sent anywhere.  Every number the tool cannot source is reported as
missing rather than as zero, and figures that can only be attributed approximately say so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] delivery_stats requires Python %s+ (found %s).\n"
        % (".".join(map(str, MIN_PYTHON)), sys.version.split()[0])
    )
    raise SystemExit(1)

MISSING = "нет данных"
CLAUDE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")


class StatsError(Exception):
    """A request that cannot be answered from local evidence."""


def _run(command: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8"
        )
    except OSError as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git(repo: Path, *arguments: str) -> str:
    code, out, err = _run(["git", "-C", str(repo), *arguments])
    if code != 0:
        raise StatsError(f"git {' '.join(arguments)} failed: {err or out or 'unknown error'}")
    return out


def _read_jsonl(path: Path) -> Iterator[dict]:
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


def _moment(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _same_path(recorded: object, repo: Path, _cache: dict = {}) -> bool:
    """Whether a recorded working directory is this repository.

    A string comparison is not enough: Windows records an 8.3 short path ("RUNNER~1") for the same
    directory the caller resolved, and case differs freely. Resolution is cached because the same
    handful of directories repeat across every record of a session.
    """
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


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------------------- scope


def _project_config(repo: Path) -> dict:
    path = repo / ".harness/project.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StatsError(".harness/project.json is not valid JSON") from exc
    return value if isinstance(value, dict) else {}


def _gh(repo: Path, *arguments: str) -> Any:
    code, out, err = _run(["gh", *arguments], cwd=repo)
    if code == 127:
        raise StatsError("the gh CLI is required to resolve an epic and is not available")
    if code != 0:
        raise StatsError(f"gh {' '.join(arguments)} failed: {err or out or 'unknown error'}")
    try:
        return json.loads(out) if out else None
    except ValueError as exc:
        raise StatsError("gh returned output that is not valid JSON") from exc


ISSUE_BRANCH = re.compile(r"^[a-z]+/issue-(\d+)-")


def ticket_of_branch(branch: object) -> Optional[int]:
    if not isinstance(branch, str):
        return None
    match = ISSUE_BRANCH.match(branch)
    return int(match.group(1)) if match else None


def resolve_scope(repo: Path, epic: int) -> dict:
    """Epic plus every ticket linked under it.

    Scope is a set of ticket numbers, not a set of live branches: an epic is usually measured after
    its work merged, and merged issue branches are normally deleted. Everything downstream matches on
    the ticket number encoded in a branch name, which survives that deletion in pull requests and in
    session transcripts alike.
    """
    parent = _gh(repo, "issue", "view", str(epic), "--json", "number,title,state,createdAt,closedAt,url")
    children = _gh(repo, "issue", "view", str(epic), "--json", "subIssues") or {}
    nodes = ((children.get("subIssues") or {}).get("nodes")) or []
    tickets = [
        {"number": parent["number"], "title": parent["title"], "state": parent["state"], "role": "epic"}
    ]
    for node in nodes:
        tickets.append(
            {"number": node["number"], "title": node.get("title", ""), "state": node.get("state", ""), "role": "child"}
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


def offline_scope(epic: int, tickets: str) -> dict:
    """Scope stated by the caller instead of read from the tracker.

    This is the offline mode: no tracker CLI is contacted, so ticket titles and states are unknown
    and reported as such rather than guessed. Code volume then comes from local refs only.
    """
    numbers = []
    for chunk in tickets.replace(",", " ").split():
        if not chunk.isdigit():
            raise StatsError(f"--tickets expects issue numbers, got {chunk!r}")
        numbers.append(int(chunk))
    if epic not in numbers:
        numbers.insert(0, epic)
    return {
        "epic": {"number": epic, "title": MISSING, "state": MISSING, "url": "", "created_at": None, "closed_at": None},
        "tickets": [
            {"number": number, "title": MISSING, "state": MISSING, "role": "epic" if number == epic else "child"}
            for number in numbers
        ],
        "numbers": set(numbers),
        "offline": True,
    }


def pull_requests(repo: Path, numbers: set) -> list:
    """Pull requests whose head branch belongs to a ticket in scope.

    GitHub keeps a merged pull request's diffstat after its branch is deleted, so this is the
    durable source for code volume; local refs are only a fallback for work with no pull request.
    """
    listed = _gh(
        repo, "pr", "list", "--state", "all", "--limit", "200", "--json",
        "number,headRefName,state,additions,deletions,changedFiles,mergedAt,url",
    ) or []
    matched = []
    for entry in listed:
        ticket = ticket_of_branch(entry.get("headRefName"))
        if ticket in numbers:
            matched.append({**entry, "ticket": ticket})
    return matched


# ------------------------------------------------------------------------------------ git volume


def git_volume(repo: Path, prs: list, extra_branches: set, base: str) -> dict:
    """Code volume per ticket, from pull requests first and local refs only where none exists."""
    entries = []
    totals = {"commits": 0, "insertions": 0, "deletions": 0, "files": 0, "pull_requests": 0}
    covered: set = set()

    adr: set = set()
    for pr in prs:
        detail = _gh(repo, "pr", "view", str(pr["number"]), "--json", "commits,files") or {}
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
            entries.append({"source": "branch", "branch": branch, "ticket": ticket_of_branch(branch),
                            "status": MISSING, "reason": "ветка удалена, pull request не найден"})
            continue
        try:
            merge_base = _git(repo, "merge-base", base, ref)
            numstat = _git(repo, "diff", "--numstat", merge_base, ref)
            commits = [line for line in _git(repo, "rev-list", f"{merge_base}..{ref}").splitlines() if line]
        except StatsError:
            entries.append({"source": "branch", "branch": branch, "ticket": ticket_of_branch(branch),
                            "status": MISSING, "reason": "не сравнить с базовой веткой"})
            continue
        insertions = deletions = 0
        files: set = set()
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
    return {"base": base, "entries": entries, "totals": totals, "adr_added": sorted(adr)}


def _is_adr(path: str) -> bool:
    return path.startswith("docs/adr/") and path.endswith(".md") and not path.endswith("template.md")


def _added_by(repo: Path, path: str) -> Optional[str]:
    """The commit that first added a path.

    A pull request that only edits an existing decision record must not be counted as adding one, so
    membership in the pull request's own commits is what decides it. This undercounts a squash-merged
    pull request, whose original commit IDs no longer exist — an undercount being the honest failure
    here, never an invented record.
    """
    # --all: the adding commit may live on a branch that is not the current HEAD, which is the
    # normal case when the epic is measured from a different worktree or before its merge.
    code, out, _ = _run(
        ["git", "-C", str(repo), "log", "--all", "--diff-filter=A", "--format=%H", "--", path]
    )
    if code != 0 or not out:
        return None
    return out.splitlines()[-1].strip()


def _added_adr(repo: Path, base: str, ref: str) -> list:
    try:
        merge_base = _git(repo, "merge-base", base, ref)
        names = _git(repo, "diff", "--name-only", "--diff-filter=A", merge_base, ref)
    except StatsError:
        return []
    return [path for path in names.splitlines() if _is_adr(path)]


def _local_issue_branches(repo: Path) -> set:
    names: set = set()
    for scope in ("refs/heads", "refs/remotes/origin"):
        code, out, _ = _run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", scope])
        if code != 0:
            continue
        for name in out.splitlines():
            names.add(name.split("origin/", 1)[-1] if scope.endswith("origin") else name)
    return {name for name in names if ticket_of_branch(name) is not None}


def _ref_exists(repo: Path, ref: str) -> bool:
    code, _, _ = _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return code == 0


# --------------------------------------------------------------------------------- claude usage


CWD_PROBE_TRANSCRIPTS = 5
CWD_PROBE_RECORDS = 200


def _slug_variants(repo: Path) -> list:
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
        return any(path.is_file() and path.stat().st_size > 0 for path in directory.glob("*.jsonl"))
    except OSError:
        return False


def _records_repo(directory: Path, repo: Path) -> bool:
    """Whether this directory's transcripts were recorded in this repository.

    Probes several transcripts, newest first, and several records of each: the working directory is
    not on every record, so inspecting only the first one misses it almost always.
    """
    try:
        transcripts = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return False
    for transcript in transcripts[:CWD_PROBE_TRANSCRIPTS]:
        for index, record in enumerate(_read_jsonl(transcript)):
            if index >= CWD_PROBE_RECORDS:
                break
            if _same_path(record.get("cwd"), repo):
                return True
    return False


def claude_project_dirs(home: Path, repo: Path) -> list:
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


def claude_usage(project_dirs: list, numbers: set) -> dict:
    """Token usage of every Claude Code turn recorded on a branch belonging to a ticket in scope."""
    if not project_dirs:
        return {"status": MISSING, "reason": "нет транскриптов Claude Code для этого репозитория"}
    branches_seen: set = set()
    models: dict = {}
    sessions: set = set()
    sidechain = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    thinking = 0
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    quota: Any = None
    turns = 0

    transcripts = [path for directory in project_dirs for path in sorted(directory.glob("*.jsonl"))]
    for transcript in transcripts:
        for record in _read_jsonl(transcript):
            branch = record.get("gitBranch")
            if ticket_of_branch(branch) not in numbers:
                continue
            branches_seen.add(branch)
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            turns += 1
            sessions.add(record.get("sessionId") or transcript.stem)
            moment = _moment(record.get("timestamp"))
            if moment:
                first = moment if first is None or moment < first else first
                last = moment if last is None or moment > last else last
            bucket = models.setdefault(
                message.get("model") or "unknown", {field: 0 for field in CLAUDE_FIELDS} | {"turns": 0}
            )
            bucket["turns"] += 1
            for field in CLAUDE_FIELDS:
                bucket[field] += _int(usage.get(field))
            details = usage.get("output_tokens_details")
            if isinstance(details, dict):
                thinking += _int(details.get("thinking_tokens"))
            if record.get("isSidechain"):
                sidechain["turns"] += 1
                sidechain["input_tokens"] += sum(_int(usage.get(f)) for f in CLAUDE_FIELDS[:3])
                sidechain["output_tokens"] += _int(usage.get("output_tokens"))
            limits = record.get("quotaLimits")
            if isinstance(limits, dict) and limits:
                quota = limits

    if not turns:
        return {"status": MISSING, "reason": "нет ходов Claude Code на ветках этих тикетов"}
    return {
        "status": "ok",
        "attribution": "exact",
        "sources": [str(directory) for directory in project_dirs],
        "branches": sorted(branches_seen),
        "models": models,
        "turns": turns,
        "sessions": len(sessions),
        "thinking_tokens": thinking,
        "sidechain": sidechain,
        "first_activity": first.isoformat() if first else None,
        "last_activity": last.isoformat() if last else None,
        "quota": quota if quota else MISSING,
    }


# ---------------------------------------------------------------------------------- codex usage


def codex_usage(sessions_root: Optional[Path], repo: Path, window: tuple) -> dict:
    """Codex records no branch, only a working directory and a timestamp.

    Work is therefore attributed to the epic by repository plus the activity window of its issue
    branches.  That is an estimate and the report must present it as one.
    """
    if sessions_root is None or not sessions_root.is_dir():
        return {"status": MISSING, "reason": "на этой машине нет сессий Codex"}
    start, end = window
    if start is None or end is None:
        return {"status": MISSING, "reason": "нет окна активности, к которому можно отнести работу Codex"}
    models: dict = {}
    sessions: set = set()
    rate_limits: Any = None
    turns = 0

    for transcript in sorted(sessions_root.rglob("rollout-*.jsonl")):
        current_model = "unknown"
        in_repo = False
        counted: set = set()
        pending: list = []
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
            if not isinstance(delta, dict):
                continue
            pending.append((current_model, delta))
            limits = record.get("rate_limits") or payload.get("rate_limits")
            if isinstance(limits, dict) and limits:
                rate_limits = limits
        if not in_repo or not pending:
            continue
        sessions.add(transcript.stem)
        for model, delta in pending:
            turns += 1
            bucket = models.setdefault(model, {field: 0 for field in CODEX_FIELDS} | {"reasoning_output_tokens": 0, "turns": 0})
            bucket["turns"] += 1
            for field in CODEX_FIELDS:
                bucket[field] += _int(delta.get(field))
            bucket["reasoning_output_tokens"] += _int(delta.get("reasoning_output_tokens"))

    if not turns:
        return {"status": MISSING, "reason": "нет ходов Codex по этому репозиторию внутри окна"}
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


# ----------------------------------------------------------------------------------------- cost


def load_rates(path: Optional[Path]) -> dict:
    if path is None or not path.is_file():
        return {"status": MISSING, "reason": "тариф не настроен", "models": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StatsError(f"rate card is not valid JSON: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
        raise StatsError("rate card must be an object with a models object")
    value.setdefault("status", "ok")
    return value


def estimate_cost(claude: dict, codex: dict, rates: dict) -> dict:
    """Cost by the supplied rate card only. No prices are built into this tool."""
    if rates.get("status") != "ok":
        return {"status": MISSING, "reason": rates.get("reason", "тариф не настроен")}
    table = rates["models"]
    per_model = []
    total = 0.0
    uncached_total = 0.0
    unpriced = []

    def price(model: str, fresh: int, cache_write: int, cache_read: int, output: int) -> None:
        nonlocal total, uncached_total
        card = table.get(model)
        if not isinstance(card, dict):
            unpriced.append(model)
            return
        rate_in = float(card.get("input", 0)) / 1_000_000
        rate_out = float(card.get("output", 0)) / 1_000_000
        write_multiplier = float(card.get("cache_write_multiplier", 1.25))
        read_multiplier = float(card.get("cache_read_multiplier", 0.1))
        cost = (
            fresh * rate_in
            + cache_write * rate_in * write_multiplier
            + cache_read * rate_in * read_multiplier
            + output * rate_out
        )
        uncached = (fresh + cache_write + cache_read) * rate_in + output * rate_out
        total += cost
        uncached_total += uncached
        per_model.append({"model": model, "cost": round(cost, 6), "uncached_cost": round(uncached, 6)})

    if claude.get("status") == "ok":
        for model, bucket in claude["models"].items():
            price(
                model,
                bucket["input_tokens"],
                bucket["cache_creation_input_tokens"],
                bucket["cache_read_input_tokens"],
                bucket["output_tokens"],
            )
    if codex.get("status") == "ok":
        for model, bucket in codex["models"].items():
            price(
                model,
                bucket["input_tokens"] - bucket["cached_input_tokens"],
                bucket["cache_write_input_tokens"],
                bucket["cached_input_tokens"],
                bucket["output_tokens"],
            )

    per_model.sort(key=lambda item: item["cost"], reverse=True)
    return {
        "status": "ok",
        "currency": rates.get("currency", "USD"),
        "rates_effective": rates.get("effective_date", MISSING),
        "rates_source": rates.get("source", MISSING),
        "per_model": per_model,
        "total": round(total, 6),
        "uncached_total": round(uncached_total, 6),
        "cache_saving": round(uncached_total - total, 6),
        "unpriced_models": sorted(set(unpriced)),
    }


# -------------------------------------------------------------------------------------- summary


def cache_split(claude: dict) -> Any:
    if claude.get("status") != "ok":
        return MISSING
    fresh = sum(bucket["input_tokens"] for bucket in claude["models"].values())
    write = sum(bucket["cache_creation_input_tokens"] for bucket in claude["models"].values())
    read = sum(bucket["cache_read_input_tokens"] for bucket in claude["models"].values())
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


def build_report(args: argparse.Namespace) -> dict:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise StatsError(f"not a git repository: {repo}")
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
    window = (_moment(claude.get("first_activity")), _moment(claude.get("last_activity")))
    codex_root = Path(args.codex_sessions) if args.codex_sessions else home / ".codex/sessions"
    codex = codex_usage(codex_root, repo, window)

    rates_path = Path(args.rates) if args.rates else repo / ".harness/reporting/rates.json"
    cost = estimate_cost(claude, codex, load_rates(rates_path))

    prs = [] if scope.get("offline") else pull_requests(repo, numbers)
    local = {name for name in _local_issue_branches(repo) if ticket_of_branch(name) in numbers}
    volume = git_volume(repo, prs, local | set(claude.get("branches") or []), base)
    if not prs and volume["totals"]["insertions"] == 0 and claude.get("status") != "ok" and codex.get("status") != "ok":
        raise StatsError(
            f"epic #{args.epic}: no pull request, branch or session recorded for it or its sub-issues"
        )
    tickets = scope["tickets"]
    for ticket in tickets:
        ticket["branches"] = sorted(
            {entry["branch"] for entry in volume["entries"] if entry.get("ticket") == ticket["number"]}
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": repo.name,
        "epic": scope["epic"],
        "tickets": tickets,
        "tickets_closed": sum(1 for t in tickets if str(t.get("state", "")).upper() == "CLOSED"),
        "tickets_total": len(tickets),
        "claude": claude,
        "codex": codex,
        "cache": cache_split(claude),
        "cost": cost,
        "volume": volume,
        "adr_added": len(volume["adr_added"]),
    }


# --------------------------------------------------------------------------------------- output


def _thousands(value: object) -> str:
    if not isinstance(value, int):
        return str(value)
    return f"{value:,}".replace(",", " ")


def _compact(value: object) -> str:
    if not isinstance(value, int):
        return str(value)
    for limit, suffix in ((1_000_000_000, "млрд"), (1_000_000, "млн"), (1_000, "тыс")):
        if value >= limit:
            return f"{value / limit:.2f}".rstrip("0").rstrip(".").replace(".", ",") + f" {suffix}"
    return str(value)


def _ru(value: object, places: int = 2) -> str:
    """Russian decimal comma for a report the reader sees in Russian."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


def render_terminal(report: dict) -> str:
    epic = report["epic"]
    lines = [
        f"Эпик #{epic['number']} — {epic['title']}",
        f"Тикетов закрыто: {report['tickets_closed']} из {report['tickets_total']}",
    ]
    totals = report["volume"]["totals"]
    lines.append(
        f"Код: +{_thousands(totals['insertions'])} / -{_thousands(totals['deletions'])} строк, "
        f"{totals['files']} файлов, {totals['commits']} коммитов"
    )
    adr = report["adr_added"]
    lines.append(f"ADR добавлено: {adr}")

    claude = report["claude"]
    if claude.get("status") == "ok":
        for model, bucket in sorted(claude["models"].items(), key=lambda kv: -kv[1]["output_tokens"]):
            lines.append(
                f"  Claude {model}: вход {_compact(sum(bucket[f] for f in CLAUDE_FIELDS[:3]))}, "
                f"выход {_compact(bucket['output_tokens'])}, ходов {bucket['turns']}"
            )
    else:
        lines.append(f"  Claude: {claude.get('reason', MISSING)}")

    codex = report["codex"]
    if codex.get("status") == "ok":
        for model, bucket in sorted(codex["models"].items(), key=lambda kv: -kv[1]["output_tokens"]):
            lines.append(
                f"  Codex {model}: вход {_compact(bucket['input_tokens'])}, "
                f"выход {_compact(bucket['output_tokens'])}, ходов {bucket['turns']} (оценка)"
            )
    else:
        lines.append(f"  Codex: {codex.get('reason', MISSING)}")

    cache = report["cache"]
    if isinstance(cache, dict):
        lines.append(
            f"Кеш Claude: чтение {_ru(cache['cache_read_percent'], 3)}%, запись {_ru(cache['cache_write_percent'], 3)}%, "
            f"свежий вход {_ru(cache['fresh_percent'], 3)}%"
        )
    else:
        lines.append(f"Кеш Claude: {MISSING}")

    cost = report["cost"]
    if cost.get("status") == "ok":
        lines.append(
            f"Стоимость по ставкам {cost['rates_effective']}: {_ru(cost['total'])} {cost['currency']}; "
            f"без кеша было бы {_ru(cost['uncached_total'])} (экономия {_ru(cost['cache_saving'])})"
        )
        if cost["unpriced_models"]:
            lines.append(f"  Без ставок в тарифе: {', '.join(cost['unpriced_models'])}")
    else:
        lines.append(f"Стоимость: {cost.get('reason', MISSING)}")
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        # The report is authored in Russian; a legacy console code page would mangle it.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description="Delivery statistics for one epic and its tickets.")
    parser.add_argument("--repo", default=".", help="target project root")
    parser.add_argument("--epic", type=int, required=True, help="epic issue number")
    parser.add_argument("--base", help="base ref for diffing issue branches; defaults to project base_branch")
    parser.add_argument(
        "--tickets",
        help="offline mode: comma-separated issue numbers instead of asking the tracker for sub-issues",
    )
    parser.add_argument("--rates", help="rate card JSON; defaults to .harness/reporting/rates.json")
    parser.add_argument("--home", help="home directory holding agent session logs")
    parser.add_argument(
        "--claude-projects",
        action="append",
        help="explicit transcript directory; repeatable when a project has more than one",
    )
    parser.add_argument("--codex-sessions", help="explicit Codex sessions directory")
    parser.add_argument("--html", help="write a standalone HTML dashboard to this path")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()

    try:
        report = build_report(args)
        if args.html:
            from render_html import write_dashboard  # local module, shipped beside this CLI

            destination = Path(args.html)
            write_dashboard(report, destination)
            report["html"] = str(destination)
    except StatsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_terminal(report))
        if args.html:
            print(f"HTML: {report['html']}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
