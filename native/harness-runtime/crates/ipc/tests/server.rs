//! The IPC server over a real Unix domain socket or Windows named pipe: permissions, `hello`,
//! errors and shutdown. The protocol tests run unchanged on both transports.

use std::fs;
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use contracts::{
    ErrorCode, MAX_FRAME_LEN, Response, Status, decode, encode, read_frame, write_frame,
};
use ipc::{PeerId, Server, StartError, resolve_paths};
use serde_json::{Value, json};

/// The client end of the transport under test.
#[cfg(unix)]
type Client = UnixStream;
#[cfg(windows)]
type Client = platform_ffi::PipeConnection;

/// The identity of the user that runs these tests.
#[cfg(unix)]
fn own_peer() -> PeerId {
    nix::unistd::getuid().as_raw()
}

#[cfg(windows)]
fn own_peer() -> PeerId {
    platform_ffi::current_user_sid().unwrap()
}

/// An identity that no test client has.
#[cfg(unix)]
fn foreign_peer() -> PeerId {
    own_peer().wrapping_add(1)
}

#[cfg(windows)]
fn foreign_peer() -> PeerId {
    // The SID of the "Nobody" authority: it is never the SID of a logon user.
    "S-1-0-0".to_owned()
}

struct Fixture {
    root: PathBuf,
    xdg: PathBuf,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let base = std::env::temp_dir().join(format!("hr-ipc-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let root = base.join("repo");
        let xdg = base.join("run");
        fs::create_dir_all(root.join(".git")).unwrap();
        fs::create_dir_all(&xdg).unwrap();
        Self {
            root: fs::canonicalize(root).unwrap(),
            xdg: fs::canonicalize(xdg).unwrap(),
        }
    }

    fn bind(&self) -> Result<Server, StartError> {
        let paths = resolve_paths(&self.root, Some(&self.xdg)).unwrap();
        Server::bind(paths)
    }
}

struct Running {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    socket: PathBuf,
    token: String,
}

impl Running {
    fn start(server: Server) -> Self {
        Self::spawn(server, None)
    }

    /// Serve clients that run as `uid`; another uid makes every real client a foreign peer.
    fn start_as(server: Server, peer: PeerId) -> Self {
        Self::spawn(server, Some(peer))
    }

    fn spawn(server: Server, peer: Option<PeerId>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let socket = server.paths().socket.clone();
        let token = fs::read_to_string(&server.paths().token).unwrap();
        let flag = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            match peer {
                Some(peer) => server.serve_until_as(&flag, peer),
                None => server.serve_until(&flag),
            }
            .unwrap();
        });
        Self {
            stop,
            thread: Some(thread),
            socket,
            token,
        }
    }

    #[cfg(unix)]
    fn connect(&self) -> Client {
        let stream = UnixStream::connect(&self.socket).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream
    }

    #[cfg(windows)]
    fn connect(&self) -> Client {
        let timeout = Duration::from_secs(5);
        let mut pipe =
            platform_ffi::connect_client(self.socket.to_str().unwrap(), timeout).unwrap();
        pipe.set_timeout(Some(timeout));
        pipe
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let served = thread.join();
            // A second panic during unwinding aborts the process and hides the failed assertion.
            if !thread::panicking() {
                served.unwrap();
            }
        }
    }
}

fn now_ms() -> u64 {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

fn request(operation: &str, payload: Option<Value>) -> Value {
    let mut body = json!({
        "protocol_version": {"major": 1, "minor": 0},
        "operation_id": format!("{operation}-1"),
        "operation": operation,
        "request_deadline": now_ms() + 10_000,
    });
    if let Some(payload) = payload {
        body["payload"] = payload;
    }
    body
}

fn hello(root: &Path, token: &str, min: u32, max: u32) -> Value {
    request(
        "hello",
        Some(json!({
            "protocol": {"min": {"major": min, "minor": 0}, "max": {"major": max, "minor": 0}},
            "client_version": "test",
            "repo_root": root,
            "token": token,
        })),
    )
}

fn exchange(stream: &mut (impl Read + Write), body: &Value) -> Response {
    write_frame(stream, &encode(body).unwrap()).unwrap();
    decode(&read_frame(stream).unwrap()).unwrap()
}

fn error_code(response: &Response) -> ErrorCode {
    assert_eq!(response.status, Status::Error);
    response.error.as_ref().unwrap().code
}

#[cfg(unix)]
fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

#[cfg(unix)]
#[test]
fn the_directory_the_socket_and_the_token_are_private_to_the_owner() {
    let fixture = Fixture::new("modes");
    let server = fixture.bind().unwrap();
    let paths = server.paths().clone();

    assert_eq!(mode(&paths.dir), 0o700);
    assert_eq!(mode(&paths.socket), 0o600);
    assert_eq!(mode(&paths.token), 0o600);
    assert_eq!(fs::read_to_string(&paths.token).unwrap().len(), 64);
}

#[cfg(windows)]
#[test]
fn the_token_file_is_private_to_the_user() {
    let fixture = Fixture::new("modes");
    let server = fixture.bind().unwrap();
    let paths = server.paths().clone();

    let dacl = platform_ffi::dacl_of_path(&paths.token).unwrap();

    assert!(dacl.protected, "{dacl:?}");
    assert_eq!(dacl.aces.len(), 1, "{dacl:?}");
    assert_eq!(dacl.aces[0].kind, "A");
    assert_eq!(dacl.aces[0].trustee, own_peer());
    assert_eq!(fs::read_to_string(&paths.token).unwrap().len(), 64);
}

#[cfg(windows)]
#[test]
fn the_pipe_is_named_after_the_repository() {
    let fixture = Fixture::new("name");
    let server = fixture.bind().unwrap();

    assert_eq!(
        server.paths().socket,
        PathBuf::from(ipc::pipe_name(&server.paths().repo_id))
    );
}

#[test]
fn hello_with_the_token_then_status() {
    let fixture = Fixture::new("hello");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    let greeting = exchange(&mut stream, &hello(&fixture.root, &runtime.token, 1, 1));
    let status = exchange(&mut stream, &request("status", None));

    assert_eq!(greeting.status, Status::Ok);
    assert_eq!(
        greeting.result.unwrap()["protocol"],
        json!({"major": 1, "minor": 0})
    );
    assert_eq!(status.status, Status::Ok);
    assert_eq!(status.result.unwrap()["state"], "serving");
}

#[test]
fn a_wrong_token_is_unauthorized_and_the_connection_closes() {
    let fixture = Fixture::new("token");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    let answer = exchange(&mut stream, &hello(&fixture.root, "not-the-token", 1, 1));

    assert_eq!(error_code(&answer), ErrorCode::UnauthorizedPeer);
    assert!(read_frame(&mut stream).is_err());
}

#[test]
fn an_incompatible_major_is_protocol_unsupported() {
    let fixture = Fixture::new("major");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    let answer = exchange(&mut stream, &hello(&fixture.root, &runtime.token, 2, 2));

    assert_eq!(error_code(&answer), ErrorCode::ProtocolUnsupported);
}

#[test]
fn an_unknown_field_is_malformed_and_the_connection_stays_usable() {
    let fixture = Fixture::new("field");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();
    let mut body = request("status", None);
    body["surprise"] = json!(1);

    let refused = exchange(&mut stream, &body);
    let greeting = exchange(&mut stream, &hello(&fixture.root, &runtime.token, 1, 1));

    assert_eq!(error_code(&refused), ErrorCode::MalformedRequest);
    assert_eq!(greeting.status, Status::Ok);
}

#[test]
fn an_oversized_frame_is_refused_without_reading_it() {
    let fixture = Fixture::new("big");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    stream
        .write_all(&(MAX_FRAME_LEN + 1).to_be_bytes())
        .unwrap();
    let answer: Response = decode(&read_frame(&mut stream).unwrap()).unwrap();

    assert_eq!(error_code(&answer), ErrorCode::MalformedRequest);
}

#[test]
fn a_request_before_hello_is_unauthorized() {
    let fixture = Fixture::new("early");
    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    let answer = exchange(&mut stream, &request("status", None));

    assert_eq!(error_code(&answer), ErrorCode::UnauthorizedPeer);
}

#[test]
fn a_second_runtime_for_the_same_repository_is_refused() {
    let fixture = Fixture::new("rival");
    let _first = Running::start(fixture.bind().unwrap());

    assert!(matches!(fixture.bind(), Err(StartError::AlreadyRunning(_))));
}

#[cfg(unix)]
#[test]
fn a_stale_socket_file_is_replaced() {
    let fixture = Fixture::new("stale");
    let paths = resolve_paths(&fixture.root, Some(&fixture.xdg)).unwrap();
    fs::create_dir_all(&paths.dir).unwrap();
    // A runtime that was killed leaves its socket file behind; nothing listens on it.
    drop(UnixListener::bind(&paths.socket).unwrap());
    let socket = paths.socket;
    assert!(socket.exists());

    let runtime = Running::start(fixture.bind().unwrap());
    let mut stream = runtime.connect();

    assert!(socket.exists());
    assert_eq!(
        exchange(&mut stream, &hello(&fixture.root, &runtime.token, 1, 1)).status,
        Status::Ok
    );
}

#[test]
fn stopping_the_server_removes_the_socket_and_the_token() {
    let fixture = Fixture::new("stop");
    let server = fixture.bind().unwrap();
    let paths = server.paths().clone();

    drop(server);

    // A pipe name is no file: asking the file system about it would connect to the pipe.
    #[cfg(unix)]
    assert!(!paths.socket.exists());
    assert!(!paths.token.exists());
}

#[test]
fn a_peer_of_another_user_is_refused_with_unauthorized_peer() {
    let fixture = Fixture::new("peer");
    let runtime = Running::start_as(fixture.bind().unwrap(), foreign_peer());
    let mut stream = runtime.connect();

    // The refusal comes before the client has sent anything, even with the right token.
    let answer: Response = decode(&read_frame(&mut stream).unwrap()).unwrap();

    assert_eq!(error_code(&answer), ErrorCode::UnauthorizedPeer);
    assert!(read_frame(&mut stream).is_err());
}

#[test]
fn simultaneous_starts_leave_one_runtime_with_its_own_token() {
    let fixture = Arc::new(Fixture::new("race"));
    let starters = 8;
    let barrier = Arc::new(Barrier::new(starters));
    let handles: Vec<_> = (0..starters)
        .map(|_| {
            let fixture = Arc::clone(&fixture);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                fixture.bind()
            })
        })
        .collect();
    let outcomes: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();

    let mut winners = Vec::new();
    for outcome in outcomes {
        match outcome {
            Ok(server) => winners.push(server),
            Err(err) => assert!(matches!(err, StartError::AlreadyRunning(_)), "{err}"),
        }
    }
    assert_eq!(winners.len(), 1);
    let runtime = Running::start(winners.pop().unwrap());
    let mut stream = runtime.connect();
    assert_eq!(
        exchange(&mut stream, &hello(&fixture.root, &runtime.token, 1, 1)).status,
        Status::Ok
    );
}

#[test]
#[should_panic(expected = "the test failed first")]
fn a_failing_test_keeps_its_message_when_the_server_thread_failed_too() {
    // A panic of the fixture destructor during unwinding aborts the whole test process, so the
    // message of the failing test would never be printed.
    let thread = thread::spawn(|| panic!("the server thread failed"));
    let _runtime = Running {
        stop: Arc::new(AtomicBool::new(false)),
        thread: Some(thread),
        socket: PathBuf::new(),
        token: String::new(),
    };

    panic!("the test failed first");
}
