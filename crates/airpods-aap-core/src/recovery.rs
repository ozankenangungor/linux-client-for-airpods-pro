//! Pure classification of coexistence failures for production-session recovery.
//! Category identities follow CoexistenceCategory declaration order in Python.

/// The immediate cause of a coexistence failure. The caller classifies Python
/// exception types before passing only their relevant metadata here.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum RecoveryCauseKind {
    None = 0,
    NestedCoexistenceResult = 1,
    DirectRecoverable = 2,
    OsError = 3,
    DBusError = 4,
    Other = 5,
}

impl TryFrom<u8> for RecoveryCauseKind {
    type Error = RecoveryError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        Ok(match value {
            0 => Self::None,
            1 => Self::NestedCoexistenceResult,
            2 => Self::DirectRecoverable,
            3 => Self::OsError,
            4 => Self::DBusError,
            5 => Self::Other,
            _ => return Err(RecoveryError::UnknownIdentity),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecoveryError {
    UnknownIdentity,
}

/// Classify a coexistence failure using the category's Python declaration-order
/// identity (0..=16). Unknown identities are errors, never recoverable.
/// `nested_recoverable`, `errno`, and `dbus_error_name` are consulted only for
/// their corresponding cause kind. Linux errno values are fixed numeric identities.
pub fn classify_coexistence_recovery(
    category: u8,
    cause: RecoveryCauseKind,
    nested_recoverable: bool,
    errno: Option<i32>,
    dbus_error_name: Option<&str>,
) -> Result<bool, RecoveryError> {
    let eligible = match category {
        // Explicitly recoverable coexistence categories.
        0 | 1 | 2 | 4 | 6 | 9 | 11 | 12 | 13 | 14 | 15 => true,
        3 | 5 | 7 | 8 | 10 | 16 => false,
        _ => return Err(RecoveryError::UnknownIdentity),
    };
    if !eligible {
        return Ok(false);
    }

    Ok(match cause {
        RecoveryCauseKind::None | RecoveryCauseKind::DirectRecoverable => true,
        RecoveryCauseKind::NestedCoexistenceResult => nested_recoverable,
        RecoveryCauseKind::OsError => matches!(
            errno,
            Some(
                99 | 11
                    | 16
                    | 103
                    | 111
                    | 104
                    | 112
                    | 113
                    | 4
                    | 100
                    | 102
                    | 101
                    | 19
                    | 2
                    | 107
                    | 110
            )
        ),
        RecoveryCauseKind::DBusError => matches!(
            dbus_error_name,
            Some(
                "org.bluez.Error.NotConnected"
                    | "org.bluez.Error.NotReady"
                    | "org.freedesktop.DBus.Error.Disconnected"
                    | "org.freedesktop.DBus.Error.NameHasNoOwner"
                    | "org.freedesktop.DBus.Error.NoNetwork"
                    | "org.freedesktop.DBus.Error.NoReply"
                    | "org.freedesktop.DBus.Error.NoServer"
                    | "org.freedesktop.DBus.Error.ServiceUnknown"
                    | "org.freedesktop.DBus.Error.Timeout"
            )
        ),
        RecoveryCauseKind::Other => false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const RECOVERABLE_CATEGORIES: [u8; 11] = [0, 1, 2, 4, 6, 9, 11, 12, 13, 14, 15];
    const TRANSIENT_ERRNOS: [i32; 16] = [
        99, 11, 16, 103, 111, 104, 112, 113, 4, 100, 102, 101, 19, 2, 107, 110,
    ];
    const TRANSIENT_DBUS_NAMES: [&str; 9] = [
        "org.bluez.Error.NotConnected",
        "org.bluez.Error.NotReady",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoNetwork",
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.NoServer",
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.Timeout",
    ];

    #[test]
    fn cause_identities_and_every_unknown_u8() {
        let expected = [
            RecoveryCauseKind::None,
            RecoveryCauseKind::NestedCoexistenceResult,
            RecoveryCauseKind::DirectRecoverable,
            RecoveryCauseKind::OsError,
            RecoveryCauseKind::DBusError,
            RecoveryCauseKind::Other,
        ];
        for (id, cause) in expected.into_iter().enumerate() {
            assert_eq!(cause as u8, id as u8);
            assert_eq!(RecoveryCauseKind::try_from(id as u8), Ok(cause));
        }
        for id in 6..=u8::MAX {
            assert_eq!(
                RecoveryCauseKind::try_from(id),
                Err(RecoveryError::UnknownIdentity)
            );
        }
    }

    #[test]
    fn exhaustive_category_cause_and_nested_result_matrix() {
        for category in 0..=16 {
            for cause_id in 0..=5 {
                let cause = RecoveryCauseKind::try_from(cause_id).unwrap();
                for nested_recoverable in [false, true] {
                    let expected = RECOVERABLE_CATEGORIES.contains(&category)
                        && match cause_id {
                            0 | 2 | 3 | 4 => true,
                            1 => nested_recoverable,
                            5 => false,
                            _ => unreachable!(),
                        };
                    assert_eq!(
                        classify_coexistence_recovery(
                            category,
                            cause,
                            nested_recoverable,
                            Some(99),
                            Some("org.bluez.Error.NotConnected"),
                        ),
                        Ok(expected),
                        "category={category} cause={cause_id} nested={nested_recoverable}"
                    );
                }
            }
        }
    }

    #[test]
    fn every_errno_and_missing_errno_across_categories() {
        for category in 0..=16 {
            for errno in (-(1)..=256)
                .map(Some)
                .chain([Some(i32::MIN), Some(i32::MAX), None])
            {
                let expected = RECOVERABLE_CATEGORIES.contains(&category)
                    && errno.is_some_and(|code| TRANSIENT_ERRNOS.contains(&code));
                assert_eq!(
                    classify_coexistence_recovery(
                        category,
                        RecoveryCauseKind::OsError,
                        true,
                        errno,
                        Some("org.bluez.Error.NotReady"),
                    ),
                    Ok(expected),
                    "category={category} errno={errno:?}"
                );
            }
        }
    }

    #[test]
    fn every_transient_dbus_name_and_non_transient_names_across_categories() {
        let names = TRANSIENT_DBUS_NAMES.into_iter().map(Some).chain([
            None,
            Some(""),
            Some("org.bluez.Error.Failed"),
            Some("org.bluez.Error.NotConnected "),
            Some("org.bluez.Error.notConnected"),
            Some("org.freedesktop.DBus.Error.AccessDenied"),
            Some("org.freedesktop.DBus.Error.TimedOut"),
        ]);
        for category in 0..=16 {
            for name in names.clone() {
                let expected = RECOVERABLE_CATEGORIES.contains(&category)
                    && name.is_some_and(|value| TRANSIENT_DBUS_NAMES.contains(&value));
                assert_eq!(
                    classify_coexistence_recovery(
                        category,
                        RecoveryCauseKind::DBusError,
                        true,
                        Some(99),
                        name,
                    ),
                    Ok(expected),
                    "category={category} dbus_error_name={name:?}"
                );
            }
        }
    }

    #[test]
    fn metadata_for_other_cause_kinds_does_not_affect_result() {
        for category in 0..=16 {
            for cause_id in [0, 1, 2, 5] {
                let cause = RecoveryCauseKind::try_from(cause_id).unwrap();
                for nested in [false, true] {
                    let without_metadata =
                        classify_coexistence_recovery(category, cause, nested, None, None);
                    for (errno, name) in [
                        (Some(99), Some("org.bluez.Error.NotReady")),
                        (Some(1), Some("org.bluez.Error.Failed")),
                    ] {
                        assert_eq!(
                            classify_coexistence_recovery(category, cause, nested, errno, name),
                            without_metadata,
                            "category={category} cause={cause_id} nested={nested}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn every_unknown_category_fails_closed_regardless_of_cause() {
        for category in 17..=u8::MAX {
            for cause_id in 0..=5 {
                let cause = RecoveryCauseKind::try_from(cause_id).unwrap();
                for nested in [false, true] {
                    assert_eq!(
                        classify_coexistence_recovery(
                            category,
                            cause,
                            nested,
                            Some(99),
                            Some("org.bluez.Error.NotConnected"),
                        ),
                        Err(RecoveryError::UnknownIdentity),
                        "category={category} cause={cause_id} nested={nested}"
                    );
                }
            }
        }
    }
}
