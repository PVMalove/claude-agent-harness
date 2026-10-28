//! Win32 calls behind a safe API. This is the only module of the workspace with `unsafe` code.
//! Each `unsafe` block states why its pointers and handles are valid.

use std::ffi::{OsStr, c_void};
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{
    ERROR_ACCESS_DENIED, ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF, ERROR_INVALID_PARAMETER,
    ERROR_IO_PENDING, ERROR_NO_DATA, ERROR_OPERATION_ABORTED, ERROR_PIPE_BUSY,
    ERROR_PIPE_CONNECTED, ERROR_PIPE_NOT_CONNECTED, FILETIME, GENERIC_READ, GENERIC_WRITE, HANDLE,
    INVALID_HANDLE_VALUE, LocalFree, STILL_ACTIVE, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertSecurityDescriptorToStringSecurityDescriptorW, ConvertSidToStringSidW,
    ConvertStringSecurityDescriptorToSecurityDescriptorW, ConvertStringSidToSidW,
    GetNamedSecurityInfoW, GetSecurityInfo, SDDL_REVISION_1, SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::Cryptography::{
    BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
};
use windows_sys::Win32::Security::{
    DACL_SECURITY_INFORMATION, GetTokenInformation, PSECURITY_DESCRIPTOR, PSID,
    SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER, TokenUser,
};
use windows_sys::Win32::Storage::FileSystem::{
    CREATE_NEW, CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_FLAG_FIRST_PIPE_INSTANCE,
    FILE_FLAG_OVERLAPPED, FILE_GENERIC_WRITE, FILE_SHARE_READ, OPEN_EXISTING, PIPE_ACCESS_DUPLEX,
    ReadFile, SECURITY_IDENTIFICATION, SECURITY_SQOS_PRESENT, WriteFile,
};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, GetNamedPipeClientProcessId, PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_UNLIMITED_INSTANCES, PIPE_WAIT,
    WaitNamedPipeW,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, GetCurrentProcess, GetExitCodeProcess, GetProcessTimes, INFINITE, OpenProcess,
    OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION, WaitForSingleObject,
};
use windows_sys::core::PWSTR;

use crate::sddl::{Dacl, parse_dacl};

/// Memory that a Win32 call allocated with `LocalAlloc`; `LocalFree` releases it.
struct LocalMemory(*mut c_void);

impl Drop for LocalMemory {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: the pointer came from a Win32 call that documents `LocalFree` as the way to
            // release its result, and this guard is its only owner.
            unsafe { LocalFree(self.0) };
        }
    }
}

fn wide(text: &OsStr) -> io::Result<Vec<u16>> {
    let mut units: Vec<u16> = text.encode_wide().collect();
    if units.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "a Windows name cannot contain a NUL",
        ));
    }
    units.push(0);
    Ok(units)
}

/// Take ownership of a handle that a Win32 call returned, or report why it failed.
fn owned_handle(handle: HANDLE) -> io::Result<OwnedHandle> {
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the handle is valid, was just returned to this caller, and nobody else closes it.
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}

/// Read a NUL-terminated UTF-16 string.
///
/// # Safety
/// `text` must point to a NUL-terminated UTF-16 string that stays valid for the call.
unsafe fn string_from_wide(text: *const u16) -> String {
    let mut len = 0;
    // SAFETY: the caller guarantees the terminator, so every offset up to it is readable.
    while unsafe { *text.add(len) } != 0 {
        len += 1;
    }
    // SAFETY: `len` units before the terminator were just read.
    String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(text, len) })
}

fn sid_to_string(sid: PSID) -> io::Result<String> {
    let mut text: PWSTR = null_mut();
    // SAFETY: `sid` points to a valid SID owned by the caller; `text` receives a `LocalAlloc`
    // buffer on success.
    if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let _free = LocalMemory(text.cast());
    // SAFETY: on success `text` is a NUL-terminated string that `_free` keeps alive.
    Ok(unsafe { string_from_wide(text) })
}

fn token_user_sid(token: HANDLE) -> io::Result<String> {
    let mut needed = 0_u32;
    // SAFETY: a size query with a null buffer; `needed` is a valid out pointer.
    unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(io::Error::last_os_error());
    }
    // A `u64` buffer is aligned for `TOKEN_USER`.
    let mut buffer = vec![0_u64; (needed as usize).div_ceil(8)];
    // SAFETY: the buffer holds at least `needed` bytes and `token` has `TOKEN_QUERY` access.
    if unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the call filled the aligned buffer with a `TOKEN_USER`; its SID points into the buffer.
    let sid = unsafe { (*buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    sid_to_string(sid)
}

fn process_token_user_sid(process: HANDLE) -> io::Result<String> {
    let mut token: HANDLE = null_mut();
    // SAFETY: `process` is a valid process handle and `token` is a valid out pointer.
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let token = owned_handle(token)?;
    token_user_sid(token.as_raw_handle())
}

/// The user SID (`S-1-5-21-...`) of the current process.
pub fn current_user_sid() -> io::Result<String> {
    // SAFETY: `GetCurrentProcess` returns a pseudo handle that is always valid and never closed.
    process_token_user_sid(unsafe { GetCurrentProcess() })
}

/// The user SID of the process `pid`. A process that cannot be opened is an error.
pub fn process_user_sid(pid: u32) -> io::Result<String> {
    // SAFETY: plain value arguments; the result is checked and owned below.
    let process = owned_handle(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })?;
    process_token_user_sid(process.as_raw_handle())
}

/// The creation time of the running process `pid` (a `FILETIME`: 100 ns intervals since 1601),
/// or `None` when no process has that pid or it has exited. A process that cannot be opened for
/// another reason, such as one of another user, is an error.
pub fn process_creation_time(pid: u32) -> io::Result<Option<u64>> {
    // SAFETY: plain value arguments; the result is checked and owned below.
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if raw.is_null() {
        let err = io::Error::last_os_error();
        return match err.raw_os_error() {
            Some(code) if code == ERROR_INVALID_PARAMETER as i32 => Ok(None),
            _ => Err(err),
        };
    }
    let process = owned_handle(raw)?;
    let mut exit_code = 0_u32;
    // SAFETY: `process` is a valid handle with query access; `exit_code` is a valid out pointer.
    if unsafe { GetExitCodeProcess(process.as_raw_handle(), &mut exit_code) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if exit_code != STILL_ACTIVE as u32 {
        return Ok(None);
    }
    let empty = FILETIME {
        dwLowDateTime: 0,
        dwHighDateTime: 0,
    };
    let (mut created, mut exited, mut kernel, mut user) = (empty, empty, empty, empty);
    // SAFETY: `process` is a valid handle with query access; the four out pointers are valid.
    if unsafe {
        GetProcessTimes(
            process.as_raw_handle(),
            &mut created,
            &mut exited,
            &mut kernel,
            &mut user,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(Some(
        (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime),
    ))
}

/// A security descriptor whose only entry grants full access to one user, with no inheritance:
/// the Windows counterpart of mode `0600` and `0700`.
pub struct OwnerOnly {
    descriptor: LocalMemory,
}

// SAFETY: the descriptor is immutable heap memory that is only read after construction.
unsafe impl Send for OwnerOnly {}
// SAFETY: the same reason; a shared reference only reads it.
unsafe impl Sync for OwnerOnly {}

impl OwnerOnly {
    /// Build the descriptor `D:P(A;;FA;;;<sid>)` for a SID string such as `S-1-5-21-1-2-3-1001`.
    pub fn for_user_sid(sid: &str) -> io::Result<Self> {
        let well_formed = sid.starts_with("S-1-")
            && sid
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'S' || byte == b'-');
        if !well_formed {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("not a SID string: {sid:?}"),
            ));
        }
        let sddl: Vec<u16> = format!("D:P(A;;FA;;;{sid})")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
        // SAFETY: `sddl` is NUL-terminated and outlives the call; `descriptor` is a valid out
        // pointer and the size out pointer may be null.
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            descriptor: LocalMemory(descriptor),
        })
    }

    /// Security attributes that point into this descriptor; valid while `self` lives.
    pub(crate) fn attributes(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: self.descriptor.0,
            bInheritHandle: 0,
        }
    }
}

/// Create a new file that only the user `sid` can access. It fails if the file exists.
pub fn create_private_file(path: &Path, sid: &str) -> io::Result<File> {
    let owner = OwnerOnly::for_user_sid(sid)?;
    let attributes = owner.attributes();
    let name = wide(path.as_os_str())?;
    // SAFETY: `name` is NUL-terminated, `attributes` points into `owner`, and both outlive the call.
    let handle = unsafe {
        CreateFileW(
            name.as_ptr(),
            FILE_GENERIC_WRITE,
            FILE_SHARE_READ,
            &attributes,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            null_mut(),
        )
    };
    Ok(File::from(owned_handle(handle)?))
}

/// Fill `buffer` from the system random number generator.
pub fn random_bytes(buffer: &mut [u8]) -> io::Result<()> {
    let len = u32::try_from(buffer.len())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "the buffer is too large"))?;
    // SAFETY: the pointer and length describe `buffer`; a null algorithm handle with the
    // system-preferred flag is the documented way to ask for the default generator.
    let status = unsafe {
        BCryptGenRandom(
            null_mut(),
            buffer.as_mut_ptr(),
            len,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        return Err(io::Error::other(format!(
            "BCryptGenRandom failed with NTSTATUS {status:#x}"
        )));
    }
    Ok(())
}

fn dacl_of_descriptor(descriptor: PSECURITY_DESCRIPTOR) -> io::Result<Dacl> {
    let mut text: PWSTR = null_mut();
    // SAFETY: `descriptor` is a valid security descriptor; `text` is a valid out pointer.
    if unsafe {
        ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            SDDL_REVISION_1,
            DACL_SECURITY_INFORMATION,
            &mut text,
            null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let _free = LocalMemory(text.cast());
    // SAFETY: on success `text` is a NUL-terminated string that `_free` keeps alive.
    let sddl = unsafe { string_from_wide(text) };
    let mut dacl = parse_dacl(&sddl).map_err(io::Error::other)?;
    for ace in &mut dacl.aces {
        ace.trustee = trustee_sid(&ace.trustee)?;
    }
    Ok(dacl)
}

/// The SID string of an SDDL trustee. Windows prints a well-known SID as a two-letter alias (`LA`
/// for the built-in administrator, `SY` for the system), so callers could not compare it with a
/// SID string.
fn trustee_sid(trustee: &str) -> io::Result<String> {
    if trustee.starts_with("S-") {
        return Ok(trustee.to_owned());
    }
    let alias = wide(OsStr::new(trustee))?;
    let mut sid: PSID = null_mut();
    // SAFETY: `alias` is NUL-terminated and outlives the call; `sid` is a valid out pointer.
    if unsafe { ConvertStringSidToSidW(alias.as_ptr(), &mut sid) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let _free = LocalMemory(sid);
    sid_to_string(sid)
}

/// The DACL of a file or directory. Tests read it back to prove that a file is private.
pub fn dacl_of_path(path: &Path) -> io::Result<Dacl> {
    let name = wide(path.as_os_str())?;
    let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
    // SAFETY: `name` is NUL-terminated; only the DACL and the descriptor are requested, and the
    // unused out pointers are null as the API allows.
    let status = unsafe {
        GetNamedSecurityInfoW(
            name.as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            null_mut(),
            null_mut(),
            null_mut(),
            null_mut(),
            &mut descriptor,
        )
    };
    if status != 0 {
        return Err(io::Error::from_raw_os_error(status as i32));
    }
    let _free = LocalMemory(descriptor);
    dacl_of_descriptor(descriptor)
}

/// The DACL of the object behind an open file or pipe handle.
fn dacl_of_handle(handle: HANDLE) -> io::Result<Dacl> {
    let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
    // SAFETY: `handle` is a valid open handle; only the DACL and the descriptor are requested, and
    // the unused out pointers are null as the API allows.
    let status = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            null_mut(),
            null_mut(),
            null_mut(),
            null_mut(),
            &mut descriptor,
        )
    };
    if status != 0 {
        return Err(io::Error::from_raw_os_error(status as i32));
    }
    let _free = LocalMemory(descriptor);
    dacl_of_descriptor(descriptor)
}

/// An `OVERLAPPED` with its own event. The structure sits in a box, so its address stays fixed
/// while the kernel may still write to it.
struct Overlapped {
    raw: Box<OVERLAPPED>,
    event: OwnedHandle,
}

// SAFETY: the box and the event handle are owned by this value alone. The kernel keeps only the
// address of the box, and `CancelIoEx` and `GetOverlappedResult` work from any thread, so an
// operation that is still pending (the `ConnectNamedPipe` of a `PipeListener`) may move with the
// value to another thread; whoever owns the value waits for or cancels the operation before it
// frees the box (`finish_io`, `PipeListener::drop`). Windows cancels the pending calls of a thread
// when it exits, so such an operation can end with `ERROR_OPERATION_ABORTED`;
// `PipeListener::accept` starts it again.
unsafe impl Send for Overlapped {}

impl Overlapped {
    fn new() -> io::Result<Self> {
        // SAFETY: null attributes and name; a manual-reset event that starts unsignalled.
        let event = owned_handle(unsafe { CreateEventW(null(), 1, 0, null()) })?;
        let mut overlapped = Self {
            raw: Box::new(OVERLAPPED::default()),
            event,
        };
        overlapped.reset();
        Ok(overlapped)
    }

    /// Prepare for the next operation: clear the result fields, keep the event.
    fn reset(&mut self) {
        *self.raw = OVERLAPPED {
            hEvent: self.event.as_raw_handle(),
            ..OVERLAPPED::default()
        };
    }

    fn as_mut_ptr(&mut self) -> *mut OVERLAPPED {
        &mut *self.raw
    }

    /// Wait for the event; `false` means the wait timed out.
    fn wait(&self, timeout: Option<Duration>) -> io::Result<bool> {
        let millis = timeout.map_or(INFINITE, |limit| {
            u32::try_from(limit.as_millis()).map_or(INFINITE - 1, |ms| ms.min(INFINITE - 1))
        });
        // SAFETY: the event handle is owned and valid.
        match unsafe { WaitForSingleObject(self.event.as_raw_handle(), millis) } {
            WAIT_OBJECT_0 => Ok(true),
            WAIT_TIMEOUT => Ok(false),
            _ => Err(io::Error::last_os_error()),
        }
    }

    /// The outcome of the operation started on `handle`. With `wait` it blocks until the
    /// operation has ended, which is what makes the buffers safe to release after a cancel.
    fn result(&mut self, handle: HANDLE, wait: bool) -> io::Result<usize> {
        let mut transferred = 0_u32;
        // SAFETY: `handle` and this structure belong to one started operation.
        if unsafe {
            GetOverlappedResult(handle, self.as_mut_ptr(), &mut transferred, i32::from(wait))
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(transferred as usize)
    }

    /// Cancel the pending operation and wait until the kernel is done with its buffers.
    fn cancel(&mut self, handle: HANDLE) -> io::Result<usize> {
        // SAFETY: cancels the operation that uses this structure; failure only means that it has
        // already completed, which `result` then reports.
        unsafe { CancelIoEx(handle, self.as_mut_ptr()) };
        self.result(handle, true)
    }
}

fn is_os_error(err: &io::Error, codes: &[u32]) -> bool {
    err.raw_os_error()
        .is_some_and(|raw| codes.iter().any(|&code| raw == code as i32))
}

/// Finish an overlapped `ReadFile` or `WriteFile` whose start returned `started`.
fn finish_io(
    handle: HANDLE,
    overlapped: &mut Overlapped,
    started: i32,
    timeout: Option<Duration>,
) -> io::Result<usize> {
    if started == 0 {
        let err = io::Error::last_os_error();
        if !is_os_error(&err, &[ERROR_IO_PENDING]) {
            return Err(err);
        }
        let ended = match overlapped.wait(timeout) {
            Ok(ended) => ended,
            Err(wait_err) => {
                // The wait itself failed, so the operation may still be running: end it before the
                // caller frees the buffer, and report the failed wait.
                let _ = overlapped.cancel(handle);
                return Err(wait_err);
            }
        };
        if !ended {
            // The operation may have finished in the race; its bytes then count.
            return match overlapped.cancel(handle) {
                Err(err) if is_os_error(&err, &[ERROR_OPERATION_ABORTED]) => {
                    Err(io::ErrorKind::TimedOut.into())
                }
                other => other,
            };
        }
    }
    overlapped.result(handle, false)
}

/// One end of a named pipe connection with overlapped I/O and a timeout for each operation.
pub struct PipeConnection {
    handle: OwnedHandle,
    overlapped: Overlapped,
    timeout: Option<Duration>,
}

impl PipeConnection {
    fn new(handle: OwnedHandle) -> io::Result<Self> {
        Ok(Self {
            handle,
            overlapped: Overlapped::new()?,
            timeout: None,
        })
    }

    /// Limit every read and write; `None` waits without end. A read that times out fails with
    /// [`io::ErrorKind::TimedOut`].
    pub fn set_timeout(&mut self, timeout: Option<Duration>) {
        self.timeout = timeout;
    }

    /// The id of the process at the other end of the pipe, as the server sees it.
    pub fn client_process_id(&self) -> io::Result<u32> {
        let mut pid = 0_u32;
        // SAFETY: the handle is an open pipe handle and `pid` is a valid out pointer.
        if unsafe { GetNamedPipeClientProcessId(self.handle.as_raw_handle(), &mut pid) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(pid)
    }
}

impl Read for PipeConnection {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let len = u32::try_from(buf.len()).unwrap_or(u32::MAX);
        let handle = self.handle.as_raw_handle();
        self.overlapped.reset();
        // SAFETY: the buffer outlives the operation, because `finish_io` waits for its end or for
        // its cancellation before it returns; the overlapped structure is boxed and owned.
        let started = unsafe {
            ReadFile(
                handle,
                buf.as_mut_ptr(),
                len,
                null_mut(),
                self.overlapped.as_mut_ptr(),
            )
        };
        match finish_io(handle, &mut self.overlapped, started, self.timeout) {
            // The other end closed the pipe: end of stream.
            Err(err)
                if is_os_error(
                    &err,
                    &[
                        ERROR_BROKEN_PIPE,
                        ERROR_PIPE_NOT_CONNECTED,
                        ERROR_HANDLE_EOF,
                    ],
                ) =>
            {
                Ok(0)
            }
            other => other,
        }
    }
}

impl Write for PipeConnection {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let len = u32::try_from(buf.len()).unwrap_or(u32::MAX);
        let handle = self.handle.as_raw_handle();
        self.overlapped.reset();
        // SAFETY: as for `read`; `finish_io` ends or cancels the write before the buffer is freed.
        let started = unsafe {
            WriteFile(
                handle,
                buf.as_ptr(),
                len,
                null_mut(),
                self.overlapped.as_mut_ptr(),
            )
        };
        finish_io(handle, &mut self.overlapped, started, self.timeout)
    }

    /// Writes reach the pipe buffer at once; the peer drains it.
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

const PIPE_BUFFER_BYTES: u32 = 64 * 1024;

fn create_instance(name: &[u16], owner: &OwnerOnly, first: bool) -> io::Result<OwnedHandle> {
    let first_flag = if first {
        FILE_FLAG_FIRST_PIPE_INSTANCE
    } else {
        0
    };
    let attributes = owner.attributes();
    // SAFETY: `name` is NUL-terminated and `attributes` points into `owner`; both outlive the call.
    let handle = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | first_flag,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,
            PIPE_BUFFER_BYTES,
            PIPE_BUFFER_BYTES,
            0,
            &attributes,
        )
    };
    owned_handle(handle)
}

/// A named pipe server: a ring of instances that all carry the owner-only DACL and refuse
/// remote clients. One instance listens for the next client. Once a client has connected to it,
/// no instance listens until [`PipeListener::accept`] hands the connection over and opens the
/// next one, so a second client finds the pipe busy and waits (see [`connect_client`]) until the
/// server accepts.
pub struct PipeListener {
    name: Vec<u16>,
    owner: OwnerOnly,
    /// The instance that listens for a client.
    instance: OwnedHandle,
    overlapped: Overlapped,
    /// A `ConnectNamedPipe` on `instance` is pending.
    pending: bool,
    /// A client is already connected to `instance`.
    ready: bool,
}

impl PipeListener {
    /// Create the first instance of `\\.\pipe\<name>` for the user `sid`. A pipe of that name
    /// that already exists fails with [`io::ErrorKind::AddrInUse`].
    pub fn bind(name: &str, sid: &str) -> io::Result<Self> {
        let owner = OwnerOnly::for_user_sid(sid)?;
        let name = wide(OsStr::new(name))?;
        let instance = create_instance(&name, &owner, true).map_err(|err| {
            if is_os_error(&err, &[ERROR_ACCESS_DENIED, ERROR_PIPE_BUSY]) {
                io::Error::new(io::ErrorKind::AddrInUse, err)
            } else {
                err
            }
        })?;
        let mut listener = Self {
            name,
            owner,
            instance,
            overlapped: Overlapped::new()?,
            pending: false,
            ready: false,
        };
        listener.arm()?;
        Ok(listener)
    }

    /// The DACL of the pipe, read back from the open instance.
    pub fn dacl(&self) -> io::Result<Dacl> {
        dacl_of_handle(self.instance.as_raw_handle())
    }

    /// Wait up to `timeout` for a client. `None` means that none came, so a caller can look at
    /// its stop flag and call again.
    pub fn accept(&mut self, timeout: Duration) -> io::Result<Option<PipeConnection>> {
        if !self.pending && !self.ready {
            self.arm()?;
        }
        if self.pending {
            if !self.overlapped.wait(Some(timeout))? {
                return Ok(None);
            }
            self.pending = false;
            match self.overlapped.result(self.instance.as_raw_handle(), false) {
                Ok(_) => self.ready = true,
                // The thread that started the wait has exited and Windows cancelled its call. The
                // instance still listens, so wait again from this thread.
                Err(err) if is_os_error(&err, &[ERROR_OPERATION_ABORTED]) => {
                    self.arm()?;
                    if !self.ready {
                        return Ok(None);
                    }
                }
                // A client that connected and left at once.
                Err(err) if is_os_error(&err, &[ERROR_NO_DATA, ERROR_BROKEN_PIPE]) => {
                    self.replace_instance()?;
                    return Ok(None);
                }
                Err(err) => return Err(err),
            }
        }
        self.hand_over().map(Some)
    }

    /// Start listening on the current instance: `ready` if a client is already there, `pending`
    /// otherwise.
    fn arm(&mut self) -> io::Result<()> {
        loop {
            self.overlapped.reset();
            // SAFETY: the instance is an open overlapped pipe handle and the structure is boxed.
            let connected = unsafe {
                ConnectNamedPipe(self.instance.as_raw_handle(), self.overlapped.as_mut_ptr())
            } != 0;
            if connected {
                self.ready = true;
                return Ok(());
            }
            let err = io::Error::last_os_error();
            if is_os_error(&err, &[ERROR_PIPE_CONNECTED]) {
                self.ready = true;
                return Ok(());
            }
            if is_os_error(&err, &[ERROR_IO_PENDING]) {
                self.pending = true;
                return Ok(());
            }
            if !is_os_error(&err, &[ERROR_NO_DATA]) {
                return Err(err);
            }
            // A client left before the connect call: start over with a fresh instance.
            self.instance = create_instance(&self.name, &self.owner, false)?;
        }
    }

    /// Give the connected instance to the caller and open the next one first, so that the next
    /// client finds a listening pipe.
    fn hand_over(&mut self) -> io::Result<PipeConnection> {
        let next = create_instance(&self.name, &self.owner, false)?;
        let connected = std::mem::replace(&mut self.instance, next);
        self.ready = false;
        let connection = PipeConnection::new(connected);
        self.arm()?;
        connection
    }

    fn replace_instance(&mut self) -> io::Result<()> {
        self.instance = create_instance(&self.name, &self.owner, false)?;
        self.ready = false;
        self.arm()
    }
}

impl Drop for PipeListener {
    fn drop(&mut self) {
        if self.pending {
            // Wait until the kernel no longer writes to the boxed `OVERLAPPED`.
            let _ = self.overlapped.cancel(self.instance.as_raw_handle());
        }
    }
}

/// Connect to the pipe `name` as a client. The client lets the server identify it but not
/// impersonate it, so a process that took the pipe name cannot act as the user. A busy pipe is
/// waited for up to `timeout`.
pub fn connect_client(name: &str, timeout: Duration) -> io::Result<PipeConnection> {
    let wide_name = wide(OsStr::new(name))?;
    let deadline = Instant::now() + timeout;
    loop {
        // SAFETY: `wide_name` is NUL-terminated and outlives the call; no security attributes,
        // no template.
        let handle = unsafe {
            CreateFileW(
                wide_name.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                null_mut(),
            )
        };
        match owned_handle(handle) {
            Ok(pipe) => return PipeConnection::new(pipe),
            Err(err) if is_os_error(&err, &[ERROR_PIPE_BUSY]) => {
                // The wait gets what is left of the timeout, so a busy pipe cannot hold a client
                // past it.
                let left = deadline.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    return Err(io::ErrorKind::TimedOut.into());
                }
                let wait_ms = u32::try_from(left.as_millis()).map_or(u32::MAX - 1, |ms| ms.max(1));
                // SAFETY: `wide_name` is NUL-terminated and outlives the call.
                if unsafe { WaitNamedPipeW(wide_name.as_ptr(), wait_ms) } == 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Err(err) => return Err(err),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_dacl_names_a_well_known_trustee_by_its_sid_not_its_alias() {
        // Windows prints the SID `S-1-5-18` as the alias `SY`.
        let owner = OwnerOnly::for_user_sid("S-1-5-18").unwrap();

        let dacl = dacl_of_descriptor(owner.descriptor.0).unwrap();

        assert_eq!(dacl.aces.len(), 1, "{dacl:?}");
        assert_eq!(dacl.aces[0].trustee, "S-1-5-18");
    }

    #[test]
    fn the_wait_of_an_exited_thread_ends_as_aborted() {
        let sid = current_user_sid().unwrap();
        let name = format!(r"\\.\pipe\pf-unit-abort-{}", std::process::id());
        let mut listener = std::thread::spawn(move || PipeListener::bind(&name, &sid).unwrap())
            .join()
            .unwrap();

        assert!(
            listener
                .overlapped
                .wait(Some(Duration::from_secs(5)))
                .unwrap()
        );
        let err = listener
            .overlapped
            .result(listener.instance.as_raw_handle(), false)
            .unwrap_err();
        assert!(is_os_error(&err, &[ERROR_OPERATION_ABORTED]), "{err}");
    }
}
