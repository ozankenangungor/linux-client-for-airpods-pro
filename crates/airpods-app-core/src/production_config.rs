//! Numeric production windows. Session construction and timeout execution stay in Python.

pub const DEFAULT_DESCRIPTOR_TIMEOUT: f64 = 30.0;
pub const DEFAULT_DAEMON_OPERATION_TIMEOUT: f64 = 150.0;
const OPEN_DBUS_WINDOWS: f64 = 9.0;
const OUTER_TIMEOUT_MARGIN: f64 = 10.0;

#[derive(Clone, Copy, Debug)]
pub struct Timeouts {
    pub descriptor: f64,
    pub dbus: f64,
    pub connect: f64,
    pub handshake: f64,
    pub start: f64,
    pub stop: f64,
    pub daemon: f64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimeoutError {
    NonPositiveOrNonFinite,
    BelowMinimum,
}

pub fn minimum(timeout: Timeouts) -> f64 {
    let cleanup = timeout.stop + 5.0 * timeout.dbus;
    let open =
        OPEN_DBUS_WINDOWS * timeout.dbus + timeout.connect + timeout.handshake + timeout.descriptor;
    (open + cleanup)
        .max(timeout.start + cleanup)
        .max(timeout.stop + cleanup)
        .max(cleanup)
        + OUTER_TIMEOUT_MARGIN
}

pub fn validate(timeout: Timeouts) -> Result<f64, TimeoutError> {
    for value in [
        timeout.descriptor,
        timeout.dbus,
        timeout.connect,
        timeout.handshake,
        timeout.start,
        timeout.stop,
        timeout.daemon,
    ] {
        if !value.is_finite() || value <= 0.0 {
            return Err(TimeoutError::NonPositiveOrNonFinite);
        }
    }
    let floor = minimum(timeout);
    if timeout.daemon < floor {
        Err(TimeoutError::BelowMinimum)
    } else {
        Ok(floor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn standard() -> Timeouts {
        Timeouts {
            descriptor: 30.0,
            dbus: 5.0,
            connect: 10.0,
            handshake: 5.0,
            start: 5.0,
            stop: 5.0,
            daemon: 150.0,
        }
    }

    #[test]
    fn exact_windows_and_daemon_edges() {
        assert_eq!(DEFAULT_DESCRIPTOR_TIMEOUT, 30.0);
        assert_eq!(DEFAULT_DAEMON_OPERATION_TIMEOUT, 150.0);
        let mut t = standard();
        let floor = minimum(t);
        assert_eq!(floor, 130.0);
        assert!(validate(t).is_ok());
        t.daemon = floor - f64::EPSILON * floor;
        assert_eq!(validate(t), Err(TimeoutError::BelowMinimum));
        t.daemon = floor;
        assert!(validate(t).is_ok());
        t.daemon = floor + f64::EPSILON * floor;
        assert!(validate(t).is_ok());
    }

    #[test]
    fn every_field_rejects_nonfinite_and_nonpositive() {
        for index in 0..7 {
            for bad in [0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
                let mut t = standard();
                match index {
                    0 => t.descriptor = bad,
                    1 => t.dbus = bad,
                    2 => t.connect = bad,
                    3 => t.handshake = bad,
                    4 => t.start = bad,
                    5 => t.stop = bad,
                    _ => t.daemon = bad,
                }
                assert_eq!(validate(t), Err(TimeoutError::NonPositiveOrNonFinite));
            }
        }
    }
}
