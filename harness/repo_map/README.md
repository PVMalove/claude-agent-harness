# Repo Map CLI

`repo_map.py` creates a deterministic, offline map from tracked files at a pinned Git commit:

```bash
python .harness/repo_map/repo_map.py --repo . --commit "$(git rev-parse HEAD)"
```

The JSON output conforms to `repo_map.schema.json`. It includes parser provenance, the policy hash,
selected files, dependency edges, diagnostics, and a conservative token estimate. The provenance
records the applied tier and numeric limits, never the policy's path or symbol patterns.

Repo Map keeps a best-effort, content-addressed cache under the system temporary directory
(`agent-harness/repo-map`), never in the mapped repository or its ledger. The cache key covers the
pinned commit, normalized seeds, budget, policy, parser identity, and token-estimator version.
Entries carry a SHA-256 of their payload; malformed or altered entries are recomputed. Use
`--cache-dir <path>` to select a different disposable cache location.
For `full`, the key also binds the selected bundle lock, worker and wheel bytes for the running
interpreter. A degraded `full` result is never cached, so installing a valid bundle can promote
the same commit from path-only inventory to parsed output.

`--seed` accepts repository-relative paths. The CLI keeps only existing, policy-approved seeds,
then deduplicates and sorts them. With effective seeds, files are ordered by their breadth-first
distance through high- and medium-confidence edges, then by path. Without effective seeds, files
are ordered by high- and medium-confidence in-degree, then by path.

Edges are deterministic and sorted by source, target, kind, and confidence:

- `import` / `high` for resolved Python imports and tracked relative TS/JS static imports;
- `unique-name-ref` / `medium` when a parsed name reference has one definition in another file;
- `ambiguous-name-ref` / `low` when it has two through four definitions in other files.

Names defined in five or more files are ignored. Low-confidence edges remain in the output but do
not affect file ranking.
TS/JS import resolution tries an exact tracked path, then `.ts`, `.tsx`, `.js`, `.jsx`, then the
same extensions under `index`. Bare packages, path aliases, dynamic imports, and type resolution
do not create import edges. Name-reference edges stay inside the Python or TS/JS language family.

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
    "tier": "full",
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

## Tiers: `full` or `minimal`

Every supported language, Python included, is parsed only by tree-sitter grammars from a verified,
offline parser bundle; the CLI has no in-process parser and never falls back to the standard-library
`ast`. The default `full` tier looks for a `parser_bundle.lock.json` under
`.harness/.cache/repo_map/parser_bundle/registry/` (or an extra directory listed in
`parser_bundle_registry_paths`), verifies every wheel's and the worker script's sha256 against the
lock, and installs the matching interpreter/platform wheelhouse with `uv pip install --offline
--no-config --no-index --require-hashes --only-binary :all: --target <cache dir>` -- never pip, a
`.venv`, `requirements.txt`, a change to the target project, `uv run`, or a network call. The worker
subprocess (`tree_sitter_worker.py`, copied into the bundle) is bounded by
`parser_bundle_timeout_seconds` (wall clock) and `parser_bundle_max_output_bytes` (stdout size).

The worker returns only per-file facts -- `parser_status`, `signatures` (text plus every symbol it
exposes), `imports`, `definitions`, and `references`. Symbol redaction, length limits, and all edges
are applied here, in-process, so policy contents never reach the worker. A file with syntax errors
keeps the signatures and imports of its intact definitions and reports `parser_status:
"syntax_error"`; invalid UTF-8 reports `"invalid_encoding"`. Top-level functions and classes and the
methods of top-level classes (`def Class.method(...)`) are serialized; defaults become `...`.
For TS, TSX, JS and JSX, the worker also extracts intact top-level functions, classes, direct
methods and arrow-function declarations, plus static import specifiers. TypeScript and TSX have
distinct grammar identities in provenance.

A successful run reports `tier: "full"`, `parser: "bundle"`, and extends `parser_provenance` with
`bundle_mode`, `bundle_source`, `python_tag`, `platform_tag`, `lock_sha256`, `script_hash`,
`core_version`, `core_abi_range`, and `grammars` (each with `name`, `version`, `abi`, `sha256`).

Any failure -- no bundle, a wrong hash, a missing wheelhouse for the running interpreter's
`(python_tag, platform_tag)` pair, no `uv` on PATH, or the worker exceeding its limits or breaking
the facts contract -- yields `tier: "minimal"`, `parser: "path-only"`: only policy-approved paths
(`{"path": "..."}`), no signatures, statuses, edges, or diagnostics, with `degradation_reason`
naming the cause and `parser_provenance.bundle_mode: "degraded"`. Setting
`repo_map_policy.tier` to `"minimal"` requests the same path inventory without looking for a bundle.

`scripts/build_parser_bundle.py` assembles the one-pair CI smoke bundle from an already-downloaded
wheelhouse. The manual `release-parser-bundle` workflow downloads the 18 binary wheels pinned in
`.github/parser-bundle-release-wheels.json` for Python 3.12–3.14 on Windows x64, Linux x64 and
macOS arm64. `scripts/release_parser_bundle.py` verifies every staged SHA-256, builds a common
nine-pair lock, generates a CycloneDX 1.6 SBOM with every wheel hash, and runs `pip-audit` against
a hashed requirements lock with `--disable-pip` and an empty cache. The workflow uploads the
release bundle only after all steps succeed. Pins and the matrix follow
`docs/adr/0024-repo-map-parser-bundle-composition-and-delivery.md`; generated wheels and release
assets stay outside Git.

The CLI rejects unknown policy fields and invalid values with an error and remedy. Token budgets
resolve in this order: `--max-tokens`, `repo_map_policy.max_tokens` when the policy is present,
then the default of 4000. A missing `.harness/orchestration.json` keeps the CLI portable; a caller
may request a smaller budget with `--max-tokens`.
