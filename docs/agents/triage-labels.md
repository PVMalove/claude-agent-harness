# Triage Labels

This repo does **not** use the upstream `mattpocock/skills` canonical five-role vocabulary (`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`) as literal labels. `triage/SKILL.md` (`skills/first-party/pvmalove/triage/`) has been customized to speak natively in the namespaced taxonomy below — this file is the reference, not a translation table. The category pair (`bug`/`enhancement`) is the one axis left unchanged from upstream.

## The taxonomy

Every triaged issue or PR carries exactly one label from each of the first three axes below, except while it carries `workflow::specs` — execution mode isn't decided yet at that stage (see [State machine](#state-machine)).

### 1. Category — unchanged from upstream

| Label | Meaning |
| --- | --- |
| `bug` | Something is broken |
| `enhancement` | New feature or improvement |

### 2. Execution mode — who does the work

| Label | Color | Meaning |
| --- | --- | --- |
| `hitl` | yellow `#fbca04` | Human-in-the-loop — needs review, testing, or approval from you |
| `afk` | light blue `#54c1e8` | Away-from-keyboard — an agent can complete it alone |

### 3. Workflow state (`workflow::*`) — where it sits in the pipeline

| Label | Color | Meaning |
| --- | --- | --- |
| `workflow::specs` | purple `#5319e7` | Design/spec-writing: an epic or large task being turned into a spec and decomposed into tickets. Too early to take into development. |
| `workflow::ready` | green `#0e8a16` | Fully specified, ready to be picked up |
| `workflow::in-progress` | blue `#1d76db` | Currently being worked on in an active session |
| `workflow::blocked` | red `#b60205` | Blocked by a dependency, or waiting on more info from you |

### Context labels — applied when relevant

| Label | Color | Applied by | Meaning |
| --- | --- | --- | --- |
| `task-report::required` | gray `#6a737d` | `/to-spec`, `/to-tickets` — acted on by `/implement` | Agent must post a completion report before closing. Applied by default to every ticket unless you say to skip it. Not part of `triage`'s own state machine — see [implement's SKILL.md](../../skills/first-party/pvmalove/implement/SKILL.md). |
| `out-of-scope` | gray `#c2c2c2` | `/triage` | This project's `wontfix` — the request was explicitly rejected. Applied at close time; see `.out-of-scope/` handling in `triage/OUT-OF-SCOPE.md`. |

Epic grouping no longer uses a label. A ticket decomposed from an epic is linked to it as a native GitHub **sub-issue**, the same mechanism `/wayfinder` uses for its map/ticket relationship — see [issue-tracker.md](./issue-tracker.md#wayfinding-operations). `wayfinder:map` and `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`) remain `/wayfinder`'s own separate namespace — a Wayfinder ticket also carries its own `hitl`/`afk` + `workflow::*` labels, but `workflow::specs` and `wayfinder:map` are not the same thing: the former is a triage state on an epic issue, the latter is Wayfinder's own map artifact.

## State machine

An unlabeled issue is implicitly "needs triage" — there's no dedicated label for that state.

1. Triage analyzes the issue: determine `bug`/`enhancement`, and either `hitl`/`afk` or — if the issue is epic-sized and needs decomposition before anything is actionable — route it to `workflow::specs` instead and point the maintainer at `/to-spec`.
2. `/to-spec` applies `workflow::specs` when it publishes a fresh epic issue directly (skipping step 1's routing when the maintainer starts from `/to-spec` rather than from an inbound issue).
3. Once specified, place the ticket in `workflow::ready` (nothing blocking it) or `workflow::blocked` (a dependency, or missing info from you — either way, post triage notes).
4. `workflow::blocked` → `workflow::ready` once the blocker clears or you reply.
5. `workflow::ready` → `workflow::in-progress` when a session (agent or you) picks it up — `/implement` sets this.
6. Rejected at any point → apply `out-of-scope`, drop the `workflow::*` label, close.

Apply `task-report::required` by default when `/to-spec` or `/to-tickets` create a ticket, unless told to skip it.

## Local markdown tracker

A local-markdown-tracked ticket (`.scratch/<feature>/issues/NN-*.md`) has no GitHub/GitLab labels — the axes above go in explicit fields in that file instead:

```markdown
**Category:** bug / enhancement
**Workflow:** workflow::specs / workflow::ready / workflow::in-progress / workflow::blocked / done
**Execution:** hitl / afk (omit while **Workflow:** is workflow::specs)
**Task report:** required (omit the line entirely if not required)
```

`workflow::*`'s values are the same strings as the GitHub labels, plus a terminal `done` — `/implement` sets `**Workflow:** done` when it finishes a ticket, mirroring the `Closes #<ID>` auto-close a GitHub/GitLab ticket gets on merge (see `docs/agents/git-workflow.md`). There's no sub-issue mechanism for the local tracker; a decomposed ticket instead lives under the epic's `docs/tasks/issue-<epic-id>-<epic-slug>/` folder per `docs/agents/artifacts.md`'s grouping convention — the folder itself is the grouping, no label or field needed for it.

## Adapting this taxonomy per project

This is the taxonomy `pvmalove-suite` ships by default — it's a design choice (namespaced multi-axis labels), not a hardcoded requirement. Edit this file directly to rename axes or add project-specific context labels; `triage/SKILL.md` speaks the vocabulary documented here.
