#![deny(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]
//! Unpublished C ABI 1 over the frozen low-level IPC client.

mod error;
mod ffi;
mod model;

#[cfg(test)]
mod tests;
