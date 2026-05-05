//! Pure activation transition policy. Numeric identities match the private Python bridge.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ActivationState {
    DescriptorsReady = 0,
    StopHeadSent = 1,
    StopHeadAcknowledged = 2,
    Connect0Sent = 3,
    Caps0Sent = 4,
    Connect4Sent = 5,
    Connect4Acknowledged = 6,
    Caps4Sent = 7,
    HrOnSent = 8,
    StartHrSent = 9,
    StartAcknowledged = 10,
    StreamComplete = 11,
    StopHrSent = 12,
    StopHrAcknowledged = 13,
    HrOffSent = 14,
    Complete = 15,
}

impl TryFrom<u8> for ActivationState {
    type Error = TransitionError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ActivationState::*;
        Ok(match value {
            0 => DescriptorsReady,
            1 => StopHeadSent,
            2 => StopHeadAcknowledged,
            3 => Connect0Sent,
            4 => Caps0Sent,
            5 => Connect4Sent,
            6 => Connect4Acknowledged,
            7 => Caps4Sent,
            8 => HrOnSent,
            9 => StartHrSent,
            10 => StartAcknowledged,
            11 => StreamComplete,
            12 => StopHrSent,
            13 => StopHrAcknowledged,
            14 => HrOffSent,
            15 => Complete,
            _ => return Err(TransitionError::UnknownIdentity),
        })
    }
}

/// Neutral identities in Python HeartRateCommand declaration order; no wire bytes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ActivationCommand {
    StopHead = 0,
    Connect0 = 1,
    Caps0 = 2,
    Connect4 = 3,
    Caps4 = 4,
    HrOn = 5,
    StartHr = 6,
    StopHr = 7,
    HrOff = 8,
}

impl TryFrom<u8> for ActivationCommand {
    type Error = TransitionError;
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ActivationCommand::*;
        Ok(match value {
            0 => StopHead,
            1 => Connect0,
            2 => Caps0,
            3 => Connect4,
            4 => Caps4,
            5 => HrOn,
            6 => StartHr,
            7 => StopHr,
            8 => HrOff,
            _ => return Err(TransitionError::UnknownIdentity),
        })
    }
}

/// Bit n represents command identity n above; only bits 0..=8 are valid.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SentCommands(u16);

impl TryFrom<u16> for SentCommands {
    type Error = TransitionError;
    fn try_from(bits: u16) -> Result<Self, Self::Error> {
        if bits & !0x01ff != 0 {
            return Err(TransitionError::UnknownSentBits);
        }
        Ok(Self(bits))
    }
}

impl SentCommands {
    pub const fn contains(self, command: ActivationCommand) -> bool {
        self.0 & (1 << command as u8) != 0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ActivationEvent {
    StopHeadAck = 0,
    Connect4Ack = 1,
    StartHrAck = 2,
    StreamComplete = 3,
    StopHrAck = 4,
    Complete = 5,
}

impl TryFrom<u8> for ActivationEvent {
    type Error = TransitionError;
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        use ActivationEvent::*;
        Ok(match value {
            0 => StopHeadAck,
            1 => Connect4Ack,
            2 => StartHrAck,
            3 => StreamComplete,
            4 => StopHrAck,
            5 => Complete,
            _ => return Err(TransitionError::UnknownIdentity),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransitionError {
    InvalidActivation,
    ActivationOnCleanupPath,
    DuplicateCleanup,
    StopHrRequiresStartHr,
    HrOffRequiresHrOn,
    InvalidAdvance,
    UnknownIdentity,
    UnknownSentBits,
}

pub fn plan_activation_send(
    state: ActivationState,
    sent: SentCommands,
    command: ActivationCommand,
) -> Result<ActivationState, TransitionError> {
    use ActivationCommand::*;
    use ActivationState::*;
    if sent.contains(command) {
        return Err(TransitionError::InvalidActivation);
    }
    match (state, command) {
        (DescriptorsReady, StopHead) => Ok(StopHeadSent),
        (StopHeadAcknowledged, Connect0) => Ok(Connect0Sent),
        (Connect0Sent, Caps0) => Ok(Caps0Sent),
        (Caps0Sent, Connect4) => Ok(Connect4Sent),
        (Connect4Acknowledged, Caps4) => Ok(Caps4Sent),
        (Caps4Sent, HrOn) => Ok(HrOnSent),
        (HrOnSent, StartHr) => Ok(StartHrSent),
        _ => Err(TransitionError::InvalidActivation),
    }
}

pub fn plan_cleanup_send(
    _state: ActivationState,
    sent: SentCommands,
    command: ActivationCommand,
) -> Result<ActivationState, TransitionError> {
    use ActivationCommand::*;
    match command {
        StopHr | HrOff => {}
        _ => return Err(TransitionError::ActivationOnCleanupPath),
    }
    if sent.contains(command) {
        return Err(TransitionError::DuplicateCleanup);
    }
    match command {
        StopHr if !sent.contains(StartHr) => Err(TransitionError::StopHrRequiresStartHr),
        HrOff if !sent.contains(HrOn) => Err(TransitionError::HrOffRequiresHrOn),
        StopHr => Ok(ActivationState::StopHrSent),
        HrOff => Ok(ActivationState::HrOffSent),
        _ => unreachable!(),
    }
}

pub fn advance(
    state: ActivationState,
    event: ActivationEvent,
) -> Result<ActivationState, TransitionError> {
    use ActivationEvent::*;
    use ActivationState::*;
    match (state, event) {
        (StopHeadSent, StopHeadAck) => Ok(StopHeadAcknowledged),
        (Connect4Sent, Connect4Ack) => Ok(Connect4Acknowledged),
        (StartHrSent, StartHrAck) => Ok(StartAcknowledged),
        (StartAcknowledged, ActivationEvent::StreamComplete) => Ok(ActivationState::StreamComplete),
        (StopHrSent, StopHrAck) => Ok(StopHrAcknowledged),
        (HrOffSent, ActivationEvent::Complete) => Ok(ActivationState::Complete),
        _ => Err(TransitionError::InvalidAdvance),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exhaustive_transition_tables() {
        let sends = [
            (0, 0, 1),
            (2, 1, 3),
            (3, 2, 4),
            (4, 3, 5),
            (6, 4, 7),
            (7, 5, 8),
            (8, 6, 9),
        ];
        let advances = [
            (1, 0, 2),
            (5, 1, 6),
            (9, 2, 10),
            (10, 3, 11),
            (12, 4, 13),
            (14, 5, 15),
        ];
        for s in 0..16u8 {
            let state = ActivationState::try_from(s).unwrap();
            for c in 0..9u8 {
                let command = ActivationCommand::try_from(c).unwrap();
                for mask in 0..512u16 {
                    let sent = SentCommands::try_from(mask).unwrap();
                    let expected = sends.iter().find(|&&(a, b, _)| a == s && b == c);
                    let actual = plan_activation_send(state, sent, command);
                    assert_eq!(
                        actual,
                        match expected {
                            Some(&(_, _, next)) if mask & (1 << c) == 0 =>
                                Ok(ActivationState::try_from(next).unwrap()),
                            _ => Err(TransitionError::InvalidActivation),
                        },
                        "activation {s} {c} {mask}"
                    );
                    let cleanup = plan_cleanup_send(state, sent, command);
                    let expected_cleanup = if c < 7 {
                        Err(TransitionError::ActivationOnCleanupPath)
                    } else if mask & (1 << c) != 0 {
                        Err(TransitionError::DuplicateCleanup)
                    } else if c == 7 && mask & (1 << 6) == 0 {
                        Err(TransitionError::StopHrRequiresStartHr)
                    } else if c == 8 && mask & (1 << 5) == 0 {
                        Err(TransitionError::HrOffRequiresHrOn)
                    } else {
                        Ok(ActivationState::try_from(if c == 7 { 12 } else { 14 }).unwrap())
                    };
                    assert_eq!(cleanup, expected_cleanup, "cleanup {s} {c} {mask}");
                }
            }
            for e in 0..6u8 {
                let event = ActivationEvent::try_from(e).unwrap();
                let expected = advances.iter().find(|&&(a, b, _)| a == s && b == e);
                assert_eq!(
                    advance(state, event),
                    match expected {
                        Some(&(_, _, next)) => Ok(ActivationState::try_from(next).unwrap()),
                        None => Err(TransitionError::InvalidAdvance),
                    }
                );
            }
        }
    }

    #[test]
    fn rejects_unknown_identities_and_bits() {
        for state in 16..=u8::MAX {
            assert_eq!(
                ActivationState::try_from(state),
                Err(TransitionError::UnknownIdentity)
            );
        }
        for command in 9..=u8::MAX {
            assert_eq!(
                ActivationCommand::try_from(command),
                Err(TransitionError::UnknownIdentity)
            );
        }
        for event in 6..=u8::MAX {
            assert_eq!(
                ActivationEvent::try_from(event),
                Err(TransitionError::UnknownIdentity)
            );
        }
        for bits in 512..=u16::MAX {
            if bits & !0x01ff != 0 {
                assert_eq!(
                    SentCommands::try_from(bits),
                    Err(TransitionError::UnknownSentBits)
                );
            }
        }
    }
}
