//! Win32 calls behind a safe API. This is the only module of the workspace with `unsafe` code.
//! Each `unsafe` block states why its pointers and handles are valid.

use std::ffi::{OsStr, c_void};
use std::fs::File;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{HANDLE, INVALID_HANDLE_VALUE, LocalFree};
use windows_sys::Win32::Security::Authorization::{
    ConvertSecurityDescriptorToStringSecurityDescriptorW, ConvertSidToStringSidW,
    ConvertStringSecurityDescriptorToSecurityDescriptorW, GetNamedSecurityInfoW, SDDL_REVISION_1,
    SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::Cryptography::{
    BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
};
use windows_sys::Win32::Security::{
    DACL_SECURITY_INFORMATION, GetTokenInformation, PSECURITY_DESCRIPTOR, PSID,
    SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER, TokenUser,
};
use windows_sys::Win32::Storage::FileSystem::{
    CREATE_NEW, CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_GENERIC_WRITE, FILE_SHARE_READ,
};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
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
    parse_dacl(&sddl).map_err(io::Error::other)
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
