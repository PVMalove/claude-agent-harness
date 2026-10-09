# Issue tracker

The delivery workflow is in [git-workflow.md](./git-workflow.md). This guide defines tracker-specific operations.

Detect which section below applies from the project tracker, the same resolution `check-branch-name.sh` uses: the `tracker` field of `.harness/project.json` when it is set; otherwise `git remote -v` — a `github.com` remote → GitHub; a `gitlab.`-hosted remote → GitLab; anything else, including no remote at all, → Local markdown. For a different tracker entirely (Jira, Linear, ...), replace this file's content with a description of that workflow instead — see `/setup-matt-pocock-skills`.

This repo's triage label vocabulary is a first-party namespaced taxonomy — `type::*` category, `hitl`/`afk` execution mode, `status::*` pipeline state, optional `priority::*`/`severity::*` context, plus the `task-report::required`/`resolution::wontfix` context labels — see [triage-labels.md](./triage-labels.md) before applying or querying labels, whichever section below applies.

## GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

### Conventions

- **Create an issue**: `gh issue create --title "..." --body-file <path>`. Write the body to a file first — inline `--body` heredocs break on nested quotes/backticks (see `/to-spec`, `/to-tickets`).
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body-file <path>`, with the path from [git-workflow.md](./git-workflow.md) §1 (`.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`)
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: post the explanation first with `gh issue comment <number> --body-file <path>`, then run `gh issue close <number>`; never an inline `--comment "..."`.

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

### Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment <number> --body-file <path>`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

### When a skill says "publish to the issue tracker"

Create a GitHub issue.

### When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## GitLab

Issues and specs for this repo live as GitLab issues. Use the [`glab`](https://gitlab.com/gitlab-org/cli) CLI for all operations.

### Conventions

- **Project addressing**: name the project explicitly in every command instead of letting `glab` choose it. On its own, `glab` picks a remote in the order `upstream` > `gitlab` > `origin`, and `glab api` fills its project placeholders from the current directory's repository and defaults `--hostname` to `gitlab.com` or that repository's host. This guide and the skills write the project with three placeholders:
  - `<host>` — `tracker.host` from `.harness/project.json`, including a web port other than 443 (`gitlab.example.com`, `gitlab.example.test:4443`);
  - `<project-url>` — `https://<host>/<tracker.project>` (`https://gitlab.example.com/group/project`); every `glab` command takes `-R <project-url>`;
  - `<project-id>` — `tracker.project` URL-encoded (`group%2Fproject`, `group%2Fsub%2Fproject`); every `glab api` call is written `GITLAB_HOST=<host> glab api projects/<project-id>/...`. `GITLAB_HOST` keeps the port and wins over the current directory's repository, while `glab api --hostname` rejects a host with a port (`invalid hostname`) and sends a host without one to port 443. In PowerShell, run `$env:GITLAB_HOST = '<host>'` before the call and `Remove-Item Env:GITLAB_HOST` after it.

  Without a `tracker` field, use the host and project that `harness health` resolves from `origin`; its `tracker.project` warning prints a ready-to-paste `tracker` snippet.
- **Create an issue**: `glab issue create -R <project-url> --title "..." --description-file <path>`, with the already-written draft file as `<path>` (per [git-workflow.md](./git-workflow.md) §1, "Body via File, Not Inline"); never an inline `--description "..."` or a heredoc.
- **Read an issue**: `glab issue view <number> -R <project-url> --comments`. Use `-F json` for machine-readable output.
- **List issues**: `glab issue list -R <project-url> --output json` with appropriate `--label` filters. In `glab issue list`, `-F` is `--output-format` (`details`/`ids`/`urls`), not JSON.
- **Comment on an issue**: `GITLAB_HOST=<host> glab api projects/<project-id>/issues/<number>/notes -F body=@<path>`, with the path from [git-workflow.md](./git-workflow.md) §1 (`.harness/.sandboxes/pr_body/issue-comment-<issue>-<slug>.md`). GitLab calls comments "notes". The `note` subcommand of `glab issue` takes only an inline `--message` and cannot read a file, so it is not used for a comment body.
- **Apply / remove labels**: `glab issue update <number> -R <project-url> --label "..."` / `--unlabel "..."`. Multiple labels can be comma-separated or by repeating the flag. Unlike `gh`, a `--label` that names a label missing from the project doesn't fail: GitLab creates it with its default color, so run `/setup-labels` before the first labelled write.
- **Close**: `glab issue close <number> -R <project-url>`. `glab issue close` does not accept a closing comment, so post the explanation first as a comment (see above), then close.
- **Merge requests**: GitLab calls PRs "merge requests". Use `glab mr create -R <project-url> --target-branch <target-branch> --title '<title>' --description-file <path> --yes` (target branch and title rules in [git-workflow.md](./git-workflow.md) §2), `glab mr update`, `glab mr view`, etc. — the same shape as `gh pr ...` with `mr` in place of `pr`, `--target-branch` in place of `--base`, `--description-file` in place of `--body-file`, and `-R <project-url>` on every command. An MR comment goes through `GITLAB_HOST=<host> glab api projects/<project-id>/merge_requests/<number>/notes -F body=@<path>` (`pr-comment-<issue>-<slug>.md`), not `glab mr note --message`.

### `glab` setup

- **Minimum version: `glab` 1.117.0.** When the project tracker is GitLab, `harness health` fails its local `environment.glab` check for an older `glab --version`; upgrade by the [installation guide](https://gitlab.com/gitlab-org/cli#installation).
- **Host with a port.** The tracker host is `tracker.host` in `.harness/project.json`, including the web port when it is not 443 — for example `gitlab.example.test:4443`. Log in to exactly that host with `glab auth login --hostname gitlab.example.test:4443` and check it with `glab auth status --hostname gitlab.example.test:4443`, the same check `harness health --online` runs. Address the project explicitly: `glab <command> -R https://gitlab.example.test:4443/group/sub/project`, and `GITLAB_HOST=gitlab.example.test:4443 glab api projects/group%2Fsub%2Fproject` with the URL-encoded project path.
- **Personal CA and proxy.** Keep them in your own `glab` configuration and environment, never in `.harness/project.json` or the repository: `glab config set ca_cert /path/to/ca.pem --host gitlab.example.test:4443` for an internal CA, and the standard `HTTPS_PROXY`/`NO_PROXY` environment variables for a proxy. Tokens stay in the storage `glab auth login` uses.

### Merge requests as a triage surface

**MRs as a request surface: no.** _(Set to `yes` if this repo treats external merge requests as feature requests; `/triage` reads this flag.)_

When set to `yes`, MRs run through the same labels and states as issues, using the `glab mr` equivalents:

- **Read an MR**: `glab mr view <number> -R <project-url> --comments` and `glab mr diff <number> -R <project-url>` for the diff.
- **List external MRs for triage**: `glab mr list -R <project-url> -F json`, then keep only MRs whose author is not a project member/owner (a contributor's MR, not a maintainer's in-flight work).
- **Comment / label / close**: `GITLAB_HOST=<host> glab api projects/<project-id>/merge_requests/<number>/notes -F body=@<path>`, `glab mr update <number> -R <project-url> --label`/`--unlabel`, `glab mr close <number> -R <project-url>`.

Unlike GitHub, GitLab numbers issues and MRs separately, so `#42` is unambiguous once you know which surface the maintainer means.

### When a skill says "publish to the issue tracker"

Create a GitLab issue.

### When a skill says "fetch the relevant ticket"

Run `glab issue view <number> -R <project-url> --comments`.

## Local markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

### Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` — never a single combined tickets file
- Triage state is recorded as a `Workflow:` line near the top of each issue file, using the `status::*` vocabulary (see [triage-labels.md](./triage-labels.md) for the exact strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

### When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

### When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** holds the Notes / Decisions-so-far / Fog body; **child** tickets hang off it. Mechanics depend on the tracker section above:

**GitHub:**

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint) — the same mechanism `/to-tickets` uses to link tickets to an epic (see [triage-labels.md](./triage-labels.md)). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`) — a separate namespace from this repo's triage labels, don't conflate them. If the map represents a larger epic that other, non-Wayfinder tickets also belong to, link those tickets as sub-issues of the epic issue the same way `/to-tickets` does, rather than a `wayfinder:*` label. Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed (a blocker closes after its merge is confirmed, never on push, publish, accepted QA or an open PR).
- **Frontier query**: list the parent issue's open children (`gh issue list --state open`, scoped to its sub-issues / task list — the map for `/wayfinder`, the epic for `/to-tickets`), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in parent order wins. `/fast-implement`, handed an epic reference instead of a specific ticket, runs this same query and keeps only `afk` + `pipeline::fast` tickets, never a `hitl` one (see [triage-labels.md](./triage-labels.md)) — that execution mode routes through `/to-guide`, not this auto-pick. A `pipeline::fast`+`afk` ticket named explicitly is still accepted by `/implement` and run through the full gated cycle without refusing — the full cycle is always a safe fallback; the label only narrows auto-pick, never a named ticket.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write, before any other write. Prevents two concurrent sessions (e.g. parallel worktrees) from picking the same frontier ticket.
- **Resolve**: `gh issue comment <n> --body-file <path>` (path per git-workflow.md §1), then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

**GitLab:**

Everything below runs on GitLab Free. Epics, the `blocks`/`is_blocked_by` issue links and the blocking quick actions are Premium features and are not used. The hierarchy lives in the child's description plus one `relates_to` issue link. `<host>`, `<project-url>` and `<project-id>` are the placeholders from [GitLab](#gitlab) → Conventions.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `glab issue create -R <project-url> --label wayfinder:map`.
- **Child ticket**: an issue whose description opens with a `## Parent: #<parent>` section — the map for `/wayfinder`, the epic for `/to-tickets` — plus exactly one `relates_to` link from the child to the parent: `GITLAB_HOST=<host> glab api --method POST projects/<project-id>/issues/<child>/links -F target_project_id=<numeric-project-id> -F target_issue_iid=<parent> -F link_type=relates_to`. `<numeric-project-id>` is the `id` field of `GITLAB_HOST=<host> glab api projects/<project-id>`; read it once and reuse it for every child. The link lists the child on the parent's page but has no direction, so the `## Parent` line records which side is the parent. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: a `## Blocked by` section in the child's description with one `Blocked by #<M>` line per blocker, or `None — can start immediately`. No link is created between a ticket and its blockers. A ticket is unblocked when every blocker is closed (a blocker closes after its merge is confirmed, never on push, publish, accepted QA or an open PR).
- **Frontier query**: list the parent's links with `GITLAB_HOST=<host> glab api --paginate projects/<project-id>/issues/<parent>/links`. Keep the linked issues whose `project_id` is `<numeric-project-id>` (a link may point to an issue in another project), whose `state` is `opened`, that have no assignee, and whose description carries `## Parent: #<parent>` (read it with `glab issue view <iid> -R <project-url> -F json`) — this drops unrelated `relates_to` links. Drop any ticket with a `#<M>` in its `Blocked by` lines that is still open (`state` from `glab issue view <M> -R <project-url> -F json`); an older single `Blocked by: #<n>, #<n>` line reads the same way. The lowest iid wins, because `/to-tickets` publishes tickets in dependency order. `/fast-implement`, handed an epic, runs this query and keeps only `afk` + `pipeline::fast` tickets, never a `hitl` one (see [triage-labels.md](./triage-labels.md)).
- **Claim**: `glab issue update <n> -R <project-url> --assignee @me` — the session's first write.
- **Resolve**: `GITLAB_HOST=<host> glab api projects/<project-id>/issues/<n>/notes -F body=@<path>` (path per git-workflow.md §1), then `glab issue close <n> -R <project-url>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

**Local markdown:**

- **Map**: `.scratch/<effort>/map.md`.
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved` (set after the predecessor's merge is confirmed).
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
