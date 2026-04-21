# airpods-aap-py

`airpods-aap-py` is the private production PyO3 bridge to `airpods-aap-core`.
It exposes `airpods_hr._airpods_aap_core` inside the `airpods-hr-linux` wheel.
The root `pyproject.toml` owns the single maturin mixed-project build; this
crate is not a separately installed Python distribution.

It delegates parsing to the core and maps native fields/errors across the FFI.
The public `airpods_hr.heartrate` adapter retains the Python dataclass and
exception hierarchy. Users import that public API, never the private bridge.
See the root `README.md` for source and editable installation.

## Private production lifecycle boundary

`production_operation(state: u8, operation: u8) -> u8` validates an operation
and returns the unchanged state. `production_transition(state: u8, event: u8,
cleanup_complete: bool) -> u8` applies a lifecycle event. Both are native
builtins on `airpods_hr._airpods_aap_core`, not public Python APIs. The bridge
owns no resources: the caller still enforces single-use open, concurrent
receive exclusion, and proof of cleanup.

| Identity | Values in numeric order |
| --- | --- |
| State (0–6) | CLOSED, OPENING, READY, STARTING, STREAMING, STOPPING, FAILED |
| Operation (0–4) | OPEN, START, RECEIVE, STOP, CLOSE |
| Event (0–8) | OPEN_BEGIN, OPEN_SUCCEEDED, OPERATION_FAILED, START_BEGIN, START_SUCCEEDED, RECEIVE_ACTIVATION_FAILED, STOP_BEGIN, STOP_SUCCEEDED, CLOSE_FINALIZED |

OPEN is allowed only from CLOSED; START from READY; RECEIVE and STOP from
STREAMING. CLOSE is legal from every state, including CLOSED (a no-op).
OPEN_BEGIN: CLOSED → OPENING; OPEN_SUCCEEDED: OPENING → READY;
OPERATION_FAILED: OPENING/STARTING/STOPPING/FAILED → FAILED; START_BEGIN: READY →
STARTING; START_SUCCEEDED: STARTING → STREAMING; RECEIVE_ACTIVATION_FAILED:
any state → FAILED; STOP_BEGIN: STREAMING → STOPPING; STOP_SUCCEEDED:
STOPPING → READY. The receive path checks STREAMING before awaiting without
the lifecycle lock; a concurrent stop or close may change state before its
activation failure is observed. CLOSE_FINALIZED accepts any non-CLOSED state,
producing CLOSED if `cleanup_complete` is true and FAILED otherwise.

Errors are `ValueError`: an unknown in-range `u8` identity is `ValueError(7)`;
a known but illegal operation/state pair is `ValueError(9)`; an illegal
event/state pair is `ValueError(10)`. Booleans, floats, strings, `None`, and
out-of-range integers used as identities raise
`ValueError("invalid transition identity")`. `cleanup_complete` must be an
actual Python bool (even for other events), otherwise it raises
`ValueError("invalid cleanup_complete flag")`. These categories are private
to the bridge; no public exception contract is introduced.

Run the FFI tests after `cargo build -p airpods-aap-py` with
`.venv/bin/python -m unittest discover -s crates/airpods-aap-py/tests`.
