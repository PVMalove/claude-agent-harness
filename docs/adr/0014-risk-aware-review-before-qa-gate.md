# Run risk-aware review before the full QA gate

In the hybrid pipeline, a developer dispatch is followed by the independent two-axis `code-review` role when a risk trigger applies, and then by the mandatory full `qa` dispatch. This preserves risk-aware review coverage while avoiding costly integration and E2E checks for a conceptually invalid change; either a review finding or a failed QA gate requires a newly approved developer dispatch.
