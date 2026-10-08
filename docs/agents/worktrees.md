# Parallel work: git worktrees

This guide covers user-managed parallel worktrees and the issue worktree that a coordinator
prepares before `batch create`. The coordinator protocol is in `.harness/orchestration/playbook.md`.

For running more than one feature branch at once without one session's dirty working tree stepping on another's, use Claude Code's native `EnterWorktree` / `ExitWorktree` tools — not manual `git worktree add` + a separate terminal multiplexer. Where `tmux` is installed, `EnterWorktree` attaches a `tmux` session to the worktree automatically and `ExitWorktree` manages its lifecycle (`keep` leaves it running, `remove` kills it) — nothing to configure either way. Where it isn't (e.g. no `tmux`/no full Linux distro under WSL), `EnterWorktree` still gives the same isolation without it.

## When to use it

Only when explicitly asked — by the developer directly, or by this doc. Don't reach for a worktree on a normal single-branch task; the regular issue-branch + PR flow in [git-workflow.md](./git-workflow.md) covers that. Use a worktree when the developer wants to work on (or have an agent work on) more than one ticket in parallel, so each gets its own working directory and branch instead of sharing one.

For a subagent spawned via the `Agent` tool to work on an independent ticket in parallel, pass `isolation: "worktree"` on that call instead of manually creating one — same underlying mechanism, scoped to that subagent.

## Conventions

- **Branch naming still applies.** `EnterWorktree`'s `name` parameter drives the new branch name, but it is not copied verbatim: the runtime has been observed to prefix it and to replace `/`, turning `feature/issue-366-checkout-quote` into `worktree-feature+issue-366-checkout-quote` — which does not satisfy `branch_pattern` in `.harness/project.json` (see [git-workflow.md](./git-workflow.md#2-workflow-sequence)). Check the branch the tool actually created and rename it before committing anything. Don't let it default to a random name for ticket work.
- **A session that only provisions a worktree for someone else does not enter it.** `EnterWorktree` is for a session that will do the work in that branch itself. A coordinator session driving `coordinator.py` is not that: its state lives in `.harness/orchestration/state/` relative to the repository root it passes as `--repo`, so switching into the worktree forks that state into a second copy and hides the batch from the root. Such a session creates the tree with plain `git worktree add`, stays in the main checkout, and passes only the path on to the batch.
- **Never change tool configuration to make a worktree behave.** Editing `.claude/settings.local.json` — `worktree.baseRef` included — to steer one task alters the project for every later session. If the default base ref is wrong for the task, create the tree explicitly from the ref you want instead.
- **Base ref**: a fresh worktree for a child ticket branches from the epic's recorded `origin/<integration-branch>`; an epic-less task uses `origin/<base_branch>` from `.harness/project.json`. Use the current local `HEAD` only when the task specifically requires it (`worktree.baseRef` setting).
- **Everything else in [git-workflow.md](./git-workflow.md) still applies inside a worktree** — TDD, `qa-gate` before opening a PR, the PR confirmation checkpoint, never merging. A worktree changes *where* the work happens, not the process.
- **Cleanup**: once a ticket in a worktree is done and its PR is open (or abandoned), exit with `ExitWorktree`. Use `remove` for a finished/abandoned ticket, `keep` only if the developer wants to return to it later. Don't leave worktrees accumulating under `.claude/worktrees/`.
- **Managed harness worktrees**: create coordinator and CLI-owned issue checkouts under the main checkout's `.harness/.sandboxes/worktrees/`. Preview stale clean worktrees with `harness cleanup <repo> --mode hard`; `--apply --confirm HARD` removes eligible directories and their local branches. Worktrees on `integration/*` branches and clean detached-HEAD worktrees (e.g. for code review) qualify too once their commits are on `origin`; detached worktrees have no branch to delete. Ledger-referenced, dirty, current, and unpushed worktrees stay in place. Remote branches stay in place.

## Coordinator issue worktree

A coordinator (`/implement`) must create the issue worktree before `batch create`. `batch create`
refuses a worktree that `git worktree list` does not show: `worktree '<path>' is not registered by git worktree`.
The coordinator runs these commands in the main checkout and does not enter the new worktree:

```bash
git fetch origin <integration-branch>
git worktree add --no-track -b feature/issue-<ID>-<slug> .harness/.sandboxes/worktrees/issue-<ID>-<slug> origin/<integration-branch>
```

- `<integration-branch>` is the ticket's `## Integration Branch` value. If the ticket has no such section, use its `## Git base` value, then the same section of its parent epic. For a task without an epic, use `base_branch` from `.harness/project.json`. If the value is absent for a ticket that has an epic, stop and report a blocker. Do not guess it.
- `--no-track` is required. Without it, `git worktree add -b` writes the upstream of the new branch to `.git/config`. In a sandbox, `.git/config` can be write-protected. The command then stops half done: the branch exists, but the worktree does not.
- `git push -u origin feature/issue-<ID>-<slug>` (see [git-workflow.md](./git-workflow.md#2-workflow-sequence), step 2) sets the upstream later. It writes `.git/config` too, so it can fail the same way in a sandbox. If it fails, push with `git push origin feature/issue-<ID>-<slug>` and tell the developer that the upstream is not set.
- Pass the absolute path of the worktree as `--worktree`, the branch as `--branch`, and the integration branch as `--integration-ref` to `batch create`. For a task without an epic, omit `--integration-ref`.

**Recovery after a partial failure.** If `git worktree add -b` fails and `git branch --list feature/issue-<ID>-<slug>` shows the branch, the branch exists and the worktree does not. Do not create the branch again and do not delete it. Run:

```bash
git worktree add .harness/.sandboxes/worktrees/issue-<ID>-<slug> feature/issue-<ID>-<slug>
```

Before this command, check that the branch points to the commit of `origin/<integration-branch>`: `git rev-parse feature/issue-<ID>-<slug> origin/<integration-branch>`. If the branch has other commits, stop and report a blocker. Then confirm the worktree with `git worktree list`.
