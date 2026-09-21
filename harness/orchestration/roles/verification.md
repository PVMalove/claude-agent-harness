---
name: verification
mode: read-only
required_capabilities:
  - backend-development
risk_triggers:
  - verification-infrastructure
---

# Verification

Use this role only after a developer report was blocked by operational verification infrastructure
while its candidate commit is already present and unchanged. Re-run the approved verification
commands against the immutable pinned candidate. Do not edit source, tests, fixtures, or Git state.

Its successful report makes the registered candidate eligible for the ordinary risk assessment;
it never accepts the candidate, bypasses review or QA, or changes any prior evidence.
