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

Epic grouping no longer uses a label. A ticket decomposed from an epic is linked to it as a native GitHub **sub-issue**, the same mechanism `/wayfinder` uses for its map/ticket relationship — see [issue-tracker.md](./issue-tracker.md#wayfinding-operations). `wayfinder:map` and `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`) remain `/wayfinder`'s own separate namespace, colors below — but a Wayfinder ticket (not the map itself) also carries the matching `hitl`/`afk` label plus `workflow::ready`, moved to `workflow::in-progress` on claim: the same two axes as everywhere else in this taxonomy, applied by `/wayfinder` itself rather than by `/triage`. `workflow::specs` and `wayfinder:map` are not the same thing: the former is a triage state on an epic issue, the latter is Wayfinder's own map artifact — the map never carries a `workflow::*` label.

| Label | Color | Meaning |
| --- | --- | --- |
| `wayfinder:map` | dark blue `#0052cc` | The map issue itself — Destination, Notes, Decisions-so-far, the fog. |
| `wayfinder:research` | green `#00875a` | AFK ticket type — reading docs/APIs/local resources to surface a fact. |
| `wayfinder:prototype` | orange `#ff8b00` | HITL ticket type — a cheap concrete artifact to react to. |
| `wayfinder:grilling` | violet `#6554c0` | HITL ticket type — a conversation, the default case. |
| `wayfinder:task` | slate `#8993a4` | HITL-or-AFK ticket type — manual work that unblocks a decision. |

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

This four-field block is the vocabulary that [issue-tracker.md](./issue-tracker.md)'s "a `Status:` line" refers to — a naming difference between the two docs, not a second schema. Wayfinder's own `Status: claimed/resolved` line (same source doc) is an orthogonal claim/lock marker for the file and coexists with these fields rather than replacing them.

`workflow::*`'s values are the same strings as the GitHub labels, plus a terminal `done` — `/implement` sets `**Workflow:** done` when it finishes a local-tracker ticket. Remote tickets close only after their change is actually merged: GitHub auto-closes `Closes #<ID>` for a default-branch PR, while GitLab's pattern can be disabled or customized; verify either result. An integration-branch PR is closed explicitly after its confirmed merge (see `docs/agents/git-workflow.md`). There's no sub-issue mechanism for the local tracker; a decomposed ticket instead lives under the feature's `.scratch/<feature-slug>/issues/` directory (see [issue-tracker.md](./issue-tracker.md)) — that directory itself is the grouping, no separate epic-folder or field needed for it.

## Adapting this taxonomy per project

This is the taxonomy `pvmalove-suite` ships by default — it's a design choice (namespaced multi-axis labels), not a hardcoded requirement. Renaming an axis or adding project-specific context labels means editing this file **and** `triage/SKILL.md` — the skill speaks the vocabulary documented here as hardcoded prose, not as data read from this file at runtime. Run `harness lock-project-skills` afterward so the customization is recorded as intentional provenance, rather than left as drift a future `harness update --force` could silently overwrite.
