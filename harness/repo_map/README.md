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
    "tier": "reduced",
    "parser_bundle_registry_paths": [],
    "parser_bundle_timeout_seconds": 30,
    "parser_bundle_max_output_bytes": 10000000
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

## Opt-in `full` tier (offline parser bundle)

Setting `repo_map_policy.tier` to `"full"` opts into an additional, offline parser bundle for
non-Python files. Python files are always parsed with the standard-library AST, in every tier; the
bundle only ever adds coverage for the file types its pinned grammars declare. The mechanism never
makes a network call: it looks for a `parser_bundle.lock.json` under
`.harness/.cache/repo_map/parser_bundle/registry/` (or an extra directory listed in
`parser_bundle_registry_paths`), verifies every wheel's sha256 against the lock, and installs the
matching interpreter/platform wheelhouse with `pip install --no-index --require-hashes
--only-binary=:all: --target <cache dir>` -- never a `.venv`, `requirements.txt`, or a change to the
target project, and never `uv run`. The worker subprocess that does the actual parsing is bounded by
`parser_bundle_timeout_seconds` (wall clock) and `parser_bundle_max_output_bytes` (stdout size).

Any failure along that path -- no bundle found, a wrong hash, a missing wheelhouse for the running
interpreter's `(python_tag, platform_tag)` pair, or the worker subprocess exceeding its time or
output limit -- degrades the whole run to `tier: "reduced"`, `parser: "ast-only"`, with
`degradation_reason` naming the specific cause. A successful `full`-tier run reports
`tier: "full"`, `parser: "bundle"`, and extends `parser_provenance` with `bundle_mode`,
`bundle_source`, `python_tag`, `platform_tag`, `lock_sha256`, `script_hash`, `core_version`,
`core_abi_range`, and `grammars` (each with `name`, `version`, `abi`, `sha256`) -- everything needed
to audit exactly which pinned artifacts produced the output.

Real tree-sitter grammars, a real release lock, and a real wheelhouse are release assets delivered
by a later change (see `docs/adr/0024-repo-map-parser-bundle-composition-and-delivery.md`); this
loader only proves the generic locate/verify/install/execute/degrade mechanism.

The CLI rejects unknown policy fields and invalid values with an error and remedy. Token budgets
resolve in this order: `--max-tokens`, `repo_map_policy.max_tokens` when the policy is present,
then the default of 4000. A missing `.harness/orchestration.json` keeps the CLI portable; a caller
may request a smaller budget with `--max-tokens`.
