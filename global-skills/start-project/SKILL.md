---
name: start-project
description: Design, initialize, inspect, or evolve any project and its self-contained agent harness. Use when the user has a new idea without a repository, wants to create a software, content, research, operations, personal, or other project, or asks to add or update a project harness.
---

# Start Project

Own the path from an unshaped idea to a portable project. A repository, stack, tracker, and harness
are outputs of project design, not prerequisites.

Resolve this skill's real path. The public `agent-harness` root is two levels above it. Its vendored
workflows are a library: read only the exact `SKILL.md` needed for the current phase and follow it
without installing the whole library globally. Use `skills/REGISTRY.md` to locate a named workflow.

## Shape

1. Establish whether the user has an idea, a seed directory, or an existing repository. Inspect
   existing files before asking questions.
2. Classify the project's primary work as software, content, research/knowledge, operations,
   personal, or another explicit domain. Classification selects workflows; it does not constrain
   what the project may become.
3. Resolve only decisions that change the next artifact. For an unshaped idea, read and follow
   `skills/vendor/mattpocock/productivity/grilling/SKILL.md`. Use the same clean library on demand
   for research, questionnaire, prototype, domain-modeling, or wayfinding when their descriptions
   match. Keep repo-dependent workflows such as `to-spec` and `wayfinder` until a repository and
   tracker exist.
4. Keep the discussion in the session while it is disposable. When the first durable project fact
   is ready, offer one recommended next artifact: a brief, a seed repository, or a harness. Ask for
   a location only when it cannot be derived safely.

## Seed

Create a minimal Git repository before choosing a stack when durable collaboration is useful. A
seed may contain only a short project brief and decision notes. Preserve an existing repository's
layout and instructions. Do not scaffold application code, a tracker, or a harness until the
project's current goals require them.

## Assemble

Build a harness only after the project has stable work to support, or immediately when the user
explicitly requests one.

1. Derive purpose, project type, stage, base branch, tracker, recurring activities, commands,
   boundaries, delivery policy, and Definition of Done from the repository and conversation. Mark
   inapplicable commands and tracker as `N/A`; do not force software fields onto other projects.
2. Select the smallest coherent public capability:
   - `project-foundation` for general project thinking, research, handoff, domain language, and
     agent-facing instructions;
   - `mattpocock-suite` for recurring software engineering work;
   - no additional package merely because it is available.
3. When the user asks `start-project` to select suitable recurring workflows, treat that request as
   authorization to consult any installed personal or organization overlay for catalog metadata
   only; do not require a second skill invocation. Do not open personal knowledge,
   tasks, notes, or secrets as part of catalog selection. Ask only when including a private package
   in a public target creates a real provenance or licensing decision.
4. For a new harness run:

   ```bash
   python3 <agent-harness-root>/harness/bin/harness init <repo> \
     --project-type <type> \
     --capability <selected-capability> \
     --base-branch <branch>
   ```

   On Windows/PowerShell there is no `python3` (use `python`), and line
   continuation is `` ` ``, not `\`.

   Fill every unresolved `{{...}}` in `AGENTS.md`. Add project-only packages under
   `.harness/skills`; keep their provenance and hashes in a versioned overlay lock. Use relative
   discovery links, never links back to a machine-local catalog.
5. For an existing harness run `harness diff` before `harness update`. Use `harness adopt
   --replace-conflicts` only for an explicit migration after reviewing same-name conflicts.

## Finish

Run `harness health`, inspect both discovery links, check unique frontmatter names and unresolved
template markers, and open one installed skill through the runtime's native mechanism. If the
current session does not rescan project skills, name a new session as the remaining activation
step. Report the project type, installed capabilities, excluded surfaces, and next useful action.
