//! Job objects, process handles and console control events of the Windows supervisor (ADR 0022,
//! sections 2 and 3).
//!
//! An execution runs in a named job object that [`Job::create`] makes without any limit: no
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` ends it with the runtime, and no breakaway flag lets a
//! process of the execution leave it. A temporary kernel object keeps its name only while a handle
//! to it is open; the processes of a job hold a pointer to it, not a handle. [`Job::share_with`]
//! therefore gives the leader of the execution its own query handle, so a runtime that starts
//! after the old one died finds the job by its name while the leader lives.
//!
//! A [`Process`] is an open handle: while it is open, Windows does not give the pid to another
//! process, and the creation time and the exit code stay readable after the process has ended.
//! [`send_ctrl_break`] attaches the calling process to the console of a member of the job, so
//! only a short helper process without a console of its own may call it.
//!
//! The members of a job are told apart by the full path of their executable image
//! ([`Process::image_path`]), by the pid of the process that created them ([`parent_pids`]) and by
//! the system directory of Windows ([`system_directory`]), where the console host lives.

use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::io;
use std::mem::{offset_of, size_of};
use std::os::windows::ffi::OsStringExt;
use std::os::windows::io::{AsHandle, AsRawHandle, OwnedHandle};
use std::path::PathBuf;
use std::process::Child;
use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{
    DuplicateHandle, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, ERROR_INSUFFICIENT_BUFFER,
    ERROR_INVALID_PARAMETER, ERROR_MORE_DATA, ERROR_NO_MORE_FILES, FILETIME, HANDLE, SetLastError,
    WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::SE_KERNEL_OBJECT;
use windows_sys::Win32::System::Console::{
    AttachConsole, CTRL_BREAK_EVENT, FreeConsole, GenerateConsoleCtrlEvent, SetConsoleCtrlHandler,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW, TH32CS_SNAPPROCESS,
    TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_BASIC_PROCESS_ID_LIST,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
    JobObjectBasicProcessIdList, JobObjectExtendedLimitInformation, OpenJobObjectW,
    QueryInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
use windows_sys::Win32::System::SystemServices::{
    JOB_OBJECT_ASSIGN_PROCESS, JOB_OBJECT_QUERY, JOB_OBJECT_TERMINATE,
};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetExitCodeProcess, GetProcessId, GetProcessTimes, OpenProcess, OpenThread,
    PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
    QueryFullProcessImageNameW, ResumeThread, THREAD_SUSPEND_RESUME, WaitForSingleObject,
};
use windows_sys::core::BOOL;

pub use windows_sys::Win32::System::Threading::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, DETACHED_PROCESS,
};

use super::{OwnerOnly, dacl_of_handle, is_os_error, owned_handle, wide};
use crate::sddl::Dacl;

/// The result of a Win32 call that returns `BOOL`.
fn check(result: BOOL) -> io::Result<()> {
    if result == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// What an opened job object handle may do.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobAccess {
    /// List and count the members: the ctrl-break helper.
    Query,
    /// List, count and terminate the members: a runtime that finds the job of an execution again.
    Control,
    /// List, count and add a process: a test that puts a process of its own into a job. The
    /// runtime never opens a job with it.
    Assign,
}

/// An open handle to a job object.
#[derive(Debug)]
pub struct Job {
    handle: OwnedHandle,
}

impl Job {
    fn raw(&self) -> HANDLE {
        self.handle.as_raw_handle()
    }

    /// Create the job object `name` (such as `Global\harness-runtime-execution-<id>`) that only the
    /// user `sid` may open. No limit is set. A name that exists already, as a job object or as an
    /// object of another type, is refused: a job that this call did not create is never taken
    /// over.
    ///
    /// # Errors
    /// [`io::ErrorKind::AlreadyExists`] for a job object of that name; the error of the OS
    /// otherwise.
    pub fn create(name: &str, sid: &str) -> io::Result<Self> {
        let owner = OwnerOnly::with_rights(sid, "GA")?;
        let attributes = owner.attributes();
        let wide_name = wide(OsStr::new(name))?;
        // SAFETY: only the last error of this thread is cleared.
        unsafe { SetLastError(0) };
        // SAFETY: `attributes` points into `owner` and `wide_name` is NUL-terminated; both outlive
        // the call.
        let raw = unsafe { CreateJobObjectW(&attributes, wide_name.as_ptr()) };
        let existed = is_os_error(&io::Error::last_os_error(), &[ERROR_ALREADY_EXISTS]);
        let handle = owned_handle(raw)?;
        if existed {
            drop(handle);
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!("the job object {name} exists already and is never taken over"),
            ));
        }
        Ok(Self { handle })
    }

    /// Open the job object `name`; `None` when no object has that name.
    ///
    /// # Errors
    /// The error of the OS, such as a name that belongs to an object of another type or a job
    /// that this user may not open.
    pub fn open(name: &str, access: JobAccess) -> io::Result<Option<Self>> {
        let rights = match access {
            JobAccess::Query => JOB_OBJECT_QUERY,
            JobAccess::Control => JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE,
            JobAccess::Assign => JOB_OBJECT_QUERY | JOB_OBJECT_ASSIGN_PROCESS,
        };
        let wide_name = wide(OsStr::new(name))?;
        // SAFETY: `wide_name` is NUL-terminated and outlives the call; the handle is not inherited.
        let raw = unsafe { OpenJobObjectW(rights, 0, wide_name.as_ptr()) };
        if raw.is_null() {
            let err = io::Error::last_os_error();
            return if is_os_error(&err, &[ERROR_FILE_NOT_FOUND]) {
                Ok(None)
            } else {
                Err(err)
            };
        }
        Ok(Some(Self {
            handle: owned_handle(raw)?,
        }))
    }

    /// Put `process` into this job. Its descendants are created in the job too.
    ///
    /// # Errors
    /// The error of the OS, such as a handle without the right to assign.
    pub fn assign(&self, process: &Process) -> io::Result<()> {
        // SAFETY: both handles are open and owned by their values for the whole call.
        check(unsafe { AssignProcessToJobObject(self.raw(), process.raw()) })
    }

    /// Give `process` its own inheritable handle to this job, with the query right only. The
    /// handle lives in that process until it ends, and keeps the name of the job alive.
    ///
    /// # Errors
    /// The error of the OS, such as a process handle without `PROCESS_DUP_HANDLE`.
    pub fn share_with(&self, process: &Process) -> io::Result<()> {
        let mut copy: HANDLE = null_mut();
        // SAFETY: the pseudo handle of this process is always valid; the job and process handles
        // are open; `copy` is a valid out pointer. The copy belongs to the other process, so this
        // process never uses or closes it.
        check(unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                self.raw(),
                process.raw(),
                &mut copy,
                JOB_OBJECT_QUERY,
                1,
                0,
            )
        })
    }

    /// The pids of the processes of the job that have not ended.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn processes(&self) -> io::Result<Vec<u32>> {
        let header =
            offset_of!(JOBOBJECT_BASIC_PROCESS_ID_LIST, ProcessIdList) / size_of::<usize>();
        let mut capacity = 64_usize;
        loop {
            // A `usize` buffer is aligned for the structure, whose list holds `usize` entries.
            let mut buffer = vec![0_usize; header + capacity];
            let bytes = u32::try_from(buffer.len() * size_of::<usize>())
                .map_err(|_| io::Error::other("the job lists too many processes"))?;
            // SAFETY: the buffer is aligned for the structure and holds `bytes` bytes; the length
            // out pointer may be null.
            let listed = unsafe {
                QueryInformationJobObject(
                    self.raw(),
                    JobObjectBasicProcessIdList,
                    buffer.as_mut_ptr().cast(),
                    bytes,
                    null_mut(),
                )
            };
            if listed == 0 {
                let err = io::Error::last_os_error();
                if is_os_error(&err, &[ERROR_MORE_DATA]) {
                    capacity *= 2;
                    continue;
                }
                return Err(err);
            }
            // SAFETY: the call filled the header of the structure at the start of the buffer.
            let list = unsafe { &*buffer.as_ptr().cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>() };
            let (assigned, count) = (
                list.NumberOfAssignedProcesses as usize,
                list.NumberOfProcessIdsInList as usize,
            );
            if assigned > count && assigned > capacity {
                capacity = assigned + 8;
                continue;
            }
            return Ok(buffer[header..header + count.min(capacity)]
                .iter()
                .filter_map(|pid| u32::try_from(*pid).ok())
                .collect());
        }
    }

    /// The number of processes of the job that have not ended.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn active_processes(&self) -> io::Result<u32> {
        let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        // SAFETY: `info` is a valid, writable structure of the size passed.
        check(unsafe {
            QueryInformationJobObject(
                self.raw(),
                JobObjectBasicAccountingInformation,
                (&raw mut info).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                null_mut(),
            )
        })?;
        Ok(info.ActiveProcesses)
    }

    /// Whether `process` is in this job.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn contains(&self, process: &Process) -> io::Result<bool> {
        let mut result: BOOL = 0;
        // SAFETY: both handles are open; `result` is a valid out pointer.
        check(unsafe { IsProcessInJob(process.raw(), self.raw(), &mut result) })?;
        Ok(result != 0)
    }

    /// End every process of the job with `exit_code`.
    ///
    /// # Errors
    /// The error of the OS, such as a handle without the terminate right.
    pub fn terminate(&self, exit_code: u32) -> io::Result<()> {
        // SAFETY: the handle is open.
        check(unsafe { TerminateJobObject(self.raw(), exit_code) })
    }

    /// The `LimitFlags` of the job: 0 for a job that [`Job::create`] made.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn limit_flags(&self) -> io::Result<u32> {
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        // SAFETY: `info` is a valid, writable structure of the size passed.
        check(unsafe {
            QueryInformationJobObject(
                self.raw(),
                JobObjectExtendedLimitInformation,
                (&raw mut info).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                null_mut(),
            )
        })?;
        Ok(info.BasicLimitInformation.LimitFlags)
    }

    /// The DACL of the job object, read back from the handle that created it.
    ///
    /// # Errors
    /// The error of the OS, such as a handle without `READ_CONTROL`.
    pub fn dacl(&self) -> io::Result<Dacl> {
        dacl_of_handle(self.raw(), SE_KERNEL_OBJECT)
    }
}

/// An open handle to a process and its pid.
#[derive(Debug)]
pub struct Process {
    handle: OwnedHandle,
    pid: u32,
}

impl Process {
    fn raw(&self) -> HANDLE {
        self.handle.as_raw_handle()
    }

    fn of(handle: OwnedHandle) -> io::Result<Self> {
        // SAFETY: the handle is open.
        let pid = unsafe { GetProcessId(handle.as_raw_handle()) };
        if pid == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { handle, pid })
    }

    /// Open the process `pid` to observe it; `None` when no process has that pid. A process that
    /// has ended is still opened while another handle keeps it.
    ///
    /// # Errors
    /// The error of the OS, such as a process of another user.
    pub fn open(pid: u32) -> io::Result<Option<Self>> {
        // SAFETY: plain value arguments; the result is checked and owned below.
        let raw = unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                0,
                pid,
            )
        };
        if raw.is_null() {
            let err = io::Error::last_os_error();
            return if is_os_error(&err, &[ERROR_INVALID_PARAMETER]) {
                Ok(None)
            } else {
                Err(err)
            };
        }
        Self::of(owned_handle(raw)?).map(Some)
    }

    /// A handle of its own to the process of `child`, with every right of the child's handle.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn from_child(child: &Child) -> io::Result<Self> {
        Self::of(child.as_handle().try_clone_to_owned()?)
    }

    /// Another handle to the same process.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn try_clone(&self) -> io::Result<Self> {
        Ok(Self {
            handle: self.handle.try_clone()?,
            pid: self.pid,
        })
    }

    /// The pid of the process.
    pub const fn pid(&self) -> u32 {
        self.pid
    }

    /// The creation time of the process (a `FILETIME`: 100 ns intervals since 1601). It stays
    /// readable after the process has ended.
    ///
    /// # Errors
    /// The error of the OS.
    pub fn creation_time(&self) -> io::Result<u64> {
        let empty = FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        };
        let (mut created, mut exited, mut kernel, mut user) = (empty, empty, empty, empty);
        // SAFETY: the handle has query access; the four out pointers are valid.
        check(unsafe {
            GetProcessTimes(
                self.raw(),
                &mut created,
                &mut exited,
                &mut kernel,
                &mut user,
            )
        })?;
        Ok((u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime))
    }

    /// The full path of the executable image of the process in the Win32 form, such as
    /// `C:\Windows\System32\conhost.exe`.
    ///
    /// # Errors
    /// The error of the OS, such as a process that has ended meanwhile.
    pub fn image_path(&self) -> io::Result<PathBuf> {
        // A Win32 path holds at most 32 767 UTF-16 units.
        let mut capacity = 1024_u32;
        loop {
            let mut buffer = vec![0_u16; capacity as usize];
            let mut size = capacity;
            // SAFETY: the handle has query access; the buffer holds `size` units and `size` is a
            // valid in-out pointer.
            let read = unsafe {
                QueryFullProcessImageNameW(
                    self.raw(),
                    PROCESS_NAME_WIN32,
                    buffer.as_mut_ptr(),
                    &mut size,
                )
            };
            if read != 0 {
                buffer.truncate(size as usize);
                return Ok(PathBuf::from(OsString::from_wide(&buffer)));
            }
            let err = io::Error::last_os_error();
            if !is_os_error(&err, &[ERROR_INSUFFICIENT_BUFFER]) || capacity >= 65_536 {
                return Err(err);
            }
            capacity *= 2;
        }
    }

    /// The exit code of the process once it has ended, `None` while it runs. The wait on the
    /// handle decides, so a process that ended with code 259 (`STILL_ACTIVE`) is not taken for a
    /// running one.
    ///
    /// # Errors
    /// The error of the OS, such as a handle without `SYNCHRONIZE`.
    pub fn exit_code(&self) -> io::Result<Option<u32>> {
        // SAFETY: the handle is open; a zero timeout only polls.
        match unsafe { WaitForSingleObject(self.raw(), 0) } {
            WAIT_TIMEOUT => Ok(None),
            WAIT_OBJECT_0 => {
                let mut code = 0_u32;
                // SAFETY: the handle has query access; `code` is a valid out pointer.
                check(unsafe { GetExitCodeProcess(self.raw(), &mut code) })?;
                Ok(Some(code))
            }
            _ => Err(io::Error::last_os_error()),
        }
    }

    /// Resume every thread of the process once and return how many it resumed. A thread that is
    /// not suspended is left as it is; a thread that ends meanwhile is skipped. The pid cannot be
    /// reused while this handle is open, so the threads found by it belong to this process.
    ///
    /// # Errors
    /// The error of the OS when the threads cannot be listed or a thread cannot be resumed.
    pub fn resume(&self) -> io::Result<usize> {
        // SAFETY: plain value arguments; the snapshot handle is checked and owned below.
        let snapshot = owned_handle(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) })?;
        let mut entry = THREADENTRY32 {
            dwSize: size_of::<THREADENTRY32>() as u32,
            ..THREADENTRY32::default()
        };
        let mut resumed = 0;
        // SAFETY: the snapshot handle is open and `entry` carries its size.
        let mut found = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } != 0;
        while found {
            if entry.th32OwnerProcessID == self.pid {
                // SAFETY: plain value arguments; the result is checked and owned below.
                let raw = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if let Ok(thread) = owned_handle(raw) {
                    // SAFETY: the thread handle is open with the suspend and resume right.
                    if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                        return Err(io::Error::last_os_error());
                    }
                    resumed += 1;
                }
            }
            // SAFETY: as for `Thread32First`.
            found = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } != 0;
        }
        Ok(resumed)
    }
}

/// The pid of the process that created each process of the system, by its pid, from one
/// snapshot. Windows keeps the pid of the creator after the creator has ended, so the pid may
/// name another process by now.
///
/// # Errors
/// The error of the OS when the processes cannot be listed.
pub fn parent_pids() -> io::Result<HashMap<u32, u32>> {
    // SAFETY: plain value arguments; the snapshot handle is checked and owned below.
    let snapshot = owned_handle(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) })?;
    let mut entry = PROCESSENTRY32W {
        dwSize: size_of::<PROCESSENTRY32W>() as u32,
        ..PROCESSENTRY32W::default()
    };
    let mut parents = HashMap::new();
    // SAFETY: the snapshot handle is open and `entry` carries its size.
    let mut found = unsafe { Process32FirstW(snapshot.as_raw_handle(), &mut entry) } != 0;
    while found {
        parents.insert(entry.th32ProcessID, entry.th32ParentProcessID);
        // SAFETY: as for `Process32FirstW`.
        found = unsafe { Process32NextW(snapshot.as_raw_handle(), &mut entry) } != 0;
    }
    let err = io::Error::last_os_error();
    if !is_os_error(&err, &[ERROR_NO_MORE_FILES]) {
        return Err(err);
    }
    Ok(parents)
}

/// The system directory of Windows, such as `C:\Windows\System32`.
///
/// # Errors
/// The error of the OS.
pub fn system_directory() -> io::Result<PathBuf> {
    let mut buffer = vec![0_u16; 260];
    loop {
        let capacity = u32::try_from(buffer.len())
            .map_err(|_| io::Error::other("the system directory is too long"))?;
        // SAFETY: the buffer holds `capacity` units.
        let len = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), capacity) } as usize;
        if len == 0 {
            return Err(io::Error::last_os_error());
        }
        // A result that fits is its length without the terminator; one that does not is the
        // size it needs with the terminator.
        if len < buffer.len() {
            buffer.truncate(len);
            return Ok(PathBuf::from(OsString::from_wide(&buffer)));
        }
        buffer = vec![0_u16; len];
    }
}

/// The console control handler of the ctrl-break helper: every event is handled, so none ends
/// the helper.
unsafe extern "system" fn ignore_event(_event: u32) -> BOOL {
    1
}

/// Ignore every console control event in this process from now on.
///
/// # Errors
/// The error of the OS.
pub fn ignore_ctrl_events() -> io::Result<()> {
    // SAFETY: the handler is a function that lives as long as the process.
    check(unsafe { SetConsoleCtrlHandler(Some(ignore_event), 1) })
}

/// Send `CTRL_BREAK_EVENT` to the process group `group` on the console of `member`: this process
/// leaves its console, attaches to the one of `member`, generates the event and leaves again.
/// Only processes of the group that share that console receive it. Only a helper process without
/// a console, which called [`ignore_ctrl_events`] first, calls this.
///
/// # Errors
/// The error of the OS: a member without a console, or a refused event.
pub fn send_ctrl_break(member: &Process, group: u32) -> io::Result<()> {
    // SAFETY: no arguments; a process without a console makes it fail, which changes nothing.
    unsafe { FreeConsole() };
    // SAFETY: a plain value argument.
    check(unsafe { AttachConsole(member.pid()) })?;
    // SAFETY: plain value arguments.
    let sent = check(unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, group) });
    // SAFETY: no arguments.
    unsafe { FreeConsole() };
    sent
}
