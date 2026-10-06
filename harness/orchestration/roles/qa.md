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

Work from the Context Package's `related_tests`; only widen beyond them when that set cannot exercise
the acceptance criteria.

When part of the brief stays undone, list each undone item in `incomplete_items` under the common
contract; its `target_role` is always `qa`, so only a narrowed QA retry finishes it. A narrowed QA
retry still runs every approved verification command. An incomplete item never replaces a failed
or not-run check in `checks_run`.
