//! Entry point of the `harness-runtime` process (ADR 0021, section 8).
//!
//! `harness-runtime serve [--repo <path>]` serves the local IPC of one repository until it gets
//! SIGINT or SIGTERM. Exit code 0 is a normal stop, 2 is a refusal to start and 1 is a failure
//! of the running server (ADR 0021, section 8).

use std::env;
use std::ffi::OsString;
use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use ipc::{Server, resolve_paths};
use serde_json::json;

const EXIT_FAILED: u8 = 1;
const EXIT_REFUSED: u8 = 2;
const USAGE: &str = "usage: harness-runtime serve [--repo <path>]";

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
struct ServeArgs {
    repo: PathBuf,
}

fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<ServeArgs, String> {
    let mut args = args.into_iter();
    match args.next().as_deref().and_then(|word| word.to_str()) {
        Some("serve") => {}
        _ => return Err(USAGE.to_owned()),
    }
    let mut repo = None;
    while let Some(flag) = args.next() {
        if flag != "--repo" {
            return Err(format!("unknown argument {flag:?}; {USAGE}"));
        }
        let value = args
            .next()
            .ok_or_else(|| format!("--repo needs a path; {USAGE}"))?;
        repo = Some(PathBuf::from(value));
    }
    let repo = match repo {
        Some(repo) => repo,
        None => {
            env::current_dir().map_err(|err| format!("cannot read the working directory: {err}"))?
        }
    };
    Ok(ServeArgs { repo })
}

/// Why `serve` ended with an error: it never started, or it failed after it was ready.
#[derive(Debug)]
enum Failure {
    Refused(String),
    Failed(String),
}

fn serve(args: &ServeArgs) -> Result<(), Failure> {
    let refused = |err: &dyn ToString| Failure::Refused(err.to_string());
    let xdg = env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from);
    let paths = resolve_paths(&args.repo, xdg.as_deref()).map_err(|err| refused(&err))?;
    // The handler comes before the bind: a signal then never kills a bound server without
    // `Drop` removing its socket and token.
    let stop = Arc::new(AtomicBool::new(false));
    let signalled = Arc::clone(&stop);
    ctrlc::set_handler(move || signalled.store(true, Ordering::Relaxed))
        .map_err(|err| Failure::Refused(format!("cannot install the signal handler: {err}")))?;
    let server = Server::bind(&args.repo, paths).map_err(|err| refused(&err))?;
    let ready = json!({
        "status": "ready",
        "socket": server.paths().socket,
        "token_file": server.paths().token,
        "repo_id": server.paths().repo_id,
    });
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{ready}")
        .and_then(|()| stdout.flush())
        .map_err(|err| Failure::Failed(format!("cannot report readiness: {err}")))?;
    drop(stdout);
    server
        .serve_until(&stop)
        .map_err(|err| Failure::Failed(format!("the accept loop failed: {err}")))
}

fn main() -> ExitCode {
    let outcome = parse_args(env::args_os().skip(1))
        .map_err(Failure::Refused)
        .and_then(|args| serve(&args));
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(Failure::Refused(message)) => {
            eprintln!("ERROR: {message}");
            ExitCode::from(EXIT_REFUSED)
        }
        Err(Failure::Failed(message)) => {
            eprintln!("ERROR: {message}");
            ExitCode::from(EXIT_FAILED)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(words: &[&str]) -> Result<ServeArgs, String> {
        parse_args(words.iter().map(OsString::from))
    }

    #[test]
    fn serve_takes_the_repository_from_the_flag() {
        assert_eq!(
            parse(&["serve", "--repo", "/r"]).unwrap().repo,
            PathBuf::from("/r")
        );
    }

    #[test]
    fn serve_defaults_to_the_working_directory() {
        assert_eq!(parse(&["serve"]).unwrap().repo, env::current_dir().unwrap());
    }

    #[test]
    fn anything_else_is_refused_with_the_usage() {
        assert!(parse(&[]).unwrap_err().contains("usage"));
        assert!(parse(&["migrate"]).unwrap_err().contains("usage"));
        assert!(
            parse(&["serve", "--socket", "x"])
                .unwrap_err()
                .contains("unknown argument")
        );
        assert!(
            parse(&["serve", "--repo"])
                .unwrap_err()
                .contains("needs a path")
        );
    }
}
