# Git workflow: feature branch + PR

This guide defines only the rules for Git, tickets, and PRs. The opt-in backend orchestration, which `/implement` drives, follows `.harness/orchestration/playbook.md`.

### 1. Fundamental Constraints & Tooling
* **Zero Direct Commits:** `base_branch` and every `integration/*` branch are protected targets. Agents commit and push only from an isolated issue branch matching `branch_pattern`. `/to-spec` has two exceptions for a new `integration/*` branch that is absent on the remote. First, `/to-spec` may commit the grill docs to that branch. The grill docs are `CONTEXT.md`, `CONTEXT-MAP.md`, `**/CONTEXT.md`, `docs/adr/**`, and `**/docs/adr/**`. The commit must be a standalone `git commit` with only message options. The index must contain only grill docs. Second, `/to-spec` may push that branch to create it. For the commit, the hook checks `origin`; for the push, it checks the remote in the command. The hook blocks the command when the branch exists there or when it cannot check the remote.
* **CLI Only:** Rely exclusively on `git` and your tracker's CLI — GitHub CLI (`gh`) or GitLab CLI (`glab`) — for repository and task operations. On GitLab, name the project explicitly in every command: each `glab` command takes `-R <project-url>`, and each `glab api` call is written `GITLAB_HOST=<host> glab api projects/<project-id>/...`. `<host>`, `<project-url>` and `<project-id>` are defined in [issue-tracker.md](./issue-tracker.md) → GitLab → Conventions.
* **Body via File, Not Inline:** Every body or comment published through `gh`/`glab` MUST come from a file at a readable literal path — never an inline `--body "..."`, `--description "..."` or `--message "..."`, a shell heredoc, a shell variable, or `-` (stdin). Nested quotes, backticks, and PowerShell's escaping rules all break inline text unpredictably, and the public-metadata hook (`block-public-attribution.sh`) checks the file's content and rejects a shell-variable, stdin, or missing path. Both trackers follow one publication contract:
  * **GitHub:** issue, PR, and comment bodies go through `--body-file <path>` (`gh issue create`/`edit`, `gh pr create`/`edit`, `gh issue comment`, `gh pr comment`).
  * **GitLab:** MR and issue bodies go through `--description-file <path>` (`glab mr create`/`update`, `glab issue create`/`update`). Issue and MR comments go through the notes API: `GITLAB_HOST=<host> glab api projects/<project-id>/issues/<iid>/notes -F body=@<path>` and `GITLAB_HOST=<host> glab api projects/<project-id>/merge_requests/<iid>/notes -F body=@<path>`, because `glab issue note` and `glab mr note` take only an inline `--message` and cannot read a file.

  For an issue or spec, the path is the already-written draft file (see [issue-tracker.md](./issue-tracker.md)). For a PR/MR body or a comment (including a `task-report::required` completion report posted as an issue comment), use repository scratch only: `.harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md` for a PR/MR body, or `.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`/`pr-comment-<issue>-<slug>.md` for a comment — the enforcing hook (`block-scratch-outside-docs-tasks.sh`) checks the basename and only allows one containing `pr-body`, `pr-comment`, or `issue-comment`; any other name in that same directory is rejected. Run one publication command per shell call. Never `docs/tasks/`; delete the file only after the command succeeds, and retain it on failure for retry.
* **Project-Only Metadata:** Commit messages and PR/MR titles/bodies contain only the project change. Automated-agent attribution, model names, session URLs, and `Co-Authored-By` trailers are forbidden; the local hook (`block-public-attribution.sh`) rejects them. The harness installs no CI check in a target project, so a project that wants a server-side check adds its own. Issue titles and bodies and every issue, PR, or MR comment are tracker text: runtime names and paths such as `.claude/` or `CLAUDE.md` are ordinary project vocabulary there, so the local hook rejects only attribution in it — a `Co-Authored-By` trailer, a "generated/written by <assistant>" statement, an AI-generated mark, or an assistant session URL.
* **Issue First:** No development begins without a registered ticket. All implementation tasks MUST be created beforehand using `gh issue create` (`glab issue create -R <project-url>` on GitLab). For the local markdown tracker, creating the ticket file under `.scratch/<feature-slug>/issues/` satisfies this instead — see [issue-tracker.md](./issue-tracker.md). `/implement` runs the gated architect → developer → code-review → qa → publish pipeline and ends after the accepted candidate is published, then offers the separate manual `/to-pull-requests <ticket>` command for PR and ticket wrap-up; `/fast-implement` ends the same way after its own commit and push.
* **Zero Auto-Merge:** The agent must never merge a pull request itself (`gh pr merge`/`glab mr merge` or equivalent). Merging into `integration/*` or `base_branch` is exclusively a manual action performed by the developer, after they confirm in the Human QA & Merge step below. This is unconditional regardless of the ticket's `hitl`/`afk` execution mode (see `docs/agents/triage-labels.md`) — `afk` means an agent can implement the work unattended, never that it may ship unattended.
* **PR Confirmation Required:** The agent must not run `gh pr create`/`glab mr create` without first getting the developer's explicit go-ahead that the branch is ready to become a PR. This is a separate, earlier checkpoint than Human QA & Merge below (which covers review *after* the PR already exists) — finishing implementation, tests, and code-review does NOT by itself imply consent to open the PR. Like Zero Auto-Merge above, this gate does not relax for `afk`-labeled tickets.

### 2. Workflow Sequence

An epic selects one integration branch named `integration/<service-or-team>`. `/to-spec` creates it
from `origin/<base_branch>` after publishing the epic when it is absent, records it in the epic, and never switches
the current worktree. `/to-tickets` copies the exact value to every child ticket. The integration
branch is the PR target for child work; `base_branch` is the release target for a separate PR.

1. **Initialization (Branching):**
   An isolated issue branch is created for each task from the epic's exact integration branch — never from whatever branch happens to already be checked out. If the task has no epic, use `base_branch`.
   * **Format:** must match `branch_pattern` in `.harness/project.json` (default: `feature/issue-<ID>-<short-slug>`, where `<ID>` is the tracker issue number and `<short-slug>` is a short task description — transliterated, words separated by hyphens or underscores).
   * **Command:** `git fetch origin <integration-branch> && git switch -c feature/issue-<ID>-<slug> --track origin/<integration-branch>`. For an epic-less task, replace `<integration-branch>` with the required `base_branch` from `.harness/project.json`. A coordinator creates the branch with `git worktree add --no-track` instead (see [worktrees.md](./worktrees.md#coordinator-issue-worktree)); `git push -u` in step 2 then sets the upstream.
2. **Post-branch Push:**
   * Immediately after creating the branch, push it to the remote (`origin`) so it exists there: `git push -u origin feature/issue-<ID>-<slug>`.
3. **Implementation & Quality Assurance (TDD):**
   * Write code test-first (TDD) and run the local tests before every commit.
4. **Committing Changes:**
   * Commits are made only to the current issue branch. Before the first edit and before every commit, verify that the current branch matches `branch_pattern` and is not `base_branch` or `integration/*`.
   * Commit messages follow the **Semantic Commit Messages** standard (e.g., `feat: ...`, `fix: ...`, `refactor: ...`).
5. **Continuous Push:**
   * Push commits to the remote (`origin`) both while implementing the task and after addressing code-review feedback: `git push origin feature/issue-<ID>-<slug>`. Never leave finished commits sitting only in the local repo.
6. **Integration (Pull Request):** *(local markdown tracker: skip this step and step 7 — see "Issue First" above.)*
   * **Confirm before opening:** before creating the PR, explicitly ask the developer whether the branch is ready to be opened as a pull request. Do not run `gh pr create`/`glab mr create` just because implementation, tests, and code-review are done — wait for an explicit go-ahead. Silence, or the mere fact that the task is otherwise complete, does not count as consent.
   * Once the developer confirms, run the `qa-gate` skill (see [issue-tracker.md](./issue-tracker.md)'s "When a skill says…" conventions for how tickets are referenced) and only proceed once it passes.
   * If the PR body template in [§3](#3-pr-body-template) has more structure than a short summary, follow it directly. A developer may instead configure and run `pr-composer` manually in the coding application; give it `.harness/.sandboxes/pr_body/pr-body-<issue>-<slug>.md`, pass that path to `--body-file` (`--description-file` on GitLab) below, and delete it only after the PR/MR is created successfully.
   * PR/MR creation is performed via CLI from the issue branch checkout, with an explicit target branch — without one, both CLIs target the default branch. `<target-branch>` is the epic's integration branch, or `base_branch` for an epic-less ticket:
     * **GitHub:** `gh pr create --base <target-branch> --title '<title>' --body-file <path>`.
     * **GitLab:** `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file <path> --yes`.

     Keep `<title>` in single quotes and write each apostrophe in it as `'\''` in a POSIX shell or as `''` in PowerShell. The body comes from a file — see §1 ("Body via File, Not Inline"); never inline `--body` or `--description`. GitLab prints the MR URL `.../-/merge_requests/<iid>`: refer to that MR as `!<iid>` and to an issue as `#<iid>`, because GitLab numbers them separately. On GitHub both are `#<n>`.
   * **Ticket footer:** A child PR targets the epic's integration branch. Before writing its body, resolve the repository's default branch through the tracker CLI — GitHub: `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`; GitLab: the `default_branch` field of `GITLAB_HOST=<host> glab api projects/<project-id>`. Use `Closes #<ID>` only when the PR/MR target is that default branch; otherwise use `Related to #<ID>`. GitHub ignores closing keywords on every other target branch. GitLab closes an issue by a closing pattern (the default pattern accepts `Closes #<ID>`) only when the MR or commit lands in the project's default branch: an MR into `integration/*` or any other branch closes nothing, and `Related to #<ID>` is not a closing pattern. A project can turn this auto-close off with its "Auto-close referenced issues on default branch" setting, and only an administrator of a self-managed instance changes the pattern itself, so the ticket state is always checked after a default-branch merge (step 7). An integration-to-`base_branch` PR is a separate release action and does not replace child PRs.
   * **Mandatory Requirement:** The pull request body MUST follow the template in [§3 PR Body Template](#3-pr-body-template) below.
7. **Human QA & Merge:**
   * Once the PR/MR is open, return its link (on GitLab, also as `!<iid>`) and explicitly ask the developer whether they want to review the change themselves before it's considered ready — do not assume silence means approval.
   * **If the developer confirms:** tell them the PR is ready and stop there. Do not merge it — merging is always a manual action the developer performs themselves.
   * **If the developer requests changes or clarifications:** address them with new commits on the same branch (repeat steps 3–5: implement, commit, push), then ask again. Repeat until the developer confirms.
   * **Close the ticket after merge:** the only trigger is the developer's confirmation that the PR/MR was merged. The developer performs these steps when working alone; an agent may do them only after the developer confirms the merge in the active session. First check the merge — GitHub: `gh pr view <n> --json state,baseRefName` (`state` is `MERGED`, `baseRefName` is the expected target); GitLab: `glab mr view <iid> -R <project-url> -F json` (`state` is `merged`, `target_branch` is the expected target). If it is not merged into that target, leave the ticket open and tell the developer. Then:
     * **`Related to #<ID>`:** close the ticket explicitly — GitHub: `gh issue close <ID> --reason completed`; GitLab: `glab issue close <ID> -R <project-url>`. `glab issue close` takes no comment, so post any closing comment first through the notes API (§1).
     * **`Closes #<ID>`:** verify that the tracker closed the ticket — GitHub: `gh issue view <ID> --json state` (`state` is `CLOSED`); GitLab: `glab issue view <ID> -R <project-url> -F json` (`state` is `closed`). GitLab closes it asynchronously after the merge, so if it still reads `opened`, read it once more; if it is still open, close it with the explicit command above and tell the developer why the tracker did not.

     Do not close an issue when its PR/MR merely opens or closes without merging.

### 3. PR Body Template

Every PR body (or MR body) MUST start with the following HTML comment verbatim. The comment is the same for every project language. The tracker does not render it; it is a checklist for the author and a navigator for the reviewer. After the comment, write the seven sections for the actual change; the comment items map to the section headings in order. Pass the body through `--body-file` (`--description-file` on GitLab) as §1 requires.

```html
<!--
This commented block in the Pull Request body serves as a checklist for the author and a navigator for the reviewer. Each item should give a clear picture of the change's context.

1. Summary
A short summary of the end goal this pull request achieves. Reading only this point, the reviewer should understand the gist of the PR without diving into the code.

2. Affected parts of the project
The modules, architectural layers, services, or databases that were changed.

3. Business logic
Which business rules were added, changed, or removed. How the system should now behave from a business perspective.

4. What changed
A technical description of the implementation — patterns used, classes/interfaces added, signature changes.

5. Verification
How the functionality was tested (unit, integration, e2e, manual) — use the `qa-gate` result if one ran this session.

6. Not verified & risks
Edge cases not covered by tests, potential performance issues, remaining workarounds.

7. Integration
What needs to happen when deploying to other environments — DB migrations, new env vars, dependencies on other PRs.
-->
```

Read `language` from `.harness/project.json` (default `ru`) to pick the section headings. The headings are output text in the project language. Do not translate them.

**`language: ru`**:

```markdown
## Итог

## Затронутые части проекта

## Бизнес-логика

## Что изменено

## Проверка

## Не проверено и риски

## Интеграция

<!-- Выбрать ровно один footer: `Closes #<ID>` для PR в default branch; `Related to #<ID>` для любого другого target. -->
Related to #<ID>
```

**`language: en`**:

```markdown
## Summary

## Affected parts of the project

## Business logic

## What changed

## Verification

## Not verified & risks

## Integration

<!-- Choose exactly one footer: `Closes #<ID>` for a PR targeting the default branch; `Related to #<ID>` for every other target. -->
Related to #<ID>
```
