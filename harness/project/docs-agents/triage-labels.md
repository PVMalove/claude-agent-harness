# Triage Labels

The system overview and its interactive workflow are described in
[current-state.md](./current-state.md). This guide is the authoritative vocabulary for triage labels.

This repo does **not** use the upstream `mattpocock/skills` canonical five-role vocabulary (`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`) as literal labels. `triage/SKILL.md` (`skills/first-party/pvmalove/triage/`) has been customized to speak natively in the namespaced taxonomy below — this file is the reference, not a translation table. Every axis, including `type::*`, uses this repo's own enterprise-style values rather than upstream's.

## The taxonomy

Every triaged issue or PR carries exactly one label from each of the first three axes below, except while it carries `status::specs` — execution mode isn't decided yet at that stage (see [State machine](#state-machine)).

### 1. Type (`type::*`) — richer than upstream's `bug`/`enhancement` pair

| Label | Meaning |
| --- | --- |
| `type::bug` | Something is broken |
| `type::feature` | New feature or improvement |
| `type::refactoring` | Internal restructuring, no behavior change |
| `type::chore` | Maintenance, tooling, or dependency upkeep |
| `type::security` | Security fix or hardening |
| `type::performance` | Performance improvement |
| `type::hotfix` | Urgent production fix |

### 2. Execution mode — who does the work

| Label | Color | Meaning |
| --- | --- | --- |
| `hitl` | yellow `#fbca04` | Human-in-the-loop — needs review, testing, or approval from you |
| `afk` | light blue `#54c1e8` | Away-from-keyboard — an agent can complete it alone |

### 3. Status (`status::*`) — where it sits in the pipeline

| Label | Color | Meaning |
| --- | --- | --- |
| `status::specs` | purple `#5319e7` | Design/spec-writing: an epic or large task being turned into a spec and decomposed into tickets. Too early to take into development. |
| `status::ready` | green `#0e8a16` | Fully specified, ready to be picked up |
| `status::in-progress` | blue `#1d76db` | Currently being worked on in an active session |
| `status::blocked` | red `#b60205` | Blocked by a dependency, or waiting on more info from you |

### Context labels — applied when relevant

| Label | Color | Applied by | Meaning |
| --- | --- | --- | --- |
| `task-report::required` | gray `#6a737d` | `/to-spec`, `/to-tickets` — acted on by `/implement` | Agent must post a completion report before closing. Applied by default to every ticket unless you say to skip it. Not part of `triage`'s own state machine — see [implement's SKILL.md](../../skills/first-party/pvmalove/implement/SKILL.md). |
| `resolution::wontfix` | gray `#c2c2c2` | `/triage` | The request was explicitly rejected. Applied at close time; see `.out-of-scope/` handling in `triage/OUT-OF-SCOPE.md`. |

### 4. Priority (`priority::*`) — optional, purely informational

Nothing consumes this automatically yet; it exists so a maintainer can flag urgency.

| Label | Meaning |
| --- | --- |
| `priority::p0` | Critical — drop everything |
| `priority::p1` | High |
| `priority::p2` | Medium |
| `priority::p3` | Low |

### 5. Severity (`severity::*`) — optional, purely informational

Nothing consumes this automatically yet; it exists so a maintainer can flag impact.

| Label | Meaning |
| --- | --- |
| `severity::s1` | Critical impact |
| `severity::s2` | Major impact |
| `severity::s3` | Minor impact |
| `severity::s4` | Trivial impact |

### 6. Environment (`env::*`) — target-project only, optional

Which deployed environment an issue or PR relates to. Not part of this harness repository's own
taxonomy — only seeded into target projects that track deployments.

| Label | Meaning |
| --- | --- |
| `env::prod` | Production |
| `env::staging` | Staging |
| `env::qa` | QA |
| `env::dev` | Development |

Epic grouping no longer uses a label. A ticket decomposed from an epic is linked to it as a native GitHub **sub-issue**, the same mechanism `/wayfinder` uses for its map/ticket relationship — see [issue-tracker.md](./issue-tracker.md#wayfinding-operations). `wayfinder:map` and `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`) remain `/wayfinder`'s own separate namespace, colors below — but a Wayfinder ticket (not the map itself) also carries the matching `hitl`/`afk` label plus `status::ready`, moved to `status::in-progress` on claim: the same two axes as everywhere else in this taxonomy, applied by `/wayfinder` itself rather than by `/triage`. `status::specs` and `wayfinder:map` are not the same thing: the former is a triage state on an epic issue, the latter is Wayfinder's own map artifact — the map never carries a `status::*` label.

| Label | Color | Meaning |
| --- | --- | --- |
| `wayfinder:map` | dark blue `#0052cc` | The map issue itself — Destination, Notes, Decisions-so-far, the fog. |
| `wayfinder:research` | green `#00875a` | AFK ticket type — reading docs/APIs/local resources to surface a fact. |
| `wayfinder:prototype` | orange `#ff8b00` | HITL ticket type — a cheap concrete artifact to react to. |
| `wayfinder:grilling` | violet `#6554c0` | HITL ticket type — a conversation, the default case. |
| `wayfinder:task` | slate `#8993a4` | HITL-or-AFK ticket type — manual work that unblocks a decision. |

### Pipeline hint (`pipeline::*`) — from story points, `afk` only

A separate namespace, independent of `hitl`/`afk` and `status::*`. `/to-tickets` assigns every `afk` ticket a Fibonacci story-point score (Planning Poker: one primary pass, plus a second cheap advisory pass when the score lands in the gray zone) and derives one of these labels from it on the STOP-AND-ASK gate, where a developer can override the label without changing the score. `hitl` tickets never receive a `pipeline::*` label. The scale and thresholds (default: `≤3` fast, `≥5` full, gray zone `4`) live in `harness/project/project.schema.json`'s optional `story_points` field, overridable per project in `.harness/project.json`.

| Label | Color | Meaning |
| --- | --- | --- |
| `pipeline::fast` | pale blue `#c5def5` | Score `≤ fast_threshold` and `Execution: afk` — the short `/fast-implement` path, no gates. |
| `pipeline::full` | orange-red `#d93f0b` | Score `≥ full_threshold` — the gated `/implement` pipeline. |

## State machine

An unlabeled issue is implicitly "needs triage" — there's no dedicated label for that state.

1. Triage analyzes the issue: determine `type::*`, and either `hitl`/`afk` or — if the issue is epic-sized and needs decomposition before anything is actionable — route it to `status::specs` instead and point the maintainer at `/to-spec`.
2. `/to-spec` applies `status::specs` when it publishes a fresh epic issue directly (skipping step 1's routing when the maintainer starts from `/to-spec` rather than from an inbound issue).
3. Once specified, place the ticket in `status::ready` (nothing blocking it) or `status::blocked` (a dependency, or missing info from you — either way, post triage notes).
4. `status::blocked` → `status::ready` once the blocker clears or you reply.
5. `status::ready` → `status::in-progress` when a session (agent or you) picks it up — `/implement` sets this.
6. Rejected at any point → apply `resolution::wontfix`, drop the `status::*` label, close.

Apply `task-report::required` by default when `/to-spec` or `/to-tickets` create a ticket, unless told to skip it.

## Local markdown tracker

A local-markdown-tracked ticket (`.scratch/<feature>/issues/NN-*.md`) has no GitHub/GitLab labels — the axes above go in explicit fields in that file instead:

```markdown
**Category:** type::bug / type::feature / type::refactoring / type::chore / type::security / type::performance / type::hotfix
**Workflow:** status::specs / status::ready / status::in-progress / status::blocked / done
**Execution:** hitl / afk (omit while **Workflow:** is status::specs)
**Story Points:** Fibonacci score (afk tickets only; omit the line entirely for hitl tickets)
**Pipeline:** pipeline::fast / pipeline::full (afk tickets only, derived from Story Points; omit the line entirely for hitl tickets)
**Task report:** required (omit the line entirely if not required)
```

This four-field block is the vocabulary that [issue-tracker.md](./issue-tracker.md)'s "a `Status:` line" refers to — a naming difference between the two docs, not a second schema. Wayfinder's own `Status: claimed/resolved` line (same source doc) is an orthogonal claim/lock marker for the file and coexists with these fields rather than replacing them.

`status::*`'s values are the same strings as the GitHub labels, plus a terminal `done`, which `/to-pull-requests` sets after the developer confirms the separate PR/merge workflow. `/implement` ends at commit and push, then offers `/to-pull-requests`. Remote tickets close only after their change is actually merged: GitHub auto-closes `Closes #<ID>` for a default-branch PR, while GitLab's pattern can be disabled or customized; verify either result. An integration-branch PR is closed explicitly after its confirmed merge (see `docs/agents/git-workflow.md`). There's no sub-issue mechanism for the local tracker; a decomposed ticket instead lives under the feature's `.scratch/<feature-slug>/issues/` directory (see [issue-tracker.md](./issue-tracker.md)) — that directory itself is the grouping, no separate epic-folder or field needed for it.

## Adapting this taxonomy per project

This is the taxonomy `pvmalove-suite` ships by default — it's a design choice (namespaced multi-axis labels), not a hardcoded requirement. Renaming an axis or adding project-specific context labels means editing this file **and** `triage/SKILL.md` — the skill speaks the vocabulary documented here as hardcoded prose, not as data read from this file at runtime. Run `harness lock-project-skills` afterward so the customization is recorded as intentional provenance, rather than left as drift a future `harness update --force` could silently overwrite.
