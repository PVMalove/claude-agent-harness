//! What the migrator asks of the operating system: random bytes, the identity of this process,
//! whether another process lives, and durable writes.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::Path;

/// Fill `bytes` from the operating system's random source.
pub fn fill_random(bytes: &mut [u8]) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Read;

        File::open("/dev/urandom")?.read_exact(bytes)
    }
    #[cfg(windows)]
    {
        platform_ffi::random_bytes(bytes)
    }
}

/// 128 random bits as lower-case hex.
pub fn random_hex() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    fill_random(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// A random UUID (version 4) in the text form Python's `uuid.uuid4()` has.
pub fn uuid4() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    fill_random(&mut bytes)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

/// The host name, as Python's `socket.gethostname()` reports it.
pub fn hostname() -> String {
    #[cfg(unix)]
    {
        nix::unistd::gethostname()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default()
    }
    #[cfg(windows)]
    {
        std::env::var("COMPUTERNAME").unwrap_or_default()
    }
}

/// The PID namespace and the boot of this process, where the platform reveals them.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Environment {
    pub pid_namespace: Option<String>,
    pub boot_id: Option<String>,
}

pub fn environment() -> Environment {
    #[cfg(target_os = "linux")]
    {
        Environment {
            pid_namespace: fs::read_link("/proc/self/ns/pid")
                .ok()
                .map(|target| target.to_string_lossy().into_owned()),
            boot_id: fs::read_to_string("/proc/sys/kernel/random/boot_id")
                .ok()
                .map(|text| text.trim().to_owned())
                .filter(|text| !text.is_empty()),
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        Environment::default()
    }
}

/// Whether the process `pid` of this host and this PID namespace is alive; `None` when the
/// platform cannot tell.
pub fn process_alive(pid: i64) -> Option<bool> {
    #[cfg(unix)]
    {
        let pid = i32::try_from(pid).ok().filter(|pid| *pid > 0)?;
        match nix::sys::signal::kill(nix::unistd::Pid::from_raw(pid), None) {
            Ok(()) | Err(nix::errno::Errno::EPERM) => Some(true),
            Err(nix::errno::Errno::ESRCH) => Some(false),
            Err(_) => None,
        }
    }
    #[cfg(windows)]
    {
        let _ = pid;
        None
    }
}

/// Flush a directory entry to disk. Only Unix can: a directory has no handle on Windows.
pub fn sync_dir(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        File::open(path)?.sync_all()
    }
    #[cfg(windows)]
    {
        let _ = path;
        Ok(())
    }
}

/// Write `bytes` to a new file at `path` and flush it to disk.
pub fn write_durable(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

/// Replace `path` with `bytes` atomically: a temporary file next to it, flushed, then renamed.
pub fn replace_durable(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = parent.join(format!(".{name}.{}.tmp", random_hex()?));
    let written = write_durable(&temporary, bytes).and_then(|()| fs::rename(&temporary, path));
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    written?;
    sync_dir(parent)
}
