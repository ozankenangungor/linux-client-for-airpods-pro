use crate :: error :: Error ;
use crate :: ffi :: * ;
use crate :: model :: { Hello , HrSample , Status , StringView } ;
use crate :: worker :: { Connect } ;
use airpods_client :: { DaemonState , HeartRateSample , SourceSide } ;

use std :: io :: { BufRead , Write } ;
use std :: mem :: { align_of , offset_of , size_of } ;






#[test]
fn pod_layout() {
    assert_eq!(size_of::<StringView>(), 2 * size_of::<usize>());
    assert_eq!(align_of::<StringView>(), align_of::<usize>());
    assert_eq!(offset_of!(StringView, data), 0);
    assert_eq!(offset_of!(StringView, len), size_of::<usize>());
    assert_eq!(size_of::<HrSample>(), 16);
    assert_eq!(align_of::<HrSample>(), align_of::<u32>());
    assert_eq!(offset_of!(HrSample, bpm), 0);
    assert_eq!(offset_of!(HrSample, source_side), 4);
    assert_eq!(offset_of!(HrSample, source_side_raw), 8);
    assert_eq!(offset_of!(HrSample, reserved), 12);
}

#[test]
fn versions_delegate() {
    assert_eq!(airpods_client_c_abi_version(), 1);
    assert_eq!(
        airpods_client_protocol_version(),
        airpods_client::PROTOCOL_VERSION
    );
}

#[test]
fn all_sdk_error_kinds_and_daemon_codes() {
    use airpods_client::Error as E;
    let variants = [
        (E::XdgRuntimeDirMissing, 10),
        (
            E::Connect {
                path: "/explicit".into(),
                kind: std::io::ErrorKind::NotFound,
                message: "missing".into(),
            },
            11,
        ),
        (
            E::Io {
                context: "reading",
                kind: std::io::ErrorKind::BrokenPipe,
                message: "closed".into(),
            },
            12,
        ),
        (E::FrameTooLarge { limit: 4096 }, 13),
        (
            E::InvalidJson {
                message: "invalid".into(),
            },
            14,
        ),
        (
            E::ProtocolVersion {
                expected: 1,
                received: Some(2),
            },
            15,
        ),
        (
            E::UnexpectedMessage {
                message: "unexpected".into(),
            },
            16,
        ),
        (
            E::DaemonError {
                code: "future\0code".into(),
                message: "safe\0message".into(),
            },
            17,
        ),
        (E::ConnectionClosed, 18),
        (E::SubscriptionActive, 19),
        (E::EventLagged { skipped: 37 }, 20),
    ];
    for (sdk, expected) in variants {
        let error = Box::into_raw(Box::new(Error::sdk(sdk, false)));
        // SAFETY: error is a fresh ABI-compatible allocation; getters borrow
        // only until free, which consumes it exactly once.
        unsafe {
            assert_eq!(airpods_error_kind(error), expected);
            let code = airpods_error_daemon_code(error);
            if expected == 17 {
                assert_eq!(
                    std::slice::from_raw_parts(code.data.cast::<u8>(), code.len),
                    b"future\0code"
                );
                let message = airpods_error_message(error);
                assert!(
                    std::slice::from_raw_parts(message.data.cast::<u8>(), message.len).contains(&0)
                );
            } else {
                assert!(code.data.is_null());
                assert_eq!(code.len, 0);
            }
            airpods_error_free(error);
        }
    }
}

#[test]
fn default_path_masked_explicit_path_retained() {
    let make = || airpods_client::Error::Connect {
        path: "/private-looking-task11-4/runtime/airpods-hubd.sock".into(),
        kind: std::io::ErrorKind::NotFound,
        message: "No such file or directory (os error 2)".into(),
    };
    let default = Error::sdk(make(), true);
    assert_eq!(default.kind, 11);
    assert_eq!(
        default.message,
        "could not connect to default airpods-hubd socket: No such file or directory (os error 2)"
    );
    assert!(!default.message.contains("private-looking"));
    assert!(
        Error::sdk(make(), false)
            .message
            .contains("/private-looking-task11-4/runtime/airpods-hubd.sock")
    );
}

#[test]
fn hello_conversion_preserves_bytes_and_boolean() {
    for flag in [false, true] {
        let hello = Hello::from(airpods_client::Hello {
            service: "service\0名".into(),
            experimental: flag,
        });
        assert_eq!(hello.service, "service\0名");
        assert_eq!(hello.experimental, u32::from(flag));
    }
}

#[test]
fn status_conversion_all_states() {
    let known = [
        DaemonState::Stopped,
        DaemonState::Starting,
        DaemonState::Ready,
        DaemonState::StartingHeartRate,
        DaemonState::Streaming,
        DaemonState::StoppingHeartRate,
        DaemonState::Failed,
        DaemonState::ShuttingDown,
    ];
    for (code, state) in known.into_iter().enumerate() {
        let status = Status::from(airpods_client::Status {
            state,
            subscriber_count: u64::MAX,
        });
        assert_eq!(status.state, code as u32);
        assert_eq!(status.subscriber_count, u64::MAX);
        assert!(status.unknown_state.is_none());
    }
    let unknown = Status::from(airpods_client::Status {
        state: DaemonState::Unknown("future_state\0名".into()),
        subscriber_count: 169,
    });
    assert_eq!(unknown.state, 255);
    assert_eq!(unknown.unknown_state.as_deref(), Some("future_state\0名"));
    assert_eq!(unknown.subscriber_count, 169);
}

#[test]
fn sample_conversion_exact() {
    for (bpm, side, expected, raw) in [
        (169, SourceSide::Left, 1, 0),
        (88, SourceSide::Right, 2, 0),
        (74, SourceSide::Unknown(37), 0, 37),
    ] {
        assert_eq!(
            HrSample::from(HeartRateSample {
                bpm,
                source_side: side
            }),
            HrSample {
                bpm: u32::from(bpm),
                source_side: expected,
                source_side_raw: raw,
                reserved: 0
            }
        );
    }
}



#[test]
fn panic_boundary_returns_internal() {
    let (result, error) = panic_result_for_test();
    assert_eq!(result, ERROR);
    assert_eq!(error.kind, 255);
    assert_eq!(error.message, "internal C client failure");
    assert!(error.daemon_code.is_none());
    assert_eq!(guarded(37, || panic!("getter test")), 37);
    guarded((), || panic!("destructor test"));
}

#[test]
fn panic_payload_destructor_cannot_escape() {
    struct BadDrop;
    impl Drop for BadDrop {
        fn drop(&mut self) {
            panic!("payload Drop must not run");
        }
    }
    assert_eq!(guarded(255, || std::panic::panic_any(BadDrop)), 255);
}






























