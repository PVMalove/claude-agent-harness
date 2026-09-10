# Model a ticket batch separately from its role dispatches

A batch owns one ticket, issue branch, worktree and accumulated evidence; it contains sequential role dispatches, each with an immutable brief and terminal outcome. This separation permits `developer → code-review → qa` handoffs and retries without reusing a completed dispatch or losing the ticket-level history.
