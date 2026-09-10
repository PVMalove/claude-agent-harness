# Return batches to explicit approval between dispatches

The batch lifecycle is `planned → awaiting-approval ↔ active → completed | blocked | failed`; after each terminal role dispatch that would advance or retry the work, the coordinator prepares the next brief and returns the batch to `awaiting-approval`. This keeps role handoffs and retries visible, prevents autonomous loops, and preserves the human's authority to accept review warnings or stop work.
