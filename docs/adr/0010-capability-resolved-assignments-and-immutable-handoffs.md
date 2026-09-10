# Capability-resolved assignments and immutable handoffs

Assignments resolve from role manifest to project mapping to a one-run override, and every resolved provider profile must satisfy the role's required capability and limitations. Provider profiles remain project-owned and declare a default model, fallback, and known limitations. Each dispatched batch receives an immutable handoff brief and returns one completion report; later information is a new coordinator decision.

## Consequences

Roles remain portable across runtimes and providers. Agents cannot silently choose an incompatible model, mutate their original scope through chat, or substitute a textual "done" report for commit and verification evidence. Heavy integration and quality gates run in one serialized lane while independent work remains parallel.
