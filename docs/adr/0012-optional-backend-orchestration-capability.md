# Optional backend orchestration capability

Backend orchestration will ship as an optional `backend-orchestration` capability extending `pvmalove-suite`. Its first slice contains six role manifests, a schema/template and health validation for project-owned provider profiles, batch lifecycle and handoff documentation, and clean-room tests. It defines no concrete provider or model; projects supply those profiles themselves.

## Consequences

Existing harness installations remain unchanged until they opt in. The batch lifecycle is coordinator-owned (`planned → approved → dispatched → working → completed | blocked | failed`) and each retry is a new dispatch. Orca support is deferred to a separate adapter slice validated against a real backend pilot.
