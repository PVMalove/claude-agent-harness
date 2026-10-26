//! Entry point of the `harness-runtime` process (ADR 0021, section 8).
//!
//! `harness-runtime migrate [--repo <path>]` cuts the state of a repository over from the file
//! ledger to the runtime store (ADR 0020, section 5). It prints one JSON line and exits with 0
//! (done, or already done), 2 (a precondition refused it, nothing changed), 3 (it failed after it
//! began; the file ledger is still the authority) or 4 (an invariant broke; an operator must look).
//!
//! `harness-runtime serve [--repo <path>]` serves the local IPC of one repository until it gets
//! SIGINT or SIGTERM (on Windows, Ctrl+C or Ctrl+Break). On Windows the `socket` key of the
//! readiness line holds the name of the named pipe. ADR 0021, section 8 defines exit code 0 (a
//! normal stop) and 2 (a refusal to start). The ADR leaves a failure of a server that is already
//! ready undefined, so this implementation picks 1 for it. A supervisor learns that the start
//! worked from the readiness line, so a failure to write that line is a refusal to start (2), not
//! a failure of a running server.

use std::env;
use std::ffi::OsString;
use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use contracts::ErrorCode;
use ipc::{Server, resolve_paths};
use serde_json::json;

const EXIT_FAILED: u8 = 1;
const EXIT_REFUSED: u8 = 2;
const USAGE: &str = "usage: harness-runtime serve|migrate [--repo <path>]";

/// What the command line asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Command {
    Serve,
    Migrate,
}

/// The command and the repository it works on.
#[derive(Debug, PartialEq, Eq)]
struct Args {
    command: Command,
    repo: PathBuf,
}

fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Args, String> {
    let mut args = args.into_iter();
    let command = match args.next().as_deref().and_then(|word| word.to_str()) {
        Some("serve") => Command::Serve,
        Some("migrate") => Command::Migrate,
        _ => return Err(USAGE.to_owned()),
    };
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
    Ok(Args { command, repo })
}

/// What an error line and its `REMEDY:` line say. The remedy line starts with the protocol error
/// code in brackets (ADR 0021, section 8).
#[derive(Debug)]
struct Report {
    code: ErrorCode,
    message: String,
    remedy: String,
}

impl Report {
    fn new(code: ErrorCode, message: impl ToString, remedy: impl ToString) -> Self {
        Self {
            code,
            message: message.to_string(),
            remedy: remedy.to_string(),
        }
    }

    /// Print the `ERROR:` line and its `REMEDY:` line, and give back the exit code.
    fn print(&self, exit: u8) -> ExitCode {
        eprintln!("ERROR: {}", self.message);
        eprintln!("REMEDY: [{}] {}", self.code.as_str(), self.remedy);
        ExitCode::from(exit)
    }
}

/// Why `serve` ended with an error: it never reported ready, or it failed after it was ready.
#[derive(Debug)]
enum Failure {
    Refused(Report),
    Failed(Report),
}

fn serve(args: &Args) -> Result<(), Failure> {
    let xdg = env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from);
    let paths = resolve_paths(&args.repo, xdg.as_deref())
        .map_err(|err| Failure::Refused(Report::new(err.code(), &err, err.remedy())))?;
    // The handler comes before the bind: a signal then never kills a bound server without
    // `Drop` removing its socket and token.
    let stop = Arc::new(AtomicBool::new(false));
    let signalled = Arc::clone(&stop);
    ctrlc::set_handler(move || signalled.store(true, Ordering::Relaxed)).map_err(|err| {
        Failure::Refused(Report::new(
            ErrorCode::Internal,
            format!("cannot install the signal handler: {err}"),
            "run the runtime in a process that may handle SIGINT and SIGTERM",
        ))
    })?;
    let state_dir = args.repo.join(state::migrate::STATE_REL);
    // A store that an older release wrote is brought to this schema before the first request; a
    // store of a newer schema is refused, and nothing is written.
    state::schema::upgrade_store(&state_dir)
        .map_err(|err| Failure::Refused(Report::new(err.code(), &err, err.remedy())))?;
    let server = Server::bind_serving(paths, Some(state_dir))
        .map_err(|err| Failure::Refused(Report::new(err.code(), &err, err.remedy())))?;
    let ready = json!({
        "status": "ready",
        "socket": server.paths().socket,
        "token_file": server.paths().token,
        "repo_id": server.paths().repo_id,
    });
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{ready}")
        .and_then(|()| stdout.flush())
        .map_err(|err| {
            Failure::Refused(Report::new(
                ErrorCode::Internal,
                format!("cannot report readiness: {err}"),
                "keep the standard output of the runtime open, then repeat the command",
            ))
        })?;
    drop(stdout);
    server.serve_until(&stop).map_err(|err| {
        Failure::Failed(Report::new(
            ErrorCode::Internal,
            format!("the accept loop failed: {err}"),
            "start `harness-runtime serve --repo <repo>` again",
        ))
    })
}

/// Run the migration and print its outcome as one JSON line.
fn migrate(args: &Args) -> ExitCode {
    match state::migrate::run(&args.repo) {
        Ok(outcome) => {
            let mut stdout = std::io::stdout().lock();
            match writeln!(stdout, "{}", outcome.to_json()).and_then(|()| stdout.flush()) {
                Ok(()) => ExitCode::SUCCESS,
                Err(err) => Report::new(
                    ErrorCode::Internal,
                    format!("the migration is done but its result cannot be written: {err}"),
                    "repeat `harness-runtime migrate --repo <repo>`: a repeat after the commit \
                     point answers already_cut_over",
                )
                .print(EXIT_FAILED),
            }
        }
        Err(error) => Report::new(error.code(), &error, error.remedy()).print(error.exit_code()),
    }
}

fn main() -> ExitCode {
    let args = match parse_args(env::args_os().skip(1)) {
        Ok(args) => args,
        Err(message) => {
            return Report::new(
                ErrorCode::MalformedRequest,
                message,
                "pass `serve` or `migrate`, with an optional `--repo <path>`",
            )
            .print(EXIT_REFUSED);
        }
    };
    if args.command == Command::Migrate {
        return migrate(&args);
    }
    match serve(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(Failure::Refused(report)) => report.print(EXIT_REFUSED),
        Err(Failure::Failed(report)) => report.print(EXIT_FAILED),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(words: &[&str]) -> Result<Args, String> {
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
    fn migrate_takes_the_repository_from_the_flag() {
        let args = parse(&["migrate", "--repo", "/r"]).unwrap();

        assert_eq!(args.command, Command::Migrate);
        assert_eq!(args.repo, PathBuf::from("/r"));
    }

    #[test]
    fn serve_defaults_to_the_working_directory() {
        assert_eq!(parse(&["serve"]).unwrap().repo, env::current_dir().unwrap());
    }

    #[test]
    fn anything_else_is_refused_with_the_usage() {
        assert!(parse(&[]).unwrap_err().contains("usage"));
        assert!(parse(&["rollback"]).unwrap_err().contains("usage"));
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
