//! The QA runs of `harness-runtime` (ADR 0022, section 3; ADR 0023, section 5).
//!
//! A QA run is a sequence of command groups the coordinator's policy chose: each group runs
//! `always`, only `after-failure` of an earlier group or only `after-success` of every earlier
//! group, and a group that stops on failure leaves its later commands `not_run`. Every command
//! runs in its own foreground `run --rm --init` container from the pinned image; its standard
//! output and error go to two files whose sha256 and size the record names, and its exit code is
//! always the real one. The classification only adds the failure domain: who is to blame for a
//! code that is not the command's own result.
//!
//! This crate holds no store and no IPC: the supervisor admits a run, records it and calls
//! [`runner::Runner::run`] on its own thread.

pub mod classify;
pub mod plan;
pub mod runner;
