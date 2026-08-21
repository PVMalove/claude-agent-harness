---
name: start-project
description: Start a new project from an idea or create, audit, or update its self-contained agent harness. Use when the user wants to shape or initialize a software, content, research, operations, personal, or other project, decide when it needs a repository, or install or revise a project harness. Ordinary work inside an already-harnessed project does not invoke this skill; a repository with substantial existing code and conventions but no harness yet is better served by `integrate-project`'s deeper audit.
---

# Start Project

Own the path from an unshaped idea to a portable project. A repository, stack, tracker, and harness
are outputs of project design, not prerequisites. Keep each phase read-only until the owner confirms
its proposed durable artifact.

Resolve this skill's real path and verify that the public `agent-harness` root two levels above it
contains `skills/REGISTRY.md` and `harness/bin/harness`. Its vendored workflows are a library: use
the registry to locate and read only the exact `SKILL.md` needed for the current phase. This
resolution is complete only when both files exist; otherwise report that the public package is
incomplete instead of guessing another installation.

## Shape

1. Establish whether the user has an idea, a seed directory, or an existing repository. Inspect
   existing files before asking questions. A repository with substantial existing code and
   conventions but no harness yet is `integrate-project`'s dedicated audit, not this generic path —
   hand off to it rather than deriving facts thinly here.
2. Classify the project's primary work as software, content, research/knowledge, operations,
   personal, or another explicit domain. Classification selects workflows; it does not constrain
   what the project may become.
3. Resolve the decisions that change the next artifact. For an unshaped idea, read and follow
   `skills/vendor/mattpocock/productivity/grilling/SKILL.md`. Use `research`, `to-questionnaire`,
   `prototype`, or `domain-modeling` only when its registry description matches the current branch.
   Tracker-dependent workflows such as `to-spec` and `wayfinder` begin after a repository and its
   tracker contract exist.
4. Keep disposable thinking in the session. Once purpose, intended outcome, recurring work, and
   the next useful artifact are known, recommend exactly one of: continue the conversation, write a
   brief, create a seed repository, or assemble a harness.

Shape is complete only when the owner confirms that recommendation or explicitly keeps the work in
the conversation. Confirmation authorizes that one artifact, not later scaffolding.

## Seed

After the owner confirms a seed, create or reuse the named Git repository and write only the
confirmed brief and decision notes. Preserve an existing repository's layout and instructions.
Record unresolved stack, tracker, and harness decisions as unresolved rather than choosing them to
make the seed look complete.

Seed is complete when the repository path is verified, durable files contain only confirmed facts,
`git status` has been inspected, and the owner can see which decisions remain open. Application
code, tracker setup, and a harness require their own confirmed next artifact.

## Assemble

Build a harness only after the project has stable work to support, or immediately when the user
explicitly requests one.

1. Derive purpose, project type, stage, base branch, tracker, recurring activities, commands,
   boundaries, delivery policy, and Definition of Done from the repository and conversation. Mark
   inapplicable commands and tracker as `N/A`.
2. Select the smallest coherent public capability:
   - `project-foundation` for general project thinking, research, handoff, domain language, and
     agent-facing instructions;
   - `mattpocock-suite` for recurring software engineering work;
   - no additional package merely because it is available.
3. When the owner asks `start-project` to select recurring workflows, allow a matching installed
   personal or organization catalog-entry skill to return catalog metadata. Pass only the project
   facts and recurring activities, and follow that entry skill's access boundary. Catalog selection
   does not authorize unrelated personal knowledge, tasks, notes, or secrets.
4. Present one harness manifest containing the project facts, public capability, overlay packages,
   project-only packages, integrations, boundaries, and files that will change. Resolve same-name,
   provenance, and licensing conflicts, then obtain owner confirmation for this manifest.
5. For a confirmed new harness run the resolved package command, replacing the example values with
   the confirmed manifest:

   ```bash
   python3 "<resolved-agent-harness-root>/harness/bin/harness" init "<repo>" \
     --project-type <type> \
     --capability <selected-capability> \
     --base-branch <branch>
   ```

   On Windows/PowerShell there is no `python3` (use `python`), and line
   continuation is `` ` ``, not `\`.

   Fill every unresolved `{{...}}` in `AGENTS.md`. Add project-only packages under
   `.harness/skills`; run `harness lock-project-skills` for their versioned hash lock, then
   `harness registry` after composing all packages. Use relative discovery links, never links back
   to a machine-local catalog.
6. For an existing harness run `harness diff` before `harness update`. Use `harness adopt
   --replace-conflicts` only for an explicit migration after reviewing same-name conflicts.
7. Keep MCP servers, plugins, hooks, and runtime settings in their native project files. When such
   files exist, inventory their relative paths, hashes, runtimes, verification action, and secret
   environment-variable names in `.harness/integrations.json`; never copy credentials into Git.

Assemble is complete when the confirmed manifest is represented by committed project files, every
package has source and hash provenance, and no unconfirmed integration or workflow was added.

## Finish

Prove each layer separately:

1. **Stored** — selected packages and locks exist under `.harness/`.
2. **Wired** — `.agents/skills` and `.claude/skills` resolve to the same project snapshot.
3. **Healthy** — `harness health` passes: template markers, names, registry, public/overlay hashes,
   discovery links, and integration inventory agree.
4. **Advertised** — a fresh runtime session lists an installed project skill. If the runtime has no
   native project skill root, it can route through `.harness/skills/REGISTRY.md` from `AGENTS.md`.
5. **Invoked** — that session opens the selected skill through its native mechanism.

If the current session cannot rescan discovery, report steps 4 and 5 as the exact remaining fresh-
session check. Finish by naming the project type, installed capabilities, excluded surfaces,
verification evidence, and next useful action.
