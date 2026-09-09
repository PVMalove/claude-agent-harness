---
name: to-pr
description: Prepare and open a pull request for an already pushed issue branch. Use after `/implement` offers it, or when the developer explicitly asks to open a PR.
disable-model-invocation: true
---

# To PR

Create the pull request for the named ticket. This is a manual command: never run it merely because `/implement` completed.

1. Resolve the ticket and its exact PR target branch from the ticket's `## Integration Branch` section, its parent epic, or `base_branch` for an epic-less ticket. If this cannot be resolved, stop and ask the developer.
2. Verify that the current branch matches `branch_pattern`, is neither `base_branch` nor `integration/*`, has no uncommitted changes, and has no commits absent from its upstream branch. If the branch has not been pushed, stop and ask the developer to return to `/implement`.
3. Run `/qa-gate` if this repository provides it. Stop on failure.
4. Prepare the PR body using `docs/agents/git-workflow.md` §3. A developer may manually run `pr-composer` in the coding application; otherwise fill in the template directly. Resolve the repository default branch and use `Closes #<ID>` only for that target, otherwise `Related to #<ID>`.
5. Ask the developer for explicit confirmation that the branch is ready to become a PR. Stop for their answer.
6. After approval, open the PR/MR with the tracker CLI and `--body-file <path>`. If the ticket carries `task-report::required`, publish its completion report on the ticket, unless the developer asked to skip it.
7. Return the PR/MR link in the primary session and ask whether the developer wants to review it. Never merge it. After the developer confirms the merge, close a `Related to #<ID>` ticket explicitly; for `Closes #<ID>`, verify that the tracker closed it.
