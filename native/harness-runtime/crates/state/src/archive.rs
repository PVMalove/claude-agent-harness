//! A deterministic ustar archive (POSIX.1-1988), written without a crate.
//!
//! The rollback keeps what format v3 cannot hold in `state/runtime-export/<store_id>.tar`
//! (ADR 0020, section 5). The same members give the same bytes: the members are sorted by name,
//! and every header has mtime 0, uid and gid 0, no owner names and mode `0444`. The archive file
//! is immutable: it is written once under a temporary name, flushed, renamed and made read-only.

use std::fs;
use std::io;
use std::path::Path;

use crate::error::{Error, Result};
use crate::platform;

const BLOCK: usize = 512;

/// One regular file of the archive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Member {
    pub name: String,
    pub bytes: Vec<u8>,
}

fn octal(field: &mut [u8], value: u64) {
    // The field holds `len - 1` octal digits, zero-padded, and a NUL.
    let digits = field.len() - 1;
    let text = format!("{value:0digits$o}");
    field[..digits].copy_from_slice(&text.as_bytes()[text.len() - digits..]);
    field[digits] = 0;
}

fn header(name: &str, size: u64) -> [u8; BLOCK] {
    let mut block = [0_u8; BLOCK];
    block[..name.len()].copy_from_slice(name.as_bytes());
    octal(&mut block[100..108], 0o444);
    octal(&mut block[108..116], 0);
    octal(&mut block[116..124], 0);
    octal(&mut block[124..136], size);
    octal(&mut block[136..148], 0);
    block[156] = b'0';
    block[257..263].copy_from_slice(b"ustar\0");
    block[263..265].copy_from_slice(b"00");
    // The checksum is computed with its own field read as eight spaces.
    block[148..156].copy_from_slice(b"        ");
    let sum: u64 = block.iter().map(|byte| u64::from(*byte)).sum();
    let text = format!("{sum:06o}\0 ");
    block[148..156].copy_from_slice(text.as_bytes());
    block
}

/// The bytes of a ustar archive of `members`, sorted by name.
///
/// # Panics
/// When a name is empty, longer than 99 bytes or not ASCII: the members are named by this crate.
pub fn ustar(members: &[Member]) -> Vec<u8> {
    let mut sorted: Vec<&Member> = members.iter().collect();
    sorted.sort_by(|a, b| a.name.cmp(&b.name));
    let mut out = Vec::new();
    for member in sorted {
        assert!(
            !member.name.is_empty() && member.name.len() < 100 && member.name.is_ascii(),
            "an archive member name must be short ASCII: {:?}",
            member.name
        );
        out.extend_from_slice(&header(&member.name, member.bytes.len() as u64));
        out.extend_from_slice(&member.bytes);
        let padding = (BLOCK - member.bytes.len() % BLOCK) % BLOCK;
        out.resize(out.len() + padding, 0);
    }
    out.resize(out.len() + 2 * BLOCK, 0);
    out
}

/// The names of the members of an archive `ustar` wrote, in archive order.
pub fn names(archive: &[u8]) -> Vec<String> {
    let mut found = Vec::new();
    let mut at = 0;
    while at + BLOCK <= archive.len() {
        let block = &archive[at..at + BLOCK];
        if block.iter().all(|byte| *byte == 0) {
            break;
        }
        let end = block[..100]
            .iter()
            .position(|byte| *byte == 0)
            .unwrap_or(100);
        found.push(String::from_utf8_lossy(&block[..end]).into_owned());
        let size_text = String::from_utf8_lossy(&block[124..135]).into_owned();
        let size = usize::from_str_radix(size_text.trim_matches(char::from(0)), 8).unwrap_or(0);
        at += BLOCK + size.div_ceil(BLOCK) * BLOCK;
    }
    found
}

fn make_read_only(path: &Path) -> io::Result<()> {
    let mut permissions = fs::metadata(path)?.permissions();
    if !permissions.readonly() {
        permissions.set_readonly(true);
        fs::set_permissions(path, permissions)?;
    }
    Ok(())
}

/// Install `bytes` at `path` once: a temporary file next to it, flushed, renamed and made
/// read-only. A file already there with the same bytes is the result of an earlier attempt and
/// is kept.
///
/// # Errors
/// `Error::Invariant` when a file with other bytes is already there; `Error::Interrupted` when the
/// file cannot be written.
pub fn install(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    match fs::read(path) {
        Ok(existing) if existing == bytes => {
            make_read_only(path)?;
            return Ok(());
        }
        Ok(_) => {
            return Err(Error::invariant(format!(
                "{} already exists with other bytes; an export archive is never replaced",
                path.display()
            )));
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => return Err(err.into()),
    }
    fs::create_dir_all(parent)?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = parent.join(format!(".{name}.{}.tmp", platform::random_hex()?));
    let written = platform::write_durable(&temporary, bytes)
        .and_then(|()| make_read_only(&temporary))
        .and_then(|()| fs::rename(&temporary, path));
    if let Err(err) = written {
        let _ = fs::remove_file(&temporary);
        return Err(err.into());
    }
    platform::sync_dir(parent)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_members_are_sorted_and_padded_to_whole_blocks() {
        let archive = ustar(&[
            Member {
                name: "b.txt".to_owned(),
                bytes: b"bb".to_vec(),
            },
            Member {
                name: "a.txt".to_owned(),
                bytes: vec![b'a'; 513],
            },
        ]);

        assert_eq!(names(&archive), ["a.txt", "b.txt"]);
        // Two headers, two blocks of a.txt, one block of b.txt and the two end blocks.
        assert_eq!(archive.len(), 7 * BLOCK);
        assert_eq!(&archive[257..263], b"ustar\0");
        assert_eq!(&archive[100..108], b"0000444\0");
    }

    #[test]
    fn the_checksum_is_the_sum_of_the_header_with_spaces_in_its_field() {
        let block = header("x", 1);
        let mut spaced = block;
        spaced[148..156].copy_from_slice(b"        ");
        let sum: u64 = spaced.iter().map(|byte| u64::from(*byte)).sum();

        assert_eq!(&block[148..156], format!("{sum:06o}\0 ").as_bytes());
    }
}
