"""Read-only proposals to connect project-owned entry points to the shared contract."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path


def _mandatory(paragraph: str) -> bool:
    text = " ".join(paragraph.lower().split())
    return all(token in text for token in ("must read", "before", "english", "handoff"))


def _reachable(connected: set[Path], transitions: dict[Path, set[Path]]) -> set[Path]:
    connected = set(connected)
    while True:
        expanded = {
            path for path, targets in transitions.items() if targets & connected
        }
        if expanded.issubset(connected):
            return connected
        connected.update(expanded)


def propose_contract_links(repo: Path, templates: Path) -> list[dict[str, str]]:
    """Return additive diffs; never change seeds or traverse arbitrary project files."""
    contract = (repo / ".harness/docs/technical-english.md").resolve()
    entries = [repo / "AGENTS.md", repo / "CLAUDE.md"]
    entries.extend(
        repo / ".claude/agents" / template.name
        for template in sorted((templates / "agents").glob("*.md"))
        if (repo / ".claude/agents" / template.name).is_file()
    )
    texts = {
        path.resolve(): path.read_text(encoding="utf-8")
        for path in entries
        if path.is_file()
    }
    connected: set[Path] = set()
    direct: set[Path] = set()
    transitions: dict[Path, set[Path]] = {}
    for path, text in texts.items():
        # Examples in fenced code are not active entry-point instructions.
        active = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
        transitions[path] = set()
        for paragraph in active.split("\n\n"):
            for target in re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", paragraph):
                destination = (
                    path.parent / target.strip("<>").split("#", 1)[0]
                ).resolve()
                if destination == contract:
                    direct.add(path)
                    if _mandatory(paragraph):
                        connected.add(path)
                elif destination in texts and _mandatory(paragraph):
                    transitions[path].add(destination)
        for target in re.findall(r"(?m)^\s*@([^\s]+)\s*$", active):
            destination = (path.parent / target).resolve()
            if destination in texts:
                transitions[path].add(destination)
    for path in direct:
        if any(
            _mandatory(part) and "contract linked above" in part
            for part in texts[path].split("\n\n")
        ):
            connected.add(path)
    mandatory = _reachable(connected, transitions)
    # First strengthen existing references. Imports can reach those approved
    # obligations without receiving a duplicate direct link themselves.
    needed = direct - mandatory
    connected = _reachable(mandatory | direct, transitions)
    agents = (repo / "AGENTS.md").resolve()
    if agents not in connected:
        needed.add(agents)
        connected = _reachable(connected | {agents}, transitions)
    needed.update(path.resolve() for path in entries if path.resolve() not in connected)

    proposals = []
    for path in entries:
        resolved = path.resolve()
        original = texts.get(resolved, "")
        if resolved not in needed:
            continue
        if resolved in direct:
            addition = "Before the first English handoff, agents must read the shared Technical English contract linked above.\n"
            reason = "Existing contract reference has no recognized mandatory reading instruction; review this addition."
        else:
            target = Path(os.path.relpath(contract, path.parent)).as_posix()
            addition = f"Before the first English handoff, agents must read\n[Technical English]({target}).\n"
            reason = "No mandatory path to the installed shared contract was found."
        adapted = (
            original
            + (
                ""
                if not original or original.endswith("\n\n")
                else "\n"
                if original.endswith("\n")
                else "\n\n"
            )
            + addition
        )
        relative = path.relative_to(repo).as_posix()
        diff = f"diff --git a/{relative} b/{relative}\n" + "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                original.splitlines(keepends=True),
                adapted.splitlines(keepends=True),
                fromfile=f"a/{relative}" if path.exists() else "/dev/null",
                tofile=f"b/{relative}",
            )
        )
        proposals.append({"path": relative, "reason": reason, "diff": diff})
    return proposals


def print_contract_proposals(proposals: list[dict[str, str]]) -> None:
    """Keep explanations before the patch so the complete trailing diff is reviewable."""
    if not proposals:
        return
    print(
        "\nSeed entry points: review and explicitly approve these additions before applying them by edit/patch."
    )
    print(
        "The CLI has not changed these project-owned files; --force-seed-files is not needed."
    )
    for proposal in proposals:
        print(f"  {proposal['path']}: {proposal['reason']}")
    for proposal in proposals:
        print(proposal["diff"], end="")
