# Markdown role manifests with project-bound assignments

Each role will have one Markdown manifest and share `harness/orchestration/roles/_common.md` for handoff, completion, commit proof, and escalation. A minimal YAML frontmatter exposes only `name`, `mode`, `required_capabilities`, and `risk_triggers`. Project configuration contains an ordered, capability-validated provider-profile plan for each role and records the resolved profile in the immutable brief.

## Consequences

The readable role contract is the source of behavioural authority, while configuration supplies runtime-specific choice. A project cannot weaken a role's write boundary, proof, or risk gate. Shared process rules change in one place rather than drifting among six role files.
