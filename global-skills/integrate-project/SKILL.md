---
name: integrate-project
description: Audit an existing, already-substantial codebase and fold this harness into it without disrupting what's already there. Use when a repository already has meaningful code, conventions, CI, or other AI-tool configuration and needs the harness (skills, AGENTS.md, discovery links) integrated for the first time. For a brand-new idea, an empty or near-empty repository, or revising an already-harnessed project, use `start-project` instead.
---

# Integrate Project

Fold a portable harness onto a codebase that already exists, without guessing what the project already
knows about itself. Read real files before asking; treat the repository, not the conversation, as the
primary source of truth.

Resolve this skill's real path and verify that the public `agent-harness` root two levels above it
contains `skills/REGISTRY.md` and `harness/bin/harness`. This resolution is complete only when both
files exist; otherwise report that the public package is incomplete instead of guessing another
installation.

## Audit

1. **Stack and commands.** Read manifests directly — `package.json` (and its `scripts`), lockfiles,
   `pyproject.toml`/`requirements.txt`, `go.mod`, `Cargo.toml`, `Gemfile`, or equivalent. Derive the
   stack and the real install/run/test/lint/build commands from what is actually configured, not from
   a README claim or the user's memory.
2. **Conventions the manifest doesn't state.** Read CI workflow files (`.github/workflows/*`,
   `.gitlab-ci.yml`, etc.) for the commands CI actually runs — ground truth over documentation, which
   drifts. Read recent branch names (`git branch -a`, `git log --all --oneline -20`) and the remote's
   default branch for `branch_pattern` and `base_branch`. Read the existing test/docs directory layout
   rather than assuming a convention.
3. **Existing AI-tool configuration.** Look for `AGENTS.md`, `CLAUDE.md`, `.cursor/rules`,
   `.github/copilot-instructions.md`, and a `.claude/` directory not managed by this harness (no
   `.harness/harness.lock` yet). Read what is there. An existing instruction that conflicts with this
   harness's conventions is a finding to reconcile with the owner, not a file to overwrite silently.
4. **Domain.** Classify the project's primary work — software, content, research/knowledge,
   operations, personal, or another explicit domain — from what the repository actually contains.
5. **What files can't answer.** Definition of Done, delivery/merge policy, and forbidden actions
   rarely live in the repository. Note these as open questions instead of guessing them from stack
   conventions.

Audit is read-only. Do not write, install, or select a capability during this phase.

## Present and confirm

Summarise findings compactly: stack, real commands, branch/CI convention, detected domain, any
existing AI-tool configuration and where it conflicts with this harness, and the recommended
capability. Ask only what Audit left genuinely open — do not re-ask what a file already answered.
Do not proceed until the owner confirms this manifest.

## Install

Continue with `global-skills/start-project/SKILL.md`'s Assemble section from step 2 onward, using
this audit in place of its step 1 (which this skill's Audit phase already covers, in more depth), and
its Finish section unchanged. A repository reaching this skill has no existing harness by definition,
so Assemble step 5 (`harness init`) applies, not step 6 (`harness update`/`adopt`) — unless Audit
found a `.harness/harness.lock` already present, in which case stop and defer to `start-project`
entirely: that is routine harness maintenance, not a first integration.
