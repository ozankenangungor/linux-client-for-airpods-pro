//! Lexical path checks only. Ownership, canonicalization and inode proofs are Python effects.

pub const UNIX_SOCKET_PATH_MAX_BYTES: usize = 107;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SocketPathError {
    NotAbsoluteFile,
    TooLong,
}

pub fn validate_socket_path(path: &[u8]) -> Result<(), SocketPathError> {
    if path.first() != Some(&b'/')
        || path.ends_with(b"/")
        || path.ends_with(b"/.")
        || path.ends_with(b"/..")
    {
        return Err(SocketPathError::NotAbsoluteFile);
    }
    if path.len() > UNIX_SOCKET_PATH_MAX_BYTES {
        return Err(SocketPathError::TooLong);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn byte_boundary_and_terminal_components() {
        assert!(validate_socket_path(format!("/{}", "a".repeat(106)).as_bytes()).is_ok());
        assert_eq!(
            validate_socket_path(format!("/{}", "a".repeat(107)).as_bytes()),
            Err(SocketPathError::TooLong)
        );
        for path in [b"".as_slice(), b"relative", b"/", b"/.", b"/..", b"/foo/"] {
            assert_eq!(
                validate_socket_path(path),
                Err(SocketPathError::NotAbsoluteFile)
            );
        }
        assert!(validate_socket_path("/ü.sock".as_bytes()).is_ok());
    }
}
