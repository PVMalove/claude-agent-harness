//! The request envelope, the response, `hello` and protocol version negotiation (ADR 0021,
//! section 3).

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::{ErrorCode, FailureDomain};

/// A protocol version. Peers with a common major are compatible; a minor adds only optional fields.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProtocolVersion {
    pub major: u32,
    pub minor: u32,
}

/// The version this build speaks.
pub const PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion { major: 1, minor: 5 };

/// The versions a client accepts: from `min` to `max`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VersionRange {
    pub min: ProtocolVersion,
    pub max: ProtocolVersion,
}

/// Choose the version of a connection, or `None` when the peers share no major.
///
/// The server's major must lie in the client's range. The minor is the lower of the two, as
/// far as the client's range names one for that major.
pub fn negotiate(server: ProtocolVersion, client: VersionRange) -> Option<ProtocolVersion> {
    if client.min > client.max || server.major < client.min.major || server.major > client.max.major
    {
        return None;
    }
    let minor = if server.major == client.max.major {
        server.minor.min(client.max.minor)
    } else {
        server.minor
    };
    Some(ProtocolVersion {
        major: server.major,
        minor,
    })
}

/// A request. The server refuses a field it does not know (`malformed_request`).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub protocol_version: ProtocolVersion,
    pub operation_id: String,
    pub operation: String,
    /// UTC milliseconds since the Unix epoch: the deadline of the answer, not of the execution.
    pub request_deadline: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub batch_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dispatch_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub candidate_sha: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plan_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fencing_token: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload: Option<Value>,
}

/// The payload of the `hello` operation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HelloPayload {
    pub protocol: VersionRange,
    pub client_version: String,
    pub repo_root: String,
    pub token: String,
}

/// The result of a successful `hello`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct HelloResult {
    pub protocol: ProtocolVersion,
    pub server_version: String,
    /// The identity of the state store; `None` while the runtime serves no store.
    pub store_id: Option<String>,
    pub features: Vec<String>,
}

/// Whether a response carries a result or an error.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Ok,
    Error,
}

/// The error of a response.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorBody {
    pub code: ErrorCode,
    pub domain: FailureDomain,
    pub retryable: bool,
    pub message: String,
    pub remedy: String,
    pub facts: Map<String, Value>,
}

impl ErrorBody {
    /// An error without facts.
    pub fn new(
        code: ErrorCode,
        domain: FailureDomain,
        retryable: bool,
        message: impl Into<String>,
        remedy: impl Into<String>,
    ) -> Self {
        Self {
            code,
            domain,
            retryable,
            message: message.into(),
            remedy: remedy.into(),
            facts: Map::new(),
        }
    }

    /// The same error with one more fact.
    #[must_use]
    pub fn with_fact(mut self, name: &str, value: impl Into<Value>) -> Self {
        self.facts.insert(name.to_owned(), value.into());
        self
    }
}

/// A response. A client ignores a field it does not know.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Response {
    pub operation_id: String,
    pub status: Status,
    pub revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorBody>,
}

impl Response {
    /// A successful response.
    pub fn ok(operation_id: impl Into<String>, result: Value) -> Self {
        Self {
            operation_id: operation_id.into(),
            status: Status::Ok,
            revision: None,
            result: Some(result),
            error: None,
        }
    }

    /// A failed response.
    pub fn error(operation_id: impl Into<String>, error: ErrorBody) -> Self {
        Self {
            operation_id: operation_id.into(),
            status: Status::Error,
            revision: None,
            result: None,
            error: Some(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::{decode, encode};

    const fn version(major: u32, minor: u32) -> ProtocolVersion {
        ProtocolVersion { major, minor }
    }

    const fn range(min: ProtocolVersion, max: ProtocolVersion) -> VersionRange {
        VersionRange { min, max }
    }

    #[test]
    fn a_common_major_is_negotiated_with_the_lower_minor() {
        assert_eq!(
            negotiate(version(1, 3), range(version(1, 0), version(1, 1))),
            Some(version(1, 1))
        );
        assert_eq!(
            negotiate(version(1, 0), range(version(1, 0), version(1, 4))),
            Some(version(1, 0))
        );
        assert_eq!(
            negotiate(version(1, 2), range(version(1, 0), version(2, 0))),
            Some(version(1, 2))
        );
    }

    #[test]
    fn an_incompatible_major_is_not_negotiated() {
        assert_eq!(
            negotiate(version(1, 0), range(version(2, 0), version(2, 5))),
            None
        );
        assert_eq!(
            negotiate(version(3, 0), range(version(1, 0), version(2, 5))),
            None
        );
    }

    #[test]
    fn an_inverted_range_is_not_negotiated() {
        assert_eq!(
            negotiate(version(1, 0), range(version(1, 4), version(1, 0))),
            None
        );
    }

    #[test]
    fn the_server_refuses_an_unknown_request_field() {
        let body = json!({
            "protocol_version": {"major": 1, "minor": 0},
            "operation_id": "op",
            "operation": "status",
            "request_deadline": 1,
            "surprise": true,
        });

        assert!(decode::<Request>(&encode(&body).unwrap()).is_err());
    }

    #[test]
    fn a_request_needs_its_required_fields_only() {
        let body = json!({
            "protocol_version": {"major": 1, "minor": 0},
            "operation_id": "op",
            "operation": "status",
            "request_deadline": 1,
        });

        let request: Request = decode(&encode(&body).unwrap()).unwrap();

        assert_eq!(request.operation, "status");
        assert_eq!(request.payload, None);
    }

    #[test]
    fn hello_refuses_an_unknown_payload_field() {
        let payload = json!({
            "protocol": {"min": {"major": 1, "minor": 0}, "max": {"major": 1, "minor": 0}},
            "client_version": "1",
            "repo_root": "/r",
            "token": "t",
            "extra": 1,
        });

        assert!(decode::<HelloPayload>(&encode(&payload).unwrap()).is_err());
    }

    #[test]
    fn a_client_ignores_an_unknown_response_field() {
        let body = json!({
            "operation_id": "op",
            "status": "ok",
            "revision": null,
            "result": {"a": 1},
            "added_in_a_later_minor": [1, 2],
        });

        let response: Response = decode(&encode(&body).unwrap()).unwrap();

        assert_eq!(response.status, Status::Ok);
        assert_eq!(response.result, Some(json!({"a": 1})));
    }

    #[test]
    fn an_error_response_carries_the_code_domain_and_remedy() {
        let error = ErrorBody::new(
            ErrorCode::ProtocolUnsupported,
            FailureDomain::Tooling,
            false,
            "no common major",
            "update harness or the runtime",
        )
        .with_fact("server", "1.0");

        let wire = encode(&Response::error("op", error)).unwrap();
        let text = String::from_utf8(wire.clone()).unwrap();

        assert!(text.contains("\"code\":\"protocol_unsupported\""));
        assert!(text.contains("\"domain\":\"tooling\""));
        let back: Response = decode(&wire).unwrap();
        assert_eq!(back.error.unwrap().facts["server"], "1.0");
    }
}
