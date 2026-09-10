# Use a coordinator-cleared FIFO lease for the QA lane

Each repository has one FIFO QA lane. A queued QA dispatch carries a lease with dispatch ID, PID/host and expiry; the coordinator may clear it only after verifying that its owner is stale, never through automatic force-unlock. This prevents concurrent heavy checks and split-brain recovery while keeping unrelated repository work independent.
