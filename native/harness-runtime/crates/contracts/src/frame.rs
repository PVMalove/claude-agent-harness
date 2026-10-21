//! The frame: a big-endian `u32` length followed by that many bytes of UTF-8 JSON (ADR 0021,
//! section 3). One request is one frame and one response is one frame.

use std::fmt;
use std::io::{self, Read, Write};

use serde::{Serialize, de::DeserializeOwned};

/// The largest frame body: 16 MiB.
pub const MAX_FRAME_LEN: u32 = 16 * 1024 * 1024;

/// Why a frame could not be read or written.
#[derive(Debug)]
pub enum FrameError {
    /// The peer closed the connection before the first byte of a frame.
    Closed,
    /// The length prefix names more than [`MAX_FRAME_LEN`] bytes; the body is never read.
    TooLarge(u32),
    /// The connection ended inside a frame.
    Truncated,
    /// Any other transport failure.
    Io(io::Error),
}

impl fmt::Display for FrameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Closed => write!(f, "the peer closed the connection"),
            Self::TooLarge(len) => {
                write!(
                    f,
                    "frame of {len} bytes exceeds the {MAX_FRAME_LEN} byte limit"
                )
            }
            Self::Truncated => write!(f, "the connection ended inside a frame"),
            Self::Io(err) => write!(f, "transport failure: {err}"),
        }
    }
}

impl std::error::Error for FrameError {}

/// Write one frame: the length prefix, then the body.
pub fn write_frame<W: Write>(writer: &mut W, body: &[u8]) -> Result<(), FrameError> {
    let len = u32::try_from(body.len())
        .ok()
        .filter(|len| *len <= MAX_FRAME_LEN)
        .ok_or(FrameError::TooLarge(
            u32::try_from(body.len()).unwrap_or(u32::MAX),
        ))?;
    writer
        .write_all(&len.to_be_bytes())
        .map_err(FrameError::Io)?;
    writer.write_all(body).map_err(FrameError::Io)?;
    writer.flush().map_err(FrameError::Io)
}

/// Read one frame body. An oversized length is refused before any body byte is read.
pub fn read_frame<R: Read>(reader: &mut R) -> Result<Vec<u8>, FrameError> {
    let mut prefix = [0_u8; 4];
    let mut filled = 0;
    while filled < prefix.len() {
        match reader.read(&mut prefix[filled..]) {
            Ok(0) if filled == 0 => return Err(FrameError::Closed),
            Ok(0) => return Err(FrameError::Truncated),
            Ok(count) => filled += count,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
            Err(err) => return Err(FrameError::Io(err)),
        }
    }
    let len = u32::from_be_bytes(prefix);
    if len > MAX_FRAME_LEN {
        return Err(FrameError::TooLarge(len));
    }
    let mut body = vec![0_u8; len as usize];
    reader.read_exact(&mut body).map_err(|err| {
        if err.kind() == io::ErrorKind::UnexpectedEof {
            FrameError::Truncated
        } else {
            FrameError::Io(err)
        }
    })?;
    Ok(body)
}

/// Serialize a message to the JSON body of a frame.
pub fn encode<T: Serialize>(message: &T) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(message)
}

/// Parse the JSON body of a frame; invalid UTF-8 and invalid JSON are both an error.
pub fn decode<T: DeserializeOwned>(body: &[u8]) -> Result<T, serde_json::Error> {
    serde_json::from_slice(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_frame_is_a_big_endian_length_and_the_body() {
        let mut wire = Vec::new();

        write_frame(&mut wire, b"{\"a\":1}").unwrap();

        assert_eq!(wire, [0, 0, 0, 7, b'{', b'"', b'a', b'"', b':', b'1', b'}']);
        assert_eq!(read_frame(&mut wire.as_slice()).unwrap(), b"{\"a\":1}");
    }

    #[test]
    fn a_clean_close_is_not_a_truncated_frame() {
        assert!(matches!(
            read_frame(&mut [].as_slice()),
            Err(FrameError::Closed)
        ));
        assert!(matches!(
            read_frame(&mut [0, 0].as_slice()),
            Err(FrameError::Truncated)
        ));
        assert!(matches!(
            read_frame(&mut [0, 0, 0, 5, b'a'].as_slice()),
            Err(FrameError::Truncated)
        ));
    }

    #[test]
    fn an_oversized_length_is_refused_before_the_body_is_read() {
        let over = (MAX_FRAME_LEN + 1).to_be_bytes();

        let refusal = read_frame(&mut over.as_slice());

        assert!(matches!(refusal, Err(FrameError::TooLarge(len)) if len == MAX_FRAME_LEN + 1));
    }

    #[test]
    fn the_limit_itself_is_a_valid_length() {
        let mut wire = MAX_FRAME_LEN.to_be_bytes().to_vec();
        wire.resize(4 + MAX_FRAME_LEN as usize, b' ');

        assert_eq!(
            read_frame(&mut wire.as_slice()).unwrap().len(),
            MAX_FRAME_LEN as usize
        );
    }

    #[test]
    fn invalid_utf8_is_not_a_message() {
        let body = [b'"', 0xff, b'"'];

        assert!(decode::<String>(&body).is_err());
    }
}
