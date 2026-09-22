# Repo Map CLI

`repo_map.py` creates a deterministic, offline map from tracked files at a pinned Git commit:

```bash
python .harness/repo_map/repo_map.py --repo . --commit "$(git rev-parse HEAD)"
```

The JSON output conforms to `repo_map.schema.json`. It includes parser provenance, the policy hash,
selected files, dependency edges, diagnostics, and a conservative token estimate. The provenance
records the applied tier and numeric limits, never the policy's path or symbol patterns.

`--seed` accepts repository-relative paths. The CLI keeps only existing, policy-approved seeds,
then deduplicates and sorts them. With effective seeds, files are ordered by their breadth-first
distance through high- and medium-confidence edges, then by path. Without effective seeds, files
are ordered by high- and medium-confidence in-degree, then by path.

Edges are deterministic and sorted by source, target, kind, and confidence:

- `import` / `high` for resolved Python imports;
- `unique-name-ref` / `medium` when an AST name reference has one definition in another file;
- `ambiguous-name-ref` / `low` when it has two through four definitions in other files.

Names defined in five or more files are ignored. Low-confidence edges remain in the output but do
not affect file ranking.

## Enterprise policy

By default the CLI is portable and applies its built-in exclusions. To enforce project policy, pass
`--policy path/to/orchestration.json`; when omitted, the CLI uses
`.harness/orchestration.json` if that file exists. The policy lives under `repo_map_policy`:

```json
{
  "repo_map_policy": {
    "allow_paths": ["src/**", "tests/**"],
    "deny_paths": ["src/legacy/**"],
    "redact_paths": ["src/customer_data/**"],
    "redact_symbols": ["customer_*"],
    "max_files": 5000,
    "max_file_bytes": 262144,
    "max_path_length": 4096,
    "max_symbol_length": 256,
    "max_signature_length": 2048,
    "timeout_seconds": 10,
    "max_tokens": 8000,
    "tier": "reduced"
  }
}
```

All path values are case-sensitive glob patterns relative to the repository root. `redact_paths`
excludes a file and its path from the output. `redact_symbols` uses case-sensitive symbol globs and
removes matching definitions and references before the graph is built. Paths longer than
`max_path_length`, symbols longer than `max_symbol_length`, and signatures longer than
`max_signature_length` are omitted before serialization. Comments and function bodies are never
serialized.

The `minimal` tier emits only policy-approved paths (`{"path": "..."}`), with no signatures,
parser statuses, or edges. Its JSON `tier` and `degradation_reason` explain the reduction. The
default `reduced` tier uses Python's standard-library AST parser and keeps signatures only.

The CLI rejects unknown policy fields and invalid values with an error and remedy. Token budgets
resolve in this order: `--max-tokens`, `repo_map_policy.max_tokens` when the policy is present,
then the default of 4000. A missing `.harness/orchestration.json` keeps the CLI portable; a caller
may request a smaller budget with `--max-tokens`.
