# Golden retrieval evaluation

`golden_tickets.json` labels 20 closed GitHub issues in this repository. Queries paraphrase the
actual goal and acceptance criteria; labels point to the existing ADR or glossary that provides
the corresponding architectural context. URLs and terminal states record the selection provenance.
The guide's PR numbers #340 and #368 were replaced by their closed issues #339 and #359. #462 was
excluded because its actual subject is private-term hook parsing, not glossary terminology.
Some broad guide labels were corrected: #345 concerns storage (ADR 0006), #359 cleanup (ADR 0006),
#421 the implementation QA gate (ADR 0005), and #394 health-report behavior (`CONTEXT.md`).

`baseline_fts5.json` records an offline run over the 10 tracked ADRs, their template, and `CONTEXT.md`, copied
unchanged into an isolated repository with the same explicit ADR/glossary policy used by
`test_build.configure`. Task archives, tracker snapshots, generated runtime state and source code
are excluded. Source and dataset SHA-256 hashes pin the measurement; the report includes every
query's ranked top-5 paths and search status. The evaluation uses raw FTS5 candidates, not the
token-limited interactive search output. Recall is a ticket-level hit rate, not document-level
recall for a multi-label relevance set. Noise is a macro-average over the returned top-5 window.

Measured FTS5: recall@1 **0.70**, recall@3 **0.75**, recall@5 **0.85**, noise **0.83**.
The default quality gate **fails** its 0.70 noise ceiling. Its thresholds remain 0.60 recall@5
and 0.70 noise. This is the recorded comparison point for #431/#433, not a passing quality claim.
The reproducibility test expects that actual failed gate decision as well as the exact metrics.

Reproduce the baseline without enabling memory in this checkout:

```bash
.harness/.venv/bin/python -m pytest -q tests/memory/test_eval.py -k baseline
```

The test creates and builds a disposable ADR/glossary corpus, then evaluates it.
It verifies hashes and the complete baseline report;
it never rewrites the baseline. Corpus/query edits require an explicit reviewed re-evaluation.
Preserve this FTS5 measurement when comparing a vector model, thresholds or source quotas.

For a separately configured repository with an existing index:

```bash
python harness/bin/harness.py memory eval <repo> --dataset tests/memory/golden_tickets.json --json
```

The command returns 1 on a quality failure and 0 on success. Omit `--json` for text metrics.
Missing, corrupt, incompatible or disabled memory also fails and exposes the per-query status.
