---
name: to-spec
description: Turn the current conversation into a spec and publish it to the project issue tracker. No interview, just synthesis of what you've already discussed.
disable-model-invocation: true
---

**Objective:** Synthesize the current conversation context and codebase understanding into a final specification.
**Strict Rule:** Do NOT interview the user or start a new grilling round. If information feels missing, it means the previous grilling was incomplete; synthesize the spec using only the known facts and explicit assumptions.

The issue tracker and triage label vocabulary should have been provided to you. If not, tell the user to run `/setup-matt-pocock-skills`.

## Execution in Two Phases

You must execute this skill in two distinct phases to ensure the user agrees with the testing strategy before you write the final document.

### Phase 1: Exploration & Seam Proposal
1. **Explore the Codebase:** Use the project's domain glossary (`CONTEXT.md`) and respect any ADRs in the touched area.
2. **Define Seams:** Sketch out the seams at which the feature will be tested.
    - Prefer existing seams over new ones.
    - Use the highest seam possible.
    - Ideal state: exactly *one* seam for the whole feature.
3. **STOP AND ASK:** Present your proposed seams to the user and explicitly ask for their approval. **Do not proceed to Phase 2 until the user confirms.**

### Phase 2: Drafting & Publishing (After User Approval)
1. **Draft the File:** Write the spec using the `<spec-template>` below. Save it first to `docs/tasks/`.
    - *Naming convention:* If the issue ID is known, use it. If not, use a descriptive slug (e.g., `docs/tasks/add-user-auth.md`) and rename the file later once the ID is generated.
2. **Publish to Tracker:** Publish the issue using the CLI: `gh issue create --body-file <path>`.
    - **CRITICAL:** Do NOT use an inline `--body` heredoc. Spec bodies contain characters (nested quotes, backticks, etc.) that break heredoc quoting. Always use `--body-file`.
3. **Apply Labels:** This published issue acts as the feature's **epic**. Apply the following labels:
    - `bug` OR `enhancement`
    - `workflow::specs` (Do NOT use `workflow::ready` as it requires decomposition first).
    - `task-report::required` (unless told to skip).
    - *Note:* Do NOT create ad-hoc `epic::<slug>` labels. `/to-tickets` will handle linking sub-tasks natively later.

---

<spec-template>

## Problem Statement
The problem that the user is facing, from the user's perspective.

## Solution
The solution to the problem, from the user's perspective.

## User Stories
A LONG, numbered list of user stories covering all aspects of the feature.
Format: `1. As an <actor>, I want a <feature>, so that <benefit>`
*(Example: As a bank customer, I want to see my balance, so that I can make informed spending decisions).*

## Implementation Decisions
A list of modules to build/modify, interface changes, technical clarifications, architectural decisions, schema changes, API contracts, and specific interactions.
- **DO NOT** include specific file paths or generic code snippets (they outdate quickly).
- **Exception:** If a prototype produced a snippet that encodes a decision perfectly (state machine, schema, type shape), inline it and note it came from a prototype. Trim it to the decision-rich parts only.

## Testing Decisions
- Description of what makes a good test here (test external behavior, not implementation details).
- Which modules will be tested.
- Prior art (similar existing tests in the codebase).

## Out of Scope
A strict list of things that will NOT be done. This is your insurance policy against over-engineering. Be explicit about boundaries so future agents do not build more than requested.

## Further Notes
Any remaining context or constraints.

</spec-template>
