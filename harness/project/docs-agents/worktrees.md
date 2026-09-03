# Parallel work: git worktrees

For running more than one feature branch at once without one session's dirty working tree stepping on another's, use Claude Code's native `EnterWorktree` / `ExitWorktree` tools — not manual `git worktree add` + a separate terminal multiplexer. Where `tmux` is installed, `EnterWorktree` attaches a `tmux` session to the worktree automatically and `ExitWorktree` manages its lifecycle (`keep` leaves it running, `remove` kills it) — nothing to configure either way. Where it isn't (e.g. no `tmux`/no full Linux distro under WSL), `EnterWorktree` still gives the same isolation without it.

## When to use it

Only when explicitly asked — by the developer directly, or by this doc. Don't reach for a worktree on a normal single-branch task; the regular `feature/<ticket-id>` + PR flow in [git-workflow.md](./git-workflow.md) covers that. Use a worktree when the developer wants to work on (or have an agent work on) more than one ticket in parallel, so each gets its own working directory and branch instead of sharing one.

For a subagent spawned via the `Agent` tool to work on an independent ticket in parallel, pass `isolation: "worktree"` on that call instead of manually creating one — same underlying mechanism, scoped to that subagent.

## Conventions

- **Branch naming still applies.** `EnterWorktree`'s `name` parameter becomes the new branch name — use the same format as any other branch in this repo, per `branch_pattern` in `.harness/project.json` (see [git-workflow.md](./git-workflow.md#2-workflow-sequence)). Don't let it default to a random name for ticket work.
- **Base ref**: a fresh worktree for a child ticket branches from the epic's recorded `origin/<integration-branch>`; an epic-less task uses `origin/<base_branch>` from `.harness/project.json`. Use the current local `HEAD` only when the task specifically requires it (`worktree.baseRef` setting).
- **Everything else in [git-workflow.md](./git-workflow.md) still applies inside a worktree** — TDD, `qa-gate` before opening a PR, the PR confirmation checkpoint, never merging. A worktree changes *where* the work happens, not the process.
- **Cleanup**: once a ticket in a worktree is done and its PR is open (or abandoned), exit with `ExitWorktree`. Use `remove` for a finished/abandoned ticket, `keep` only if the developer wants to return to it later. Don't leave worktrees accumulating under `.claude/worktrees/`.
