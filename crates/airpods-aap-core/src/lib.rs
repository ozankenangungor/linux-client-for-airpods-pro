#![forbid(unsafe_code)]
//! Platform-independent types and parsers for proven AAP wire structures.
//!
//! This crate owns production heart-rate parsing and pure AAP frame analysis.
//! It performs no operating-system I/O. Python retains the public models,
//! errors, and Linux integration.

mod activation;
mod analysis;
mod control;
mod heart_rate;
pub mod pre_auth_diagnostics;
mod production;
mod recovery;
pub mod semantics;

pub use activation::{
    ActivationCommand, ActivationEvent, ActivationState, SentCommands, TransitionError, advance,
    plan_activation_send, plan_cleanup_send,
};
pub use analysis::{
    AapFrameSummary, AapType2bFrameSummary, DescriptorEvidence, RecordSuffixSummary,
};
pub use control::{
    ControlFrameSummary, is_connect4_ack, is_observed_service_ack, is_service_ack_candidate_shape,
};

pub use heart_rate::{
    HEART_RATE_MARKER, HEART_RATE_REPORT_ID, HEART_RATE_REPORT_SIZE, HeartRateParseError,
    HeartRateReport, SourceSide, parse_heart_rate_packet,
};
pub use production::{
    ProductionError, ProductionEvent, ProductionOperation, ProductionState, production_operation,
    production_transition,
};
pub use recovery::{RecoveryCauseKind, RecoveryError, classify_coexistence_recovery};
