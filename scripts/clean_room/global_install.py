"""Глобальная установка, overlay и совместимость: сценарий clean-room из `scripts/test_clean_room.py`."""

import filecmp
import os
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    HARNESS,
    INSTALL_GLOBAL,
    ROOT,
    run_fails,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Глобальная установка, overlay и совместимость.

    Читает из контекста: `target_home`, `test_root`.
    """
    target_home = ctx.target_home
    test_root = ctx.test_root
    runtime_args = []
    for runtime_name in ("codex", "claude", "kimi", "opencode", "hermes"):
        runtime_args += ["--runtime", runtime_name]
    legacy_roots = [
        target_home / ".agents" / "skills",
        target_home / ".claude" / "skills",
        target_home / ".hermes" / "skills",
    ]
    for legacy_root in legacy_roots:
        legacy_root.mkdir(parents=True, exist_ok=True)
        (legacy_root / "project-harness-bootstrap").symlink_to(
            ROOT / "global-skills" / "project-harness-bootstrap",
            target_is_directory=True,
        )
        (legacy_root / "skill-library").symlink_to(
            ROOT / "global-skills" / "skill-library", target_is_directory=True
        )

    run_ok(INSTALL_GLOBAL + ["--target-home", str(target_home)] + runtime_args)
    run_ok(
        INSTALL_GLOBAL
        + ["--target-home", str(target_home)]
        + runtime_args
        + ["--check"]
    )
    for rel in (
        ".agents/skills/start-project",
        ".claude/skills/start-project",
        ".hermes/skills/start-project",
        ".agents/skills/integrate-project",
        ".claude/skills/integrate-project",
        ".hermes/skills/integrate-project",
    ):
        if not (target_home / rel).is_symlink():
            sys.exit(f"expected symlink missing: {rel}")
    for rel in (
        ".agents/skills/project-harness-bootstrap",
        ".hermes/skills/skill-library",
    ):
        p = target_home / rel
        if p.exists() or p.is_symlink():
            sys.exit(f"retired entry still present: {rel}")

    foreign_home = test_root / "foreign-home"
    (foreign_home / ".agents" / "skills").mkdir(parents=True)
    (foreign_home / "foreign").mkdir(parents=True)
    (foreign_home / ".agents" / "skills" / "skill-library").symlink_to(
        foreign_home / "foreign", target_is_directory=True
    )
    if not run_fails(
        INSTALL_GLOBAL
        + ["--target-home", str(foreign_home), "--runtime", "codex", "--skills-only"]
    ):
        sys.exit("foreign retired entry was unexpectedly accepted")
    foreign_link = foreign_home / ".agents" / "skills" / "skill-library"
    if not foreign_link.is_symlink():
        sys.exit("foreign symlink was unexpectedly replaced with something else")
    if not os.path.samefile(foreign_link, foreign_home / "foreign"):
        sys.exit("foreign symlink was unexpectedly modified")

    overlay_home = test_root / "overlay-home"
    overlay_home.mkdir(parents=True)
    run_ok(
        INSTALL_GLOBAL
        + ["--target-home", str(overlay_home)]
        + runtime_args
        + ["--skills-only"]
    )
    if (overlay_home / ".codex" / "AGENTS.md").exists():
        sys.exit("--skills-only unexpectedly wrote a profile file")
    for rel in (
        ".agents/skills/start-project",
        ".hermes/skills/start-project",
        ".agents/skills/integrate-project",
        ".hermes/skills/integrate-project",
    ):
        if not (overlay_home / rel).is_symlink():
            sys.exit(f"expected symlink missing: {rel}")

    legacy = test_root / "legacy"
    (legacy / ".harness" / "skills" / "project-only").mkdir(parents=True)
    (legacy / ".harness" / "skills" / "ask-matt").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=legacy, check=True)
    (legacy / "AGENTS.md").write_text("# Legacy project\n", encoding="utf-8")
    (legacy / ".harness" / "skills" / "project-only" / "SKILL.md").write_text(
        "---\nname: project-only\ndescription: Use for the legacy project-only workflow.\n---\n\n# Project only\n",
        encoding="utf-8",
    )
    tool_cache = (
        legacy
        / ".harness"
        / "skills"
        / "project-only"
        / "tool"
        / "node_modules"
        / ".vite"
    )
    tool_cache.mkdir(parents=True)
    (
        legacy / ".harness" / "skills" / "project-only" / "tool" / ".gitignore"
    ).write_text("node_modules/\n", encoding="utf-8")
    (tool_cache / "results.json").write_text(
        "initial runtime result\n", encoding="utf-8"
    )
    (legacy / ".harness" / "skills" / "ask-matt" / "SKILL.md").write_text(
        "old fork\n", encoding="utf-8"
    )
    (legacy / ".harness" / "harness.lock").write_text(
        '{"schema": 0, "files": {}}\n', encoding="utf-8"
    )

    run_ok(
        HARNESS
        + [
            "adopt",
            str(legacy),
            "--capability",
            "mattpocock-suite",
            "--replace-conflicts",
        ]
    )
    if not (legacy / ".harness" / "skills" / "project-only" / "SKILL.md").is_file():
        sys.exit("adopt dropped a project-only skill")
    if not filecmp.cmp(
        ROOT
        / "skills"
        / "vendor"
        / "mattpocock"
        / "engineering"
        / "ask-matt"
        / "SKILL.md",
        legacy / ".harness" / "skills" / "ask-matt" / "SKILL.md",
        shallow=False,
    ):
        sys.exit("adopt --replace-conflicts did not restore the canonical ask-matt")
    if not run_fails(HARNESS + ["health", str(legacy)], quiet_all=True):
        sys.exit("unlocked project-owned skill unexpectedly passed health")

    run_ok(HARNESS + ["lock-project-skills", str(legacy)])
    if "node_modules" in (
        legacy / ".harness" / "overlays" / "project-local.lock"
    ).read_text(encoding="utf-8"):
        sys.exit("project-local lock unexpectedly contains ignored runtime files")
    run_ok(HARNESS + ["health", str(legacy)])
    (tool_cache / "results.json").write_text(
        "changed runtime result\n", encoding="utf-8"
    )
    run_ok(HARNESS + ["health", str(legacy)])

    print("clean-room verification passed")
