//! Entry point of the `harness-runtime` process (ADR 0021, section 8).
//!
//! `harness-runtime migrate [--repo <path>]` cuts the state of a repository over from the file
//! ledger to the runtime store (ADR 0020, section 5). It prints one JSON line and exits with 0
//! (done, or already done), 2 (a precondition refused it, nothing changed), 3 (it failed after it
//! began; the file ledger is still the authority) or 4 (an invariant broke; an operator must look).
//!
//! `harness-runtime rollback --to-legacy [--repo <path>] --approved-by <human> --approved-at <RFC
//! 3339 time> --reason <text>` exports the runtime store into a new generation of the file ledger
//! and returns the authority to it (ADR 0020, section 5). It holds the instance lock of `serve`
//! for its whole run, so a running `serve` refuses it, prints one JSON line and uses the exit codes
//! of `migrate`.
//!
//! `harness-runtime serve [--repo <path>]` serves the local IPC of one repository until it gets
//! SIGINT or SIGTERM (on Windows, Ctrl+C or Ctrl+Break). Before it reports ready it reconciles the
//! executions of the store, and a stop never signals an execution: they outlive the runtime. On Windows the `socket` key of the
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
use ipc::{Server, StartError, resolve_paths};
use serde_json::json;

const EXIT_FAILED: u8 = 1;
const EXIT_REFUSED: u8 = 2;
const USAGE: &str = "usage: harness-runtime serve|migrate [--repo <path>] | harness-runtime \
    rollback --to-legacy [--repo <path>] --approved-by <human> --approved-at <time> --reason <text>";

/// What the command line asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Command {
    Serve,
    Migrate,
    Rollback,
}

/// The command and the repository it works on.
#[derive(Debug, PartialEq, Eq)]
struct Args {
    command: Command,
    repo: PathBuf,
    /// The approval of `rollback`; blank fields are refused by the rollback itself.
    approval: state::rollback::Approval,
}

fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Args, String> {
    let mut args = args.into_iter();
    let command = match args.next().as_deref().and_then(|word| word.to_str()) {
        Some("serve") => Command::Serve,
        Some("migrate") => Command::Migrate,
        Some("rollback") => Command::Rollback,
        _ => return Err(USAGE.to_owned()),
    };
    let mut repo = None;
    let mut to_legacy = false;
    let mut approval = state::rollback::Approval {
        approved_by: String::new(),
        approved_at: String::new(),
        reason: String::new(),
    };
    while let Some(flag) = args.next() {
        let rollback = command == Command::Rollback;
        let field = match flag.to_str() {
            Some("--repo") => None,
            Some("--to-legacy") if rollback => {
                to_legacy = true;
                continue;
            }
            Some("--approved-by") if rollback => Some(&mut approval.approved_by),
            Some("--approved-at") if rollback => Some(&mut approval.approved_at),
            Some("--reason") if rollback => Some(&mut approval.reason),
            _ => return Err(format!("unknown argument {flag:?}; {USAGE}")),
        };
        let what = if field.is_none() { "a path" } else { "a value" };
        let value = args
            .next()
            .ok_or_else(|| format!("{} needs {what}; {USAGE}", flag.to_string_lossy()))?;
        match field {
            None => repo = Some(PathBuf::from(value)),
            Some(field) => {
                *field = value
                    .into_string()
                    .map_err(|value| format!("{value:?} is not UTF-8; {USAGE}"))?;
            }
        }
    }
    if command == Command::Rollback && !to_legacy {
        return Err(format!("rollback needs --to-legacy; {USAGE}"));
    }
    let repo = match repo {
        Some(repo) => repo,
        None => {
            env::current_dir().map_err(|err| format!("cannot read the working directory: {err}"))?
        }
    };
    Ok(Args {
        command,
        repo,
        approval,
    })
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
    // With the instance lock held, the executions a dead runtime left are adopted or recorded
    // lost before the first request (ADR 0022, section 2); then the periodic reconcile starts.
    server.supervisor().reconcile_all();
    server.supervisor().start(Arc::clone(&stop));
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

/// Print what a rollback did as one JSON line, or the failure with the remedy of the rollback.
fn rollback(args: &Args) -> ExitCode {
    let refuse = |code: ErrorCode, message: String, remedy: &str| {
        Report::new(code, message, remedy).print(EXIT_REFUSED)
    };
    if let Err(error) = state::rollback::check_approval(&args.approval) {
        return refuse(
            error.code(),
            error.to_string(),
            state::rollback::remedy(&error),
        );
    }
    let xdg = env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from);
    let paths = match resolve_paths(&args.repo, xdg.as_deref()) {
        Ok(paths) => paths,
        Err(err) => return refuse(err.code(), err.to_string(), err.remedy()),
    };
    // The instance lock of `serve`: no runtime serves the store while the rollback exports it.
    let _instance = match ipc::hold_instance_lock(&paths) {
        Ok(lock) => lock,
        Err(err @ StartError::AlreadyRunning(_)) => {
            return refuse(
                err.code(),
                err.to_string(),
                "stop `harness-runtime serve` of this repository first, then repeat the rollback",
            );
        }
        Err(err) => return refuse(err.code(), err.to_string(), err.remedy()),
    };
    match state::rollback::run(&args.repo, &args.approval) {
        Ok(outcome) => {
            let mut stdout = std::io::stdout().lock();
            match writeln!(stdout, "{}", outcome.to_json()).and_then(|()| stdout.flush()) {
                Ok(()) => ExitCode::SUCCESS,
                Err(err) => Report::new(
                    ErrorCode::Internal,
                    format!("the rollback is done but its result cannot be written: {err}"),
                    "repeat the rollback: a repeat after the commit point answers \
                     already_rolled_back",
                )
                .print(EXIT_FAILED),
            }
        }
        Err(error) => Report::new(error.code(), &error, state::rollback::remedy(&error))
            .print(error.exit_code()),
    }
}

fn main() -> ExitCode {
    let args = match parse_args(env::args_os().skip(1)) {
        Ok(args) => args,
        Err(message) => {
            return Report::new(
                ErrorCode::MalformedRequest,
                message,
                "pass `serve` or `migrate` with an optional `--repo <path>`, or `rollback \
                 --to-legacy` with `--approved-by`, `--approved-at` and `--reason`",
            )
            .print(EXIT_REFUSED);
        }
    };
    match args.command {
        Command::Migrate => return migrate(&args),
        Command::Rollback => return rollback(&args),
        Command::Serve => {}
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
    fn rollback_takes_the_approval_from_its_flags() {
        let args = parse(&[
            "rollback",
            "--to-legacy",
            "--repo",
            "/r",
            "--approved-by",
            "human:a",
            "--approved-at",
            "2026-10-27T10:00:00+00:00",
            "--reason",
            "back to 1.x",
        ])
        .unwrap();

        assert_eq!(args.command, Command::Rollback);
        assert_eq!(args.repo, PathBuf::from("/r"));
        assert_eq!(args.approval.approved_by, "human:a");
        assert_eq!(args.approval.approved_at, "2026-10-27T10:00:00+00:00");
        assert_eq!(args.approval.reason, "back to 1.x");
    }

    #[test]
    fn serve_defaults_to_the_working_directory() {
        assert_eq!(parse(&["serve"]).unwrap().repo, env::current_dir().unwrap());
    }

    #[test]
    fn anything_else_is_refused_with_the_usage() {
        assert!(parse(&[]).unwrap_err().contains("usage"));
        assert!(parse(&["rewind"]).unwrap_err().contains("usage"));
        assert!(parse(&["rollback"]).unwrap_err().contains("--to-legacy"));
        assert!(
            parse(&["migrate", "--reason", "x"])
                .unwrap_err()
                .contains("unknown argument")
        );
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
