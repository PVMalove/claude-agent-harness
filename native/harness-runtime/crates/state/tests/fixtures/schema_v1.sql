-- The schema of the store at version 1, as the first release of the runtime wrote it. The upgrade
-- test builds a store from this text and checks that `upgrade` reaches the schema `create` writes.
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
    body_json TEXT NOT NULL
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
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE record (
    path       TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
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

CREATE TABLE audit_checkpoint (
    seq          INTEGER NOT NULL,
    chain_sha256 TEXT NOT NULL
) STRICT;
