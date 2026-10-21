//! One connection's conversation: `hello`, then requests (ADR 0021, section 3).
//!
//! A session turns one request frame into one response and says whether the connection must
//! close afterwards. It does no I/O, so every rule is tested without a socket.

use std::fs;
use std::path::PathBuf;

use contracts::{
    ErrorBody, ErrorCode, FailureDomain, HelloPayload, HelloResult, PROTOCOL_VERSION,
    ProtocolVersion, Request, Response, decode, negotiate,
};
use serde_json::{Value, json};

use crate::paths::{git_common_dir, repo_id};
use crate::token::tokens_equal;

/// What every session of one server shares.
#[derive(Clone, Debug)]
pub struct Shared {
    pub token: String,
    pub repo_root: PathBuf,
    pub repo_id: String,
    pub server_version: String,
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
        let result = HelloResult {
            protocol: version,
            server_version: shared.server_version.clone(),
            store_id: None,
            features: vec!["status".to_owned()],
        };
        Reply {
            response: Response::ok(id, json!(result)),
            close: false,
        }
    }
}

/// A client names its repository by a working tree root. The runtime serves the repository, not
/// one checkout: a linked worktree has the repo-id of its main checkout (ADR 0021, section 2),
/// the id that picked this socket, so it is the same repository.
fn serves_repository(shared: &Shared, client_root: &str) -> bool {
    fs::canonicalize(client_root)
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
        fs::canonicalize(base.join("repo")).unwrap()
    }

    fn shared_for(root: PathBuf) -> Shared {
        let id = repo_id(&git_common_dir(&root).unwrap());
        Shared {
            token: "secret".to_owned(),
            repo_root: root,
            repo_id: id,
            server_version: "9.9.9".to_owned(),
        }
    }

    fn shared() -> Shared {
        shared_for(scratch_repository("shared"))
    }

    fn hello_body(shared: &Shared, token: &str, min: u32, max: u32) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "protocol_version": {"major": max, "minor": 0},
            "operation_id": "h",
            "operation": "hello",
            "request_deadline": NOW + 1,
            "payload": {
                "protocol": {"min": {"major": min, "minor": 0}, "max": {"major": max, "minor": 0}},
                "client_version": "1",
                "repo_root": shared.repo_root,
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

    fn authenticated(shared: &Shared) -> Session {
        let mut session = Session::new();
        let reply = session.handle(shared, &hello_body(shared, "secret", 1, 1), NOW);
        assert_eq!(reply.response.status, Status::Ok);
        session
    }

    #[test]
    fn hello_with_the_token_negotiates_the_version_and_names_the_server() {
        let shared = shared();

        let reply = Session::new().handle(&shared, &hello_body(&shared, "secret", 1, 1), NOW);

        let result = reply.response.result.unwrap();
        assert_eq!(result["protocol"], json!({"major": 1, "minor": 0}));
        assert_eq!(result["server_version"], "9.9.9");
        assert_eq!(result["store_id"], Value::Null);
        assert!(!reply.close);
    }

    #[test]
    fn a_wrong_token_is_unauthorized_and_closes_the_connection() {
        let shared = shared();

        let reply = Session::new().handle(&shared, &hello_body(&shared, "guess", 1, 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnauthorizedPeer);
        assert!(reply.close);
    }

    #[test]
    fn an_incompatible_major_is_protocol_unsupported_with_both_versions() {
        let shared = shared();

        let reply = Session::new().handle(&shared, &hello_body(&shared, "secret", 2, 2), NOW);

        assert_eq!(code(&reply), ErrorCode::ProtocolUnsupported);
        let error = reply.response.error.unwrap();
        assert_eq!(error.facts["server"], json!({"major": 1, "minor": 0}));
        assert!(error.remedy.contains("update harness"));
        assert!(reply.close);
    }

    #[test]
    fn hello_for_another_repository_is_refused() {
        let mut shared = shared();
        shared.repo_root = scratch_repository("other");

        let reply = Session::new().handle(&shared, &hello_body(&shared, "secret", 1, 1), NOW);

        assert_eq!(code(&reply), ErrorCode::PolicyDenied);
    }

    #[test]
    fn hello_from_a_path_that_is_no_repository_is_refused() {
        let mut shared = shared();
        shared.repo_root = PathBuf::from("/nonexistent-repo");

        let reply = Session::new().handle(&shared, &hello_body(&shared, "secret", 1, 1), NOW);

        assert_eq!(code(&reply), ErrorCode::PolicyDenied);
    }

    #[test]
    fn hello_from_a_linked_worktree_is_the_same_repository() {
        let shared = shared();
        let common = shared.repo_root.join(".git");
        let git_dir = common.join("worktrees/linked");
        fs::create_dir_all(&git_dir).unwrap();
        fs::write(git_dir.join("commondir"), "../..\n").unwrap();
        let linked = shared.repo_root.parent().unwrap().join("linked");
        fs::create_dir_all(&linked).unwrap();
        fs::write(
            linked.join(".git"),
            format!("gitdir: {}\n", git_dir.display()),
        )
        .unwrap();
        let mut asker = shared.clone();
        asker.repo_root = linked;

        let reply = Session::new().handle(&shared, &hello_body(&asker, "secret", 1, 1), NOW);

        assert_eq!(reply.response.status, Status::Ok);
    }

    #[test]
    fn a_request_before_hello_is_unauthorized() {
        let reply = Session::new().handle(&shared(), &status_body(1, "status", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnauthorizedPeer);
        assert!(reply.close);
    }

    #[test]
    fn an_authenticated_status_request_is_served() {
        let shared = shared();
        let mut session = authenticated(&shared);

        let reply = session.handle(&shared, &status_body(1, "status", NOW + 1), NOW);

        assert_eq!(reply.response.status, Status::Ok);
        assert_eq!(reply.response.operation_id, "s");
        assert_eq!(reply.response.result.unwrap()["state"], "serving");
    }

    #[test]
    fn an_unknown_operation_is_named() {
        let shared = shared();
        let mut session = authenticated(&shared);

        let reply = session.handle(&shared, &status_body(1, "launch", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::UnknownOperation);
        assert!(!reply.close);
    }

    #[test]
    fn another_major_after_hello_is_protocol_unsupported() {
        let shared = shared();
        let mut session = authenticated(&shared);

        let reply = session.handle(&shared, &status_body(2, "status", NOW + 1), NOW);

        assert_eq!(code(&reply), ErrorCode::ProtocolUnsupported);
    }

    #[test]
    fn an_unknown_request_field_is_malformed_and_keeps_the_operation_id() {
        let shared = shared();
        let mut session = authenticated(&shared);
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
        let reply = Session::new().handle(&shared(), b"\xff not json", NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
        assert_eq!(reply.response.operation_id, "");
    }

    #[test]
    fn a_passed_request_deadline_is_deadline_exceeded() {
        let shared = shared();
        let mut session = authenticated(&shared);

        let reply = session.handle(&shared, &status_body(1, "status", NOW - 1), NOW);

        assert_eq!(code(&reply), ErrorCode::DeadlineExceeded);
    }

    #[test]
    fn a_hello_without_a_payload_is_malformed() {
        let body = status_body(1, "hello", NOW + 1);

        let reply = Session::new().handle(&shared(), &body, NOW);

        assert_eq!(code(&reply), ErrorCode::MalformedRequest);
    }
}
