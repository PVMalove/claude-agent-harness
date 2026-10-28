-- The schema of the store at version 2, as the release before the generation guard and the QA
-- lease of version 3 wrote it. The upgrade test builds a store from this text and checks that
-- `upgrade` reaches the schema `create` writes.
CREATE TABLE store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE batch (
    id                             TEXT PRIMARY KEY,
    state                          TEXT NOT NULL,
    revision                       INTEGER NOT NULL DEFAULT 1,
    risk_reassessment_required     INTEGER,
    risk_reassessment_candidate    TEXT,
    risk_reassessment_pending_next INTEGER,
    body_json                      TEXT NOT NULL,
    -- Issue #781: a batch that owes a risk reassessment names its candidate or is pending.
    CHECK (NOT (
        risk_reassessment_required = 1
        AND COALESCE(risk_reassessment_candidate, '') = ''
        AND COALESCE(risk_reassessment_pending_next, 0) != 1
    ))
) STRICT;

CREATE TABLE plan (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE dispatch (
    id        TEXT PRIMARY KEY,
    batch_id  TEXT NOT NULL REFERENCES batch (id),
    state     TEXT NOT NULL,
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE dispatch_status (
    id        TEXT PRIMARY KEY REFERENCES dispatch (id),
    state     TEXT NOT NULL,
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL,
    execution_state TEXT CHECK (execution_state IS NULL OR (
        execution_state IN ('launching', 'running', 'stopping', 'exited', 'lost', 'unknown')
        AND NOT (
            execution_state IN ('unknown', 'launching', 'running')
            AND state IN ('reported', 'failed', 'blocked', 'cancelled', 'abandoned')
        )))
) STRICT;

CREATE TABLE risk_assessment (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE context_package (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE checkpoint (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE qa_lane (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    revision      INTEGER NOT NULL DEFAULT 1,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    body_json     TEXT NOT NULL
) STRICT;

CREATE TABLE qa_queue (
    id        TEXT PRIMARY KEY,
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE record (
    path       TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
    body_json  TEXT NOT NULL
) STRICT;

CREATE TABLE artifact (
    path   TEXT PRIMARY KEY,
    bytes  BLOB NOT NULL,
    sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE legacy_generation (
    generation TEXT NOT NULL,
    path       TEXT NOT NULL,
    bytes      BLOB NOT NULL,
    sha256     TEXT NOT NULL,
    PRIMARY KEY (generation, path)
) STRICT;

CREATE TABLE legacy_audit (
    path                TEXT PRIMARY KEY,
    bytes               BLOB NOT NULL,
    sha256              TEXT NOT NULL,
    record_sha256_valid INTEGER NOT NULL
) STRICT;

CREATE TABLE import_manifest (
    path   TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE audit (
    seq               INTEGER PRIMARY KEY,
    at                TEXT NOT NULL,
    action            TEXT NOT NULL,
    details_json      TEXT NOT NULL,
    record_sha256     TEXT NOT NULL,
    prev_chain_sha256 TEXT NOT NULL,
    chain_sha256      TEXT NOT NULL
) STRICT;

-- One row: the last audit record that an ordinary command need not verify again.
CREATE TABLE audit_checkpoint (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    seq          INTEGER NOT NULL,
    chain_sha256 TEXT NOT NULL
) STRICT;

-- The idempotent writes (ADR 0021, section 4): one row per operation id.
CREATE TABLE operations (
    operation_id   TEXT PRIMARY KEY,
    operation      TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('intent', 'committed', 'failed')),
    result_json    TEXT,
    created_at     TEXT NOT NULL,
    completed_at   TEXT
) STRICT;
