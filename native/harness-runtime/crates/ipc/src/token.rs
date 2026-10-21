//! The capability token: 256 random bits, hex-encoded, written to a file only the user can access
//! (`0600`; on Windows a DACL for the user alone) at start.

use std::io;

/// Fill `bytes` from the operating system's random source: `/dev/urandom`, or `BCryptGenRandom`
/// on Windows.
fn fill_random(bytes: &mut [u8]) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Read;

        std::fs::File::open("/dev/urandom")?.read_exact(bytes)
    }
    #[cfg(windows)]
    {
        platform_ffi::random_bytes(bytes)
    }
}

/// A fresh token from the operating system's random source.
pub fn generate_token() -> io::Result<String> {
    let mut bytes = [0_u8; 32];
    fill_random(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Compare two tokens without stopping at the first different byte.
pub fn tokens_equal(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
            == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_is_256_bits_of_lower_case_hex_and_never_repeats() {
        let first = generate_token().unwrap();

        assert_eq!(first.len(), 64);
        assert!(
            first
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        );
        assert_ne!(first, generate_token().unwrap());
    }

    #[test]
    fn tokens_are_equal_only_when_every_byte_is() {
        assert!(tokens_equal(b"abc", b"abc"));
        assert!(!tokens_equal(b"abc", b"abd"));
        assert!(!tokens_equal(b"abc", b"abcd"));
    }
}
