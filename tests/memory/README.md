# Golden retrieval evaluation

`golden_tickets.json` labels 20 closed GitHub issues in this repository. Queries paraphrase the
actual goal and acceptance criteria; labels point to the existing ADR or glossary that provides
the corresponding architectural context. URLs and terminal states record the selection provenance.
The guide's PR numbers #340 and #368 were replaced by their closed issues #339 and #359. #462 was
excluded because its actual subject is private-term hook parsing, not glossary terminology.
Some broad guide labels were corrected: #345 concerns storage (ADR 0006), #359 cleanup (ADR 0006),
#421 the implementation QA gate (ADR 0005), and #394 health-report behavior (`CONTEXT.md`).

The initial one-source labels omitted useful supplementary context. The corrected relevance sets
cover the unchanged goals and DoD across the whole corpus: e.g. shared-index work also uses the
storage ADR, console update/cleanup uses the delivery/storage ADRs, and Context Package/report
work uses their glossary contracts. Every label has a `source_evidence` excerpt copied exactly
from the source and a ticket-specific explanation of its relevance. Labels describe useful
architectural context; they do not claim that every source alone answers the entire ticket.

`baseline_fts5.json` records an offline run over the 10 tracked ADRs, their template, and `CONTEXT.md`, copied
unchanged into an isolated repository with the same explicit ADR/glossary policy used by
`test_build.configure`. Task archives, tracker snapshots, generated runtime state and source code
are excluded. Source and dataset SHA-256 hashes pin the measurement; the report includes every
query's ranked top-5 paths and search status. The evaluation uses raw FTS5 candidates, not the
token-limited interactive search output. Recall is a ticket-level hit rate, not document-level
recall for a multi-label relevance set. Noise is a macro-average over the returned top-5 window.

Measured FTS5 with the corrected labels: recall@1/@3/@5 **0.95**, noise **0.60**.
The default quality gate **passes** its unchanged 0.60 recall@5 floor and 0.70 noise ceiling.
The reproducibility test checks the complete report and CLI exit code 0 on that real corpus.

`baseline_fts5_initial.json` preserves the original single-label measurement byte-for-byte:
recall@1 **0.70**, recall@3 **0.75**, recall@5 **0.85**, noise **0.83**, FAIL.
The historical test reconstructs that dataset, verifies its original SHA-256, reproduces the
entire old report and confirms that ranked paths have not changed. The current version-2 baseline
is an **annotation correction, not a retrieval-engine improvement**: queries, source bytes, FTS5,
top-5 window and thresholds are identical. Vector comparisons must use the same reviewed
multi-label dataset and corpus as the active FTS5 baseline; never compare across label revisions.

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
