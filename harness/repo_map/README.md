# Repo Map CLI

`repo_map.py` creates a deterministic, offline map from tracked files at a pinned Git commit:

```bash
python .harness/repo_map/repo_map.py --repo . --commit "$(git rev-parse HEAD)"
```

The JSON output conforms to `repo_map.schema.json`. It includes parser provenance, the policy hash,
selected files, import edges, diagnostics, and a conservative token estimate.

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
    "max_files": 5000,
    "max_file_bytes": 262144,
    "timeout_seconds": 10,
    "max_tokens": 8000
  }
}
```

All path values are case-sensitive glob patterns relative to the repository root. `redact_paths`
excludes a file and its path from the output. The CLI rejects unknown policy fields and invalid
values. `max_tokens` is an upper bound: a caller may request a smaller budget with `--max-tokens`.
