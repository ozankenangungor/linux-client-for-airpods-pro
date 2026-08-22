#ifndef AIRPODS_CLIENT_H
#define AIRPODS_CLIENT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AIRPODS_CLIENT_C_ABI_VERSION 1u
typedef uint32_t airpods_result_t;
#define AIRPODS_RESULT_OK       0u
#define AIRPODS_RESULT_TIMEOUT  1u
#define AIRPODS_RESULT_END      2u
#define AIRPODS_RESULT_ERROR    3u

typedef uint32_t airpods_error_kind_t;
#define AIRPODS_ERROR_INVALID_ARGUMENT          1u
#define AIRPODS_ERROR_INVALID_STATE             2u
#define AIRPODS_ERROR_XDG_RUNTIME_DIR_MISSING   10u
#define AIRPODS_ERROR_CONNECT                  11u
#define AIRPODS_ERROR_IO                       12u
#define AIRPODS_ERROR_FRAME_TOO_LARGE          13u
#define AIRPODS_ERROR_INVALID_JSON             14u
#define AIRPODS_ERROR_PROTOCOL_VERSION         15u
#define AIRPODS_ERROR_UNEXPECTED_MESSAGE       16u
#define AIRPODS_ERROR_DAEMON                   17u
#define AIRPODS_ERROR_CONNECTION_CLOSED        18u
#define AIRPODS_ERROR_SUBSCRIPTION_ACTIVE      19u
#define AIRPODS_ERROR_EVENT_LAGGED             20u
#define AIRPODS_ERROR_INTERNAL                255u

typedef uint32_t airpods_daemon_state_t;
#define AIRPODS_DAEMON_STOPPED               0u
#define AIRPODS_DAEMON_STARTING              1u
#define AIRPODS_DAEMON_READY                 2u
#define AIRPODS_DAEMON_STARTING_HEART_RATE    3u
#define AIRPODS_DAEMON_STREAMING             4u
#define AIRPODS_DAEMON_STOPPING_HEART_RATE    5u
#define AIRPODS_DAEMON_FAILED                6u
#define AIRPODS_DAEMON_SHUTTING_DOWN         7u
#define AIRPODS_DAEMON_UNKNOWN             255u

typedef uint32_t airpods_source_side_t;
#define AIRPODS_SOURCE_UNKNOWN  0u
#define AIRPODS_SOURCE_LEFT     1u
#define AIRPODS_SOURCE_RIGHT    2u
#define AIRPODS_WAIT_FOREVER UINT32_MAX

typedef struct airpods_client airpods_client_t;
typedef struct airpods_error airpods_error_t;
typedef struct airpods_hello airpods_hello_t;
typedef struct airpods_status airpods_status_t;

/* Borrowed bytes, NOT necessarily NUL terminated; embedded NUL is permitted.
 * Protocol/generated strings are UTF-8. Valid until the owning object is freed.
 * Absent optional strings have data == NULL and len == 0. Never free a view. */
typedef struct {
    const char *data;
    size_t len;
} airpods_string_view_t;

/* Exact daemon sample, with no filtering or medical interpretation.
 * Known source sides have source_side_raw == 0; unknown preserves the raw byte.
 * reserved is always 0. */
typedef struct {
    uint32_t bpm;
    uint32_t source_side;
    uint32_t source_side_raw;
    uint32_t reserved;
} airpods_hr_sample_t;

/* ABI 1 is separate from crate version 0.1.0 and protocol version. This is an
 * unpublished local SDK, not a project-wide 1.0 stability promise.
 *
 * Each handle owns one worker thread. Handles may move between native threads,
 * but ALL calls on one handle must be serialized, including close/free.
 * Independent handles may run concurrently. No callbacks or reconnect exist.
 * The daemon must already be running; this SDK owns only Unix socket IPC.
 *
 * All non-null pointers must be valid, aligned and live for the call. Outputs
 * must be writable and must not alias inputs or each other. Arbitrary invalid
 * non-null pointers cannot be validated and remain caller undefined behavior.
 * Opaque objects must originate from this ABI and be freed exactly once.
 *
 * out_error is optional. When provided, its slot is cleared before work and
 * remains NULL on OK/TIMEOUT/END; ERROR supplies an owned error when possible.
 * Required object outputs are cleared before work. These slots must not hold
 * allocations needing destruction: clearing a slot does not free its old value.
 * NULL inputs to getters are invalid usage; defensive fallback values are
 * provided (INTERNAL/UNKNOWN/zero/absent). All free(NULL) calls are no-ops. */

uint32_t airpods_client_c_abi_version(void);
uint64_t airpods_client_protocol_version(void);

/* Default: exactly $XDG_RUNTIME_DIR/airpods-hubd.sock, no fallback or startup.
 * Default connect errors mask the resolved path. Explicit path is NUL terminated
 * Unix bytes (need not be UTF-8), and may appear in connect error text.
 * Connect blocks and returns a handle only on success. */
airpods_result_t airpods_client_connect(
    airpods_client_t **out_client, airpods_error_t **out_error);
airpods_result_t airpods_client_connect_to(
    const char *socket_path, airpods_client_t **out_client,
    airpods_error_t **out_error);

airpods_result_t airpods_client_ping(
    airpods_client_t *client, airpods_error_t **out_error);
airpods_result_t airpods_client_hello(
    airpods_client_t *client, airpods_hello_t **out_hello,
    airpods_error_t **out_error);
airpods_result_t airpods_client_status(
    airpods_client_t *client, airpods_status_t **out_status,
    airpods_error_t **out_error);

/* One subscription per client. Double subscribe is SUBSCRIPTION_ACTIVE;
 * next/unsubscribe without a subscription is INVALID_STATE.
 * Unsubscribe awaits the real daemon acknowledgement. */
airpods_result_t airpods_client_hr_subscribe(
    airpods_client_t *client, airpods_error_t **out_error);
airpods_result_t airpods_client_hr_unsubscribe(
    airpods_client_t *client, airpods_error_t **out_error);

/* Required out_sample is zeroed before work. OK writes exactly one sample.
 * timeout_ms: 0 = poll; 1..UINT32_MAX-1 = finite milliseconds;
 * AIRPODS_WAIT_FOREVER = indefinite blocking wait.
 * A ready sample wins over a simultaneously ready deadline. TIMEOUT is normal
 * and consumes no sample. END means stream completion. ERROR carries typed
 * failure, including bounded-consumer EVENT_LAGGED. No background consumer or
 * extra sample queue exists. WAIT_FOREVER also blocks that handle's caller. */
airpods_result_t airpods_client_hr_next(
    airpods_client_t *client, uint32_t timeout_ms,
    airpods_hr_sample_t *out_sample, airpods_error_t **out_error);

/* Close confirms any active unsubscribe, then terminates and joins the worker,
 * even if unsubscribe failed. Idempotent. Ordinary calls after close report
 * INVALID_STATE. The allocation remains valid until free.
 * Free alone requests immediate best-effort shutdown, drops the connection,
 * and joins the worker without awaiting daemon unsubscribe. Applications which
 * require confirmed unsubscribe must close before free. */
airpods_result_t airpods_client_close(
    airpods_client_t *client, airpods_error_t **out_error);
void airpods_client_free(airpods_client_t *client);

airpods_error_kind_t airpods_error_kind(const airpods_error_t *error);
airpods_string_view_t airpods_error_message(const airpods_error_t *error);
/* Present only for DAEMON errors, preserving the daemon code exactly. */
airpods_string_view_t airpods_error_daemon_code(const airpods_error_t *error);
void airpods_error_free(airpods_error_t *error);

airpods_string_view_t airpods_hello_service(const airpods_hello_t *hello);
/* Always 0 or 1. */
uint32_t airpods_hello_experimental(const airpods_hello_t *hello);
void airpods_hello_free(airpods_hello_t *hello);

airpods_daemon_state_t airpods_status_state(const airpods_status_t *status);
uint64_t airpods_status_subscriber_count(const airpods_status_t *status);
/* UNKNOWN preserves its exact raw string; known states return absent. */
airpods_string_view_t airpods_status_unknown_state(const airpods_status_t *status);
void airpods_status_free(airpods_status_t *status);

#ifdef __cplusplus
}
#endif
#endif
