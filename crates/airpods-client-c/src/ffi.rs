//! The only module which dereferences C pointers or transfers Box ownership.
//!
//! Caller contract for every pointer entrypoint: each non-null pointer must be
//! valid, correctly aligned, and live for the call; outputs must be writable and
//! must not alias inputs or each other. Opaque objects must come from this ABI.
//! Serialize operations on each client, including close/free. NULL getters are
//! invalid usage but receive defensive fallbacks. Dangling pointers are caller UB.

use crate::error::Error;
use crate::model::{Hello, HrSample, Status, StringView};
use crate::worker::{Client, Connect, Operation, Reply, Wait};
use std::ffi::{CStr, OsString, c_char};
use std::os::unix::ffi::OsStringExt;
use std::panic::{AssertUnwindSafe, catch_unwind};

pub(crate) const OK: u32 = 0;
pub(crate) const TIMEOUT: u32 = 1;
pub(crate) const END: u32 = 2;
pub(crate) const ERROR: u32 = 3;

pub(crate) fn guarded<T>(fallback: T, work: impl FnOnce() -> T) -> T {
    // Deliberate AssertUnwindSafe: C calls are serialized and all Rust handle
    // state is mutex protected. We never resume the failed operation or expose
    // partially initialized outputs as success. Ownership stays in Rust until
    // an explicit output write. Poisoned locks can still be closed and joined.
    match catch_unwind(AssertUnwindSafe(work)) {
        Ok(value) => value,
        Err(payload) => {
            // A custom panic payload may itself panic in Drop. Do not run it at
            // an ABI boundary. Only this exceptional payload is intentionally leaked.
            std::mem::forget(payload);
            fallback
        }
    }
}

unsafe fn result_boundary(
    out_error: *mut *mut Error,
    work: impl FnOnce() -> Result<u32, Error>,
) -> u32 {
    guarded(ERROR, || {
        if !out_error.is_null() {
            // SAFETY: the caller supplies a writable, non-aliasing error output.
            unsafe { out_error.write(std::ptr::null_mut()) };
        }
        // Construct the detailed fallback inside the outer guard as well.
        match guarded(Err(Error::internal()), work) {
            Ok(result) => result,
            Err(error) => {
                if !out_error.is_null() {
                    // SAFETY: output was validated by the caller contract above;
                    // Box::into_raw transfers the allocation to error_free.
                    unsafe { out_error.write(Box::into_raw(Box::new(error))) };
                }
                ERROR
            }
        }
    })
}

unsafe fn output<T>(pointer: *mut *mut T) -> Result<(), Error> {
    if pointer.is_null() {
        return Err(Error::invalid_argument());
    }
    // SAFETY: the caller guarantees a live, aligned, writable output slot.
    unsafe { pointer.write(std::ptr::null_mut()) };
    Ok(())
}

unsafe fn required<'a, T>(pointer: *const T) -> Result<&'a T, Error> {
    // SAFETY: non-null input is live and aligned for the call. No reference
    // escapes the call; caller serialization prevents concurrent client free.
    unsafe { pointer.as_ref() }.ok_or_else(Error::invalid_argument)
}

unsafe fn view<T, R: Copy>(pointer: *const T, fallback: R, read: impl FnOnce(&T) -> R) -> R {
    guarded(fallback, || {
        // SAFETY: non-null input is a live ABI object. NULL is handled without
        // dereference; borrowed views remain tied to the owner's allocation.
        unsafe { pointer.as_ref() }.map_or(fallback, read)
    })
}

unsafe fn release<T>(pointer: *mut T) {
    guarded((), || {
        if !pointer.is_null() {
            // SAFETY: ownership was returned by this ABI, has not been freed,
            // and no operation overlaps destruction. Reconstitute exactly once.
            drop(unsafe { Box::from_raw(pointer) });
        }
    });
}

#[unsafe(no_mangle)]
pub extern "C" fn airpods_client_c_abi_version() -> u32 {
    guarded(1, || 1)
}

#[unsafe(no_mangle)]
pub extern "C" fn airpods_client_protocol_version() -> u64 {
    guarded(airpods_client::PROTOCOL_VERSION, || {
        airpods_client::PROTOCOL_VERSION
    })
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_connect(
    out_client: *mut *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: outputs are caller-owned writable slots; the handle is transferred once.
    unsafe {
        result_boundary(out_error, || {
            output(out_client)?;
            let client = Client::connect(Connect::Default)?;
            out_client.write(Box::into_raw(Box::new(client)));
            Ok(OK)
        })
    }
}

/// # Safety
/// Pointer arguments satisfy the caller contract; path is NUL terminated.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_connect_to(
    socket_path: *const c_char,
    out_client: *mut *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: path is readable through its NUL and outputs are valid writable slots.
    unsafe {
        result_boundary(out_error, || {
            output(out_client)?;
            if socket_path.is_null() {
                return Err(Error::invalid_argument());
            }
            let path = OsString::from_vec(CStr::from_ptr(socket_path).to_bytes().to_vec());
            let client = Client::connect(Connect::Explicit(path.into()))?;
            out_client.write(Box::into_raw(Box::new(client)));
            Ok(OK)
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_ping(
    client: *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client remains alive and its operations are serialized; error slot is valid.
    unsafe {
        result_boundary(out_error, || {
            required(client)?.request(Operation::Ping)?;
            Ok(OK)
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_hello(
    client: *mut Client,
    out_hello: *mut *mut Hello,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client is live; outputs are writable and do not alias client or each other.
    unsafe {
        result_boundary(out_error, || {
            output(out_hello)?;
            match required(client)?.request(Operation::Hello)? {
                Reply::Hello(hello) => {
                    out_hello.write(Box::into_raw(Box::new(hello)));
                    Ok(OK)
                }
                _ => Err(Error::internal()),
            }
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_status(
    client: *mut Client,
    out_status: *mut *mut Status,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client is live; outputs are writable and do not alias client or each other.
    unsafe {
        result_boundary(out_error, || {
            output(out_status)?;
            match required(client)?.request(Operation::Status)? {
                Reply::Status(status) => {
                    out_status.write(Box::into_raw(Box::new(status)));
                    Ok(OK)
                }
                _ => Err(Error::internal()),
            }
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_hr_subscribe(
    client: *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client remains alive with serialized calls; optional error slot is writable.
    unsafe {
        result_boundary(out_error, || {
            required(client)?.request(Operation::Subscribe)?;
            Ok(OK)
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_hr_unsubscribe(
    client: *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client remains alive with serialized calls; optional error slot is writable.
    unsafe {
        result_boundary(out_error, || {
            required(client)?.request(Operation::Unsubscribe)?;
            Ok(OK)
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_hr_next(
    client: *mut Client,
    timeout_ms: u32,
    out_sample: *mut HrSample,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: sample/error outputs are writable and disjoint; client is live and serialized.
    unsafe {
        result_boundary(out_error, || {
            if out_sample.is_null() {
                return Err(Error::invalid_argument());
            }
            out_sample.write(HrSample::default());
            match required(client)?.request(Operation::Next(Wait::milliseconds(timeout_ms)))? {
                Reply::Sample(sample) => {
                    out_sample.write(sample);
                    Ok(OK)
                }
                Reply::Timeout => Ok(TIMEOUT),
                Reply::End => Ok(END),
                _ => Err(Error::internal()),
            }
        })
    }
}

/// # Safety
/// Pointer arguments must satisfy this module's caller contract.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_close(
    client: *mut Client,
    out_error: *mut *mut Error,
) -> u32 {
    // SAFETY: client stays allocated through its serialized close; error output is writable.
    unsafe {
        result_boundary(out_error, || {
            required(client)?.close()?;
            Ok(OK)
        })
    }
}

/// # Safety
/// The live owned handle is freed once, without overlapping operations.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_client_free(client: *mut Client) {
    // SAFETY: caller transfers its unique handle ownership; NULL is a no-op.
    unsafe { release(client) }
}

/// # Safety
/// Non-null error is a live object returned by this ABI.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_error_kind(error: *const Error) -> u32 {
    // SAFETY: getter contract; NULL defensively returns INTERNAL.
    unsafe { view(error, 255, |v| v.kind) }
}

/// # Safety
/// Non-null error is live; returned view is borrowed until error_free.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_error_message(error: *const Error) -> StringView {
    // SAFETY: getter contract; object keeps the message allocation alive.
    unsafe {
        view(error, StringView::ABSENT, |v| {
            StringView::borrowed(&v.message)
        })
    }
}

/// # Safety
/// Non-null error is live; returned view is borrowed until error_free.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_error_daemon_code(error: *const Error) -> StringView {
    // SAFETY: getter contract; object keeps the optional code allocation alive.
    unsafe {
        view(error, StringView::ABSENT, |v| {
            v.daemon_code
                .as_deref()
                .map_or(StringView::ABSENT, StringView::borrowed)
        })
    }
}

/// # Safety
/// Transfer a live error allocation exactly once; NULL is allowed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_error_free(error: *mut Error) {
    // SAFETY: caller owns this ABI allocation and relinquishes it exactly once.
    unsafe { release(error) }
}

/// # Safety
/// Non-null hello is live; returned view is borrowed until hello_free.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_hello_service(hello: *const Hello) -> StringView {
    // SAFETY: getter contract; hello owns its service string.
    unsafe {
        view(hello, StringView::ABSENT, |v| {
            StringView::borrowed(&v.service)
        })
    }
}

/// # Safety
/// Non-null hello is a live object returned by this ABI.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_hello_experimental(hello: *const Hello) -> u32 {
    // SAFETY: getter contract; NULL defensively returns 0.
    unsafe { view(hello, 0, |v| v.experimental) }
}

/// # Safety
/// Transfer a live hello allocation exactly once; NULL is allowed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_hello_free(hello: *mut Hello) {
    // SAFETY: caller owns this ABI allocation and relinquishes it exactly once.
    unsafe { release(hello) }
}

/// # Safety
/// Non-null status is a live object returned by this ABI.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_status_state(status: *const Status) -> u32 {
    // SAFETY: getter contract; NULL defensively returns UNKNOWN.
    unsafe { view(status, 255, |v| v.state) }
}

/// # Safety
/// Non-null status is a live object returned by this ABI.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_status_subscriber_count(status: *const Status) -> u64 {
    // SAFETY: getter contract; NULL defensively returns 0.
    unsafe { view(status, 0, |v| v.subscriber_count) }
}

/// # Safety
/// Non-null status is live; returned view is borrowed until status_free.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_status_unknown_state(status: *const Status) -> StringView {
    // SAFETY: getter contract; status owns the exact optional unknown string.
    unsafe {
        view(status, StringView::ABSENT, |v| {
            v.unknown_state
                .as_deref()
                .map_or(StringView::ABSENT, StringView::borrowed)
        })
    }
}

/// # Safety
/// Transfer a live status allocation exactly once; NULL is allowed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn airpods_status_free(status: *mut Status) {
    // SAFETY: caller owns this ABI allocation and relinquishes it exactly once.
    unsafe { release(status) }
}

#[cfg(test)]
pub(crate) fn panic_result_for_test() -> (u32, Box<Error>) {
    let mut error = std::ptr::null_mut();
    // SAFETY: local writable output; boundary returns an owned allocation on ERROR.
    let result = unsafe { result_boundary(&mut error, || panic!("test panic")) };
    // SAFETY: the caught panic populated error with a fresh Box allocation.
    (result, unsafe { Box::from_raw(error) })
}
