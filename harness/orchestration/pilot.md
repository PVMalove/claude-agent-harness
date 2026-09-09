# Backend orchestration pilot baseline

This guide records the first observable baseline for the optional
`backend-orchestration` capability. It is an observation protocol for a coordinator, not a
dispatch mechanism or a performance policy.

## Purpose

Collect comparable evidence from real completed batches before deciding whether the project needs
future targets or process changes. Use the same worksheet and counting rules for the whole
observation period.

## Before the first run

1. Choose an observation period and write down its start and end timestamps.
2. Define where the evidence will come from: batch records, completion reports, quality-gate logs,
   and the issue tracker. Record unavailable sources before the period starts.
3. Record the batch ID, ticket ID, issue branch, role sequence, and completion state for every
   batch in scope.
4. Declare the post-integration observation window and the project's severity rule for defects.
5. Prepare one worksheet row per batch and one summary row per closed ticket. Do not fill missing
   measurements with estimates.

## Observation period

For every closed ticket in the period, apply the same rules:

- count every role start as a start, and count a retry as a new dispatch start;
- attribute reported input and output tokens to the dispatch that produced them; mark token data as
  missing when the execution environment does not report it;
- record the quality gate's elapsed run time separately from any queue or waiting time;
- link each post-integration defect to its batch, and apply the declared observation window and
  severity rule consistently;
- retain the measurement period, batch/ticket IDs, source, missing-data notes, and same counting
  rules with the worksheet.

## Record each batch

Capture the following before closing the batch record:

- batch and ticket IDs, roles started, dispatch IDs, and whether any dispatch was a retry;
- token counts for each dispatch when available, with the source and missing-data note;
- the quality-gate start and result timestamps, plus queue time when it is available;
- the integration timestamp and any linked defect IDs observed during the declared window.

## Measure the quality gate

Use the timestamps emitted by the serialized quality-gate lane. Calculate wall time from gate start to
gate result, and keep queue time as a separate observation. If either timestamp is unavailable,
record the gap instead of reconstructing it from unrelated events.

## Record post-integration defects

During the declared observation window, record each defect that can be linked to a batch. Include the
batch ID, defect ID, discovery timestamp, severity under the project's rule, and evidence source.
Do not count an unlinked defect or a defect outside the window without documenting why it is
included.

## Baseline worksheet

| Measurement | Required observation | Source / missing-data note |
| --- | --- | --- |
| Agent starts per closed ticket | Starts and retries for each closed ticket | Batch records and dispatch IDs |
| Tokens per batch | Reported input and output tokens for every dispatch | Completion reports or execution records |
| Quality-gate wall time | Gate start to result, with queue time separate | Quality-gate timestamps |
| Post-integration defects | Linked defects in the declared window under the severity rule | Issue tracker and defect records |

The resulting baseline is evidence for later decisions. This guide does not prescribe a named
provider, model, runtime, or platform, or a hard numerical target.
