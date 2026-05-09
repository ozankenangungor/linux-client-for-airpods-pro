//! Pure production-session lifecycle policy; identities are private to the native bridge.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ProductionState {
    Closed = 0,
    Opening = 1,
    Ready = 2,
    Starting = 3,
    Streaming = 4,
    Stopping = 5,
    Failed = 6,
}

impl TryFrom<u8> for ProductionState {
    type Error = ProductionError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ProductionState::*;
        Ok(match value {
            0 => Closed,
            1 => Opening,
            2 => Ready,
            3 => Starting,
            4 => Streaming,
            5 => Stopping,
            6 => Failed,
            _ => return Err(ProductionError::UnknownIdentity),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ProductionOperation {
    Open = 0,
    Start = 1,
    Receive = 2,
    Stop = 3,
    Close = 4,
}

impl TryFrom<u8> for ProductionOperation {
    type Error = ProductionError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ProductionOperation::*;
        Ok(match value {
            0 => Open,
            1 => Start,
            2 => Receive,
            3 => Stop,
            4 => Close,
            _ => return Err(ProductionError::UnknownIdentity),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ProductionEvent {
    OpenBegin = 0,
    OpenSucceeded = 1,
    OperationFailed = 2,
    StartBegin = 3,
    StartSucceeded = 4,
    ReceiveActivationFailed = 5,
    StopBegin = 6,
    StopSucceeded = 7,
    CloseFinalized = 8,
}

impl TryFrom<u8> for ProductionEvent {
    type Error = ProductionError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ProductionEvent::*;
        Ok(match value {
            0 => OpenBegin,
            1 => OpenSucceeded,
            2 => OperationFailed,
            3 => StartBegin,
            4 => StartSucceeded,
            5 => ReceiveActivationFailed,
            6 => StopBegin,
            7 => StopSucceeded,
            8 => CloseFinalized,
            _ => return Err(ProductionError::UnknownIdentity),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProductionError {
    UnknownIdentity,
    InvalidOperation,
    InvalidTransition,
}

/// Validate an operation without changing the state. Single-use open and other
/// resource/consumer guards remain the caller's responsibility.
pub fn production_operation(
    state: ProductionState,
    operation: ProductionOperation,
) -> Result<ProductionState, ProductionError> {
    use ProductionOperation::*;
    use ProductionState::*;
    match (state, operation) {
        (Closed, Open) | (Ready, Start) | (Streaming, Receive | Stop) | (_, Close) => Ok(state),
        _ => Err(ProductionError::InvalidOperation),
    }
}

/// Apply one lifecycle event. `cleanup_complete` is used only for CloseFinalized;
/// the caller proves resource release before supplying it.
pub fn production_transition(
    state: ProductionState,
    event: ProductionEvent,
    cleanup_complete: bool,
) -> Result<ProductionState, ProductionError> {
    use ProductionEvent::*;
    use ProductionState::*;
    match (state, event) {
        (Closed, OpenBegin) => Ok(Opening),
        (Opening, OpenSucceeded) => Ok(Ready),
        // An unlocked receive can mark FAILED while stop is still cleaning up.
        (Opening | Starting | Stopping | Failed, OperationFailed) => Ok(Failed),
        (Ready, StartBegin) => Ok(Starting),
        (Starting, StartSucceeded) => Ok(Streaming),
        // receive_report checks STREAMING before awaiting without the lifecycle lock;
        // a concurrent stop/close may change state before its failure is observed.
        (_, ReceiveActivationFailed) => Ok(Failed),
        (Streaming, StopBegin) => Ok(Stopping),
        (Stopping, StopSucceeded) => Ok(Ready),
        (_, CloseFinalized) if state != Closed => {
            Ok(if cleanup_complete { Closed } else { Failed })
        }
        _ => Err(ProductionError::InvalidTransition),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exhaustive_operation_table() {
        let legal = [(0, 0), (2, 1), (4, 2), (4, 3)];
        for s in 0..7 {
            let state = ProductionState::try_from(s).unwrap();
            for o in 0..5 {
                let operation = ProductionOperation::try_from(o).unwrap();
                let expected = if o == 4 || legal.contains(&(s, o)) {
                    Ok(state)
                } else {
                    Err(ProductionError::InvalidOperation)
                };
                assert_eq!(production_operation(state, operation), expected, "{s} {o}");
            }
        }
    }

    #[test]
    fn exhaustive_transition_table_for_both_cleanup_outcomes() {
        let rules = [
            (0, 0, 1),
            (1, 1, 2),
            (1, 2, 6),
            (3, 2, 6),
            (5, 2, 6),
            (6, 2, 6),
            (2, 3, 3),
            (3, 4, 4),
            (4, 6, 5),
            (5, 7, 2),
        ];
        for s in 0..7 {
            let state = ProductionState::try_from(s).unwrap();
            for e in 0..9 {
                let event = ProductionEvent::try_from(e).unwrap();
                for cleanup_complete in [false, true] {
                    let expected = if e == 5 {
                        Ok(ProductionState::Failed)
                    } else if e == 8 && s != 0 {
                        Ok(
                            ProductionState::try_from(if cleanup_complete { 0 } else { 6 })
                                .unwrap(),
                        )
                    } else if let Some(&(_, _, next)) =
                        rules.iter().find(|&&(a, b, _)| a == s && b == e)
                    {
                        Ok(ProductionState::try_from(next).unwrap())
                    } else {
                        Err(ProductionError::InvalidTransition)
                    };
                    assert_eq!(
                        production_transition(state, event, cleanup_complete),
                        expected,
                        "{s} {e} {cleanup_complete}"
                    );
                }
            }
        }
    }

    #[test]
    fn rejects_every_unknown_u8_identity() {
        for s in 7..=u8::MAX {
            assert_eq!(
                ProductionState::try_from(s),
                Err(ProductionError::UnknownIdentity)
            );
        }
        for o in 5..=u8::MAX {
            assert_eq!(
                ProductionOperation::try_from(o),
                Err(ProductionError::UnknownIdentity)
            );
        }
        for e in 9..=u8::MAX {
            assert_eq!(
                ProductionEvent::try_from(e),
                Err(ProductionError::UnknownIdentity)
            );
        }
    }
}
