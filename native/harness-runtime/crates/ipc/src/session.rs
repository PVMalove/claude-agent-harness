//! One connection's conversation: `hello`, then requests (ADR 0021, section 3).
//!
//! A session turns one request frame into one response and says whether the connection must
//! close afterwards. It does no socket I/O: it only reads the state directory that `hello` and
//! `state.read` answer from, so every rule is tested against a scratch state without a socket.

use std::path::{Path, PathBuf};

use contracts::{
    ErrorBody, ErrorCode, FailureDomain, HelloPayload, HelloResult, PROTOCOL_VERSION,
    ProtocolVersion, Request, Response, decode, negotiate,
};
use serde_json::{Value, json};

use crate::paths::{canonical, git_common_dir, repo_id};
use crate::token::tokens_equal;

/// What every session of one server shares.
#[derive(Clone, Debug)]
pub struct Shared {
    pub token: String,
    pub repo_id: String,
    pub server_version: String,
    /// The state directory whose runtime store this runtime serves once the store is the
    /// authority (ADR 0020, section 1); `None` for a runtime that serves no state.
    pub state_dir: Option<PathBuf>,
}

/// The answer to one frame.
#[derive(Debug)]
pub struct Reply {
    pub response: Response,
    /// The connection is closed after the response is written.
    pub close: bool,
}

/// The state of one connection: the protocol version `hello` negotiated, once it did.
#[derive(Debug, Default)]
pub struct Session {
    negotiated: Option<ProtocolVersion>,
}

fn operation_id_of(body: &[u8]) -> String {
    decode::<Value>(body)
        .ok()
        .and_then(|value| value.get("operation_id")?.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn failure(operation_id: &str, error: ErrorBody, close: bool) -> Reply {
    Reply {
        response: Response::error(operation_id, error),
        close,
    }
}

impl Session {
    pub fn new() -> Self {
        Self::default()
    }

    /// The reply to one frame body, given the current time in UTC milliseconds.
    pub fn handle(&mut self, shared: &Shared, body: &[u8], now_ms: u64) -> Reply {
        let request: Request = match decode(body) {
            Ok(request) => request,
            Err(err) => return malformed(&operation_id_of(body), &err.to_string()),
        };
        if request.request_deadline < now_ms {
            return failure(
                &request.operation_id,
                ErrorBody::new(
                    ErrorCode::DeadlineExceeded,
                    FailureDomain::Timeout,
                    false,
                    "the request deadline had passed when the runtime read the request",
                    "send the request again with a later request_deadline",
                )
                .with_fact("now_ms", now_ms),
                false,
            );
        }
        if request.operation == "hello" {
            return self.hello(shared, &request);
        }
        let Some(negotiated) = self.negotiated else {
            return failure(
                &request.operation_id,
                ErrorBody::new(
                    ErrorCode::UnauthorizedPeer,
                    FailureDomain::Policy,
                    false,
                    "the connection has not completed hello",
                    "send hello with the runtime token before any other request",
                ),
                true,
            );
        };
        if request.protocol_version.major != negotiated.major {
            return failure(
                &request.operation_id,
                ErrorBody::new(
                    ErrorCode::ProtocolUnsupported,
                    FailureDomain::Tooling,
                    false,
                    "the request names another protocol major than hello negotiated",
                    "send the negotiated protocol_version",
                )
                .with_fact("negotiated_major", negotiated.major),
                false,
            );
        }
        match request.operation.as_str() {
            "status" => Reply {
                response: Response::ok(
                    &request.operation_id,
                    json!({
                        "state": "serving",
                        "pid": std::process::id(),
                        "repo_id": shared.repo_id,
                    }),
                ),
                close: false,
            },
            "state.read" => state_read(shared, &request),
            "state.write" => state_write(shared, &request),
            "state.validate" => state_validate(shared, &request),
            other => failure(
                &request.operation_id,
                ErrorBody::new(
                    ErrorCode::UnknownOperation,
                    FailureDomain::Tooling,
                    false,
                    format!("the runtime does not serve the operation {other:?}"),
                    "use an operation the hello features list names, or update the runtime",
                ),
                false,
            ),
        }
    }

    fn hello(&mut self, shared: &Shared, request: &Request) -> Reply {
        let id = &request.operation_id;
        let Some(payload) = request.payload.clone() else {
            return malformed(id, "hello needs a payload");
        };
        let hello: HelloPayload = match serde_json::from_value(payload) {
            Ok(hello) => hello,
            Err(err) => return malformed(id, &err.to_string()),
        };
        if !tokens_equal(hello.token.as_bytes(), shared.token.as_bytes()) {
            return failure(
                id,
                ErrorBody::new(
                    ErrorCode::UnauthorizedPeer,
                    FailureDomain::Policy,
                    false,
                    "the token is not the runtime's token",
                    "read the token file of this runtime again; a restarted runtime writes a new one",
                ),
                true,
            );
        }
        if !serves_repository(shared, &hello.repo_root) {
            return failure(
                id,
                ErrorBody::new(
                    ErrorCode::PolicyDenied,
                    FailureDomain::Policy,
                    false,
                    "this runtime serves another repository",
                    "connect to the runtime socket of the repository you work in",
                ),
                true,
            );
        }
        let Some(version) = negotiate(PROTOCOL_VERSION, hello.protocol) else {
            return failure(
                id,
                ErrorBody::new(
                    ErrorCode::ProtocolUnsupported,
                    FailureDomain::Tooling,
                    false,
                    "the client and the runtime share no protocol major version",
                    "update harness or harness-runtime so that both speak the same protocol major",
                )
                .with_fact("server", json!(PROTOCOL_VERSION))
                .with_fact("client", json!(hello.protocol)),
                true,
            );
        };
        self.negotiated = Some(version);
        // The store is the authority only after a cutover, which can happen while this runtime
        // lives, so every `hello` asks again.
        let store_id = shared.state_dir.as_deref().and_then(state::read::store_id);
        let mut features = vec!["status".to_owned()];
        if store_id.is_some() {
            features.push("state.read".to_owned());
            features.push("state.write".to_owned());
            features.push("state.validate".to_owned());
        }
        let result = HelloResult {
            protocol: version,
            server_version: shared.server_version.clone(),
            store_id,
            features,
        };
        Reply {
            response: Response::ok(id, json!(result)),
            close: false,
        }
    }
}

/// `state.read`: records of the runtime store in the form of the legacy ledger files.
fn state_read(shared: &Shared, request: &Request) -> Reply {
    use state::read::ReadError;

    let id = &request.operation_id;
    let refusal = |code, domain, retryable, message: String, remedy: &str| {
        failure(
            id,
            ErrorBody::new(code, domain, retryable, message, remedy),
            false,
        )
    };
    let Some(state_dir) = shared.state_dir.as_deref() else {
        return refusal(
            ErrorCode::NotFound,
            FailureDomain::Policy,
            false,
            "this runtime serves no state".to_owned(),
            "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
        );
    };
    match state::read::read(
        state_dir,
        request.payload.as_ref(),
        request.expected_revision,
    ) {
        Ok(answer) => {
            let mut response = Response::ok(
                id,
                json!({
                    "records": answer.records,
                    "revisions": answer.revisions,
                    "generation_revision": answer.generation_revision,
                }),
            );
            response.revision = Some(answer.revision);
            Reply {
                response,
                close: false,
            }
        }
        Err(ReadError::NotAttached(reason)) => refusal(
            ErrorCode::NotFound,
            FailureDomain::Policy,
            false,
            format!("the runtime store is not the authority of this repository: {reason}"),
            "run `harness-runtime migrate --repo <repo>`, or use the installation that matches the state",
        ),
        Err(ReadError::Malformed(detail)) => {
            let Reply { response, .. } = malformed(id, &detail);
            Reply {
                response,
                close: false,
            }
        }
        Err(ReadError::RevisionConflict { expected, actual }) => failure(
            id,
            ErrorBody::new(
                ErrorCode::RevisionConflict,
                FailureDomain::Infrastructure,
                true,
                "the store changed since the revision the request expected",
                "read again",
            )
            .with_fact("expected_revision", expected)
            .with_fact("actual_revision", actual),
            false,
        ),
        Err(ReadError::Broken(broken)) => failure(id, broken.to_error(), false),
        Err(ReadError::TooLarge) => failure(
            id,
            ErrorBody::new(
                ErrorCode::MalformedRequest,
                FailureDomain::Tooling,
                false,
                "the answer would not fit one frame",
                "ask for fewer collections or paths in one request",
            )
            .with_fact("reason", "answer_too_large"),
            false,
        ),
        Err(ReadError::Store {
            message,
            busy: true,
        }) => refusal(
            ErrorCode::StoreBusy,
            FailureDomain::Infrastructure,
            true,
            format!("the store is busy: {message}"),
            "repeat the command",
        ),
        Err(ReadError::Store {
            message,
            busy: false,
        }) => refusal(
            ErrorCode::Internal,
            FailureDomain::Infrastructure,
            false,
            format!("the store cannot be read: {message}"),
            "repeat the command; if the store stays unreadable, restore the state directory from the snapshot named in AUTHORITY.json",
        ),
    }
}

/// `state.write`: the mutations of one command, applied in one transaction (ADR 0020, section 3).
fn state_write(shared: &Shared, request: &Request) -> Reply {
    let id = &request.operation_id;
    let Some(state_dir) = shared.state_dir.as_deref() else {
        return failure(
            id,
            ErrorBody::new(
                ErrorCode::NotFound,
                FailureDomain::Policy,
                false,
                "this runtime serves no state",
                "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
            ),
            false,
        );
    };
    match state::write::write(state_dir, request) {
        Ok(written) => {
            let mut response = Response::ok(id, written.result);
            response.revision = Some(written.revision);
            Reply {
                response,
                close: false,
            }
        }
        Err(error) => failure(id, error, false),
    }
}

/// `state.validate`: the verification of the store; `{"full": true}` recomputes everything from
/// the genesis of the audit chain and does not use the checkpoint (ADR 0020, section 4).
fn state_validate(shared: &Shared, request: &Request) -> Reply {
    let id = &request.operation_id;
    let full = match request.payload.as_ref() {
        None => false,
        Some(Value::Object(members))
            if members.keys().all(|key| key == "full")
                && members.get("full").is_none_or(Value::is_boolean) =>
        {
            members.get("full") == Some(&Value::Bool(true))
        }
        Some(_) => return malformed(id, "the payload is {\"full\": <bool>} or none"),
    };
    let Some(state_dir) = shared.state_dir.as_deref() else {
        return failure(
            id,
            ErrorBody::new(
                ErrorCode::NotFound,
                FailureDomain::Policy,
                false,
                "this runtime serves no state",
                "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
            ),
            false,
        );
    };
    match state::validate::validate(state_dir, full) {
        Ok(result) => Reply {
            response: Response::ok(id, result),
            close: false,
        },
        Err(error) => failure(id, error, false),
    }
}

/// A client names its repository by a working tree root. The runtime serves the repository, not
/// one checkout: a linked worktree has the repo-id of its main checkout (ADR 0021, section 2),
/// the id that picked this socket, so it is the same repository.
fn serves_repository(shared: &Shared, client_root: &str) -> bool {
    canonical(Path::new(client_root))
        .and_then(|root| git_common_dir(&root).map_err(std::io::Error::other))
        .is_ok_and(|common| repo_id(&common) == shared.repo_id)
}

fn malformed(operation_id: &str, detail: &str) -> Reply {
    failure(
        operation_id,
        ErrorBody::new(
            ErrorCode::MalformedRequest,
            FailureDomain::Tooling,
            false,
            format!("the request is not valid: {detail}"),
            "send a request the protocol defines: a known envelope, no unknown fields",
        ),
        false,
    )
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use contracts::Status;

    use super::*;

    const NOW: u64 = 1_000;

    /// A scratch directory with a `.git` directory: the root of a repository.
    fn scratch_repository(name: &str) -> PathBuf {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let base =
            std::env::temp_dir().join(format!("hr-session-{name}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(base.join("repo/.git")).unwrap();
        canonical(&base.join("repo")).unwrap()
    }

    fn shared_for(root: &Path) -> Shared {
        let id = repo_id(&git_common_dir(root).unwrap());
        Shared {
            token: "secret".to_owned(),
            repo_id: id,
            server_version: "9.9.9".to_owned(),
            state_dir: None,
        }
    }

    /// A server state and the root of the repository it serves, the root a client says in `hello`.
    fn setup() -> (Shared, PathBuf) {
        let root = scratch_repository("shared");
        (shared_for(&root), root)
    }

    fn hello_body(client_root: &Path, token: &str, min: u32, max: u32) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "protocol_version": {"major": max, "minor": 0},
            "operation_id": "h",
            "operation": "hello",
            "request_deadline": NOW + 1,
            "payload": {
                "protocol": {"min": {"major": min, "minor": 0}, "max": {"major": max, "minor": 0}},
                "client_version": "1",
                "repo_root": client_root,
                "token": token,
            },
        }))
        .unwrap()
    }

    fn status_body(major: u32, operation: &str, deadline: u64) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "protocol_version": {"major": major, "minor": 0},
            "operation_id": "s",
            "operation": operation,
            "request_deadline": deadline,
        }))
        .unwrap()
    }

    fn code(reply: &Reply) -> ErrorCode {
        assert_eq!(reply.response.status, Status::Error);
        reply.response.error.as_ref().unwrap().code
    }

    fn authenticated(shared: &Shared, root: &Path) -> Session {
        let mut session = Session::new();
        let reply = session.handle(shared, &hello_body(root, "secret", 1, 1), NOW);
        assert_eq!(reply.response.status, Status::Ok);
        session
    }

    #[test]
    fn hello_with_the_token_negotiates_the_version_and_names_the_server() {
        let (shared, root) = setup();

        let reply = Session::new().handle(&shared, &hello_body(&root, "secret", 1, 1), NOW);

        let result = reply.response.result.unwrap();
        assert_eq!(result["protocol"], json!({"major": 1, "minor": 0}));
        assert_eq!(result["server_version"], "9.9.9");
        assert_eq!(result["store_id"], Value::Null);
        assert!(!reply.close);
    }

    #[test]
    fn a_wrong_token_is_unauthorized_and_closes_the_connection() {
        let (shared, root) = setup();

        let reply = Session::new().handle(&shared, &hello_body(&root, "guess", 1, 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnauthorizedPeer);
        assert!(reply.close);
    }

    #[test]
    fn an_incompatible_major_is_protocol_unsupported_with_both_versions() {
        let (shared, root) = setup();

        let reply = Session::new().handle(&shared, &hello_body(&root, "secret", 2, 2), NOW);

        assert_eq!(code(&reply), ErrorCode::ProtocolUnsupported);
        let error = reply.response.error.unwrap();
        assert_eq!(error.facts["server"], json!({"major": 1, "minor": 1}));
        assert!(error.remedy.contains("update harness"));
        assert!(reply.close);
    }

    #[test]
    fn hello_for_another_repository_is_refused() {
        let (shared, _) = setup();
        let other = scratch_repository("other");

        let reply = Session::new().handle(&shared, &hello_body(&other, "secret", 1, 1), NOW);

        assert_eq!(code(&reply), ErrorCode::PolicyDenied);
    }

    #[test]
    fn hello_from_a_path_that_is_no_repository_is_refused() {
        let (shared, _) = setup();

        let reply = Session::new().handle(
            &shared,
            &hello_body(Path::new("/nonexistent-repo"), "secret", 1, 1),
            NOW,
        );

        assert_eq!(code(&reply), ErrorCode::PolicyDenied);
    }

    #[test]
    fn hello_from_a_linked_worktree_is_the_same_repository() {
        let (shared, root) = setup();
        let git_dir = root.join(".git/worktrees/linked");
        fs::create_dir_all(&git_dir).unwrap();
        fs::write(git_dir.join("commondir"), "../..\n").unwrap();
        let linked = root.parent().unwrap().join("linked");
        fs::create_dir_all(&linked).unwrap();
        fs::write(
            linked.join(".git"),
            format!("gitdir: {}\n", git_dir.display()),
        )
        .unwrap();

        let reply = Session::new().handle(&shared, &hello_body(&linked, "secret", 1, 1), NOW);

        assert_eq!(reply.response.status, Status::Ok);
    }

    #[test]
    fn a_request_before_hello_is_unauthorized() {
        let reply = Session::new().handle(&setup().0, &status_body(1, "status", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnauthorizedPeer);
        assert!(reply.close);
    }

    #[test]
    fn an_authenticated_status_request_is_served() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &status_body(1, "status", NOW + 1), NOW);

        assert_eq!(reply.response.status, Status::Ok);
        assert_eq!(reply.response.operation_id, "s");
        assert_eq!(reply.response.result.unwrap()["state"], "serving");
    }

    #[test]
    fn an_unknown_operation_is_named() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &status_body(1, "launch", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnknownOperation);
        assert!(!reply.close);
    }

    #[test]
    fn another_major_after_hello_is_protocol_unsupported() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &status_body(2, "status", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::ProtocolUnsupported);
    }

    #[test]
    fn an_unknown_request_field_is_malformed_and_keeps_the_operation_id() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);
        let body = serde_json::to_vec(&json!({
            "protocol_version": {"major": 1, "minor": 0},
            "operation_id": "keep-me",
            "operation": "status",
            "request_deadline": NOW + 1,
            "surprise": 1,
        }))
        .unwrap();

        let reply = session.handle(&shared, &body, NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
        assert_eq!(reply.response.operation_id, "keep-me");
        assert!(!reply.close);
    }

    #[test]
    fn bytes_that_are_no_json_are_malformed() {
        let reply = Session::new().handle(&setup().0, b"\xff not json", NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
        assert_eq!(reply.response.operation_id, "");
    }

    #[test]
    fn a_passed_request_deadline_is_deadline_exceeded() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &status_body(1, "status", NOW - 1), NOW);

        assert_eq!(code(&reply), ErrorCode::DeadlineExceeded);
    }

    #[test]
    fn a_hello_without_a_payload_is_malformed() {
        let body = status_body(1, "hello", NOW + 1);

        let reply = Session::new().handle(&setup().0, &body, NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
    }

    fn read_body(payload: Value, expected_revision: Option<u64>) -> Vec<u8> {
        let mut request = json!({
            "protocol_version": {"major": 1, "minor": 0},
            "operation_id": "r",
            "operation": "state.read",
            "request_deadline": NOW + 1,
            "payload": payload,
        });
        if let Some(revision) = expected_revision {
            request["expected_revision"] = json!(revision);
        }
        serde_json::to_vec(&request).unwrap()
    }

    /// A runtime that serves the state directory of a repository whose state was cut over to the
    /// store: the fixture state of the Python 1.x ledger, migrated.
    fn serving_a_store() -> (Shared, PathBuf, String) {
        let root = scratch_repository("store");
        let state_dir = root.join(".harness/orchestration/state");
        let fixture =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../state/tests/fixtures/legacy_v3");
        copy_tree(&fixture, &state_dir);
        let generation = fs::read_dir(state_dir.join("generations"))
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| path.join("qa-lane/lease.json").exists())
            .unwrap();
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        for entry in fs::read_dir(generation.join("qa-lane/queue")).unwrap() {
            fs::remove_file(entry.unwrap().path()).unwrap();
        }
        let outcome = state::migrate::run(&root).unwrap();
        let mut shared = shared_for(&root);
        shared.state_dir = Some(state_dir);
        (shared, root, outcome.authority.store_id)
    }

    fn copy_tree(from: &Path, to: &Path) {
        fs::create_dir_all(to).unwrap();
        for entry in fs::read_dir(from).unwrap() {
            let entry = entry.unwrap();
            let target = to.join(entry.file_name());
            if entry.file_type().unwrap().is_dir() {
                copy_tree(&entry.path(), &target);
            } else {
                fs::copy(entry.path(), target).unwrap();
            }
        }
    }

    #[test]
    fn a_runtime_that_serves_no_state_does_not_know_state_read() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &read_body(json!({}), None), NOW);

        assert_eq!(code(&reply), ErrorCode::NotFound);
    }

    #[test]
    fn before_the_cutover_hello_names_no_store_and_state_read_is_not_found() {
        let (mut shared, root) = setup();
        shared.state_dir = Some(root.join(".harness/orchestration/state"));
        let mut session = Session::new();

        let hello = session.handle(&shared, &hello_body(&root, "secret", 1, 1), NOW);
        let reply = session.handle(
            &shared,
            &read_body(json!({"collections": ["batches"]}), None),
            NOW,
        );

        let result = hello.response.result.unwrap();
        assert_eq!(result["store_id"], Value::Null);
        assert_eq!(result["features"], json!(["status"]));
        assert_eq!(code(&reply), ErrorCode::NotFound);
    }

    #[test]
    fn after_the_cutover_hello_names_the_store_and_state_read_answers_records_and_the_revision() {
        let (shared, root, store_id) = serving_a_store();
        let mut session = Session::new();

        let hello = session.handle(&shared, &hello_body(&root, "secret", 1, 1), NOW);
        let reply = session.handle(
            &shared,
            &read_body(
                json!({"collections": ["batches"], "paths": ["plans/nobody.json"]}),
                Some(1),
            ),
            NOW,
        );

        let result = hello.response.result.unwrap();
        assert_eq!(result["store_id"], store_id.as_str());
        assert_eq!(
            result["features"],
            json!(["status", "state.read", "state.write", "state.validate"])
        );
        assert_eq!(reply.response.status, Status::Ok);
        assert_eq!(reply.response.revision, Some(1));
        let records = &reply.response.result.unwrap()["records"];
        assert_eq!(records.as_object().unwrap().len(), 1);
    }

    #[test]
    fn a_stale_revision_is_a_retryable_revision_conflict() {
        let (shared, root, _) = serving_a_store();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &read_body(json!({}), Some(9)), NOW);

        assert_eq!(code(&reply), ErrorCode::RevisionConflict);
        assert!(reply.response.error.unwrap().retryable);
    }

    fn write_body(id: &str, mutations: Value) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "protocol_version": {"major": 1, "minor": 1},
            "operation_id": id,
            "operation": "state.write",
            "request_deadline": NOW + 1,
            "payload": {"mutations": mutations},
        }))
        .unwrap()
    }

    #[test]
    fn a_runtime_that_serves_no_state_does_not_know_state_write() {
        let (shared, root) = setup();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &write_body("w", json!([])), NOW);

        assert_eq!(code(&reply), ErrorCode::NotFound);
    }

    #[test]
    fn state_write_commits_and_answers_the_revisions_and_the_audit_revision() {
        let (shared, root, _) = serving_a_store();
        let mut session = authenticated(&shared, &root);
        let mutation = json!([{
            "op": "create",
            "path": "reports/r1.json",
            "content": "{\n  \"a\": 1\n}\n",
        }]);

        let reply = session.handle(&shared, &write_body("w", mutation.clone()), NOW);
        let repeat = session.handle(&shared, &write_body("w", mutation), NOW);

        assert_eq!(reply.response.status, Status::Ok);
        assert_eq!(reply.response.revision, Some(2));
        let result = reply.response.result.unwrap();
        assert_eq!(result["revisions"]["reports/r1.json"], 1);
        assert_eq!(repeat.response.revision, Some(2));
        assert_eq!(repeat.response.result.unwrap(), result);
    }

    fn validate_body(payload: Value) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "protocol_version": {"major": 1, "minor": 1},
            "operation_id": "v",
            "operation": "state.validate",
            "request_deadline": NOW + 1,
            "payload": payload,
        }))
        .unwrap()
    }

    #[test]
    fn state_validate_answers_the_mode_and_refuses_a_payload_it_does_not_define() {
        let (shared, root, _) = serving_a_store();
        let mut session = authenticated(&shared, &root);

        let ordinary = session.handle(&shared, &validate_body(json!({})), NOW);
        let full = session.handle(&shared, &validate_body(json!({"full": true})), NOW);
        let unknown = session.handle(&shared, &validate_body(json!({"deep": true})), NOW);
        let not_boolean = session.handle(&shared, &validate_body(json!({"full": "yes"})), NOW);

        assert_eq!(ordinary.response.result.unwrap()["mode"], "incremental");
        let full = full.response.result.unwrap();
        assert_eq!(
            (full["mode"].as_str(), full["valid"].as_bool()),
            (Some("full"), Some(true))
        );
        assert_eq!(code(&unknown), ErrorCode::MalformedRequest);
        assert_eq!(code(&not_boolean), ErrorCode::MalformedRequest);
    }

    #[test]
    fn a_refused_state_write_answers_the_typed_error_of_the_store() {
        let (shared, root, _) = serving_a_store();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(
            &shared,
            &write_body(
                "w",
                json!([{"op": "create", "path": "audit/x.json", "content": "{}\n"}]),
            ),
            NOW,
        );

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
        assert!(!reply.close);
    }

    #[test]
    fn an_unknown_payload_field_of_state_read_is_malformed() {
        let (shared, root, _) = serving_a_store();
        let mut session = authenticated(&shared, &root);

        let reply = session.handle(&shared, &read_body(json!({"surprise": true}), None), NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
        assert!(!reply.close);
    }
}
