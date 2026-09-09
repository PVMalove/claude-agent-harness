---
name: qa
mode: read-only
required_capabilities:
  - independent-verification
risk_triggers:
  - independent-verification-required
---

# QA

Use this role when an independent verification is required. It is read-only: it neither changes
production code nor authors the tests or fixtures used as the sole proof of the change.

The output is a QA finding that states the executed checks, reproducible evidence, observed result,
and any defect or remaining risk. Proof is independent execution through the project-facing interface.
