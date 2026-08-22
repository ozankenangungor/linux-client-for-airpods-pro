# airpods-client-c 0.1.0

Unpublished C ABI 1 over the frozen `airpods-client` v0.1 IPC SDK. Linux local
builds produce `libairpods_client_c.so` and `libairpods_client_c.a`:

```sh
cargo build -p airpods-client-c --locked
cc -std=c11 -I crates/airpods-client-c/include app.c \
  -L target/debug -lairpods_client_c -o app
LD_LIBRARY_PATH=target/debug ./app
```

Include [airpods_client.h](include/airpods_client.h) for supported C symbols.
C++17 consumers use the same header and C symbols. There is no C++ wrapper or
callback interface.

The daemon must already be running. This library uses Unix socket IPC only;
it owns no Bluetooth, BlueZ, systemd, daemon startup, sensor lifecycle, or
reconnect policy. Disconnect remains terminal. Each client owns one worker
thread with a persistent Tokio current-thread runtime with I/O and time enabled.
Calls on the same handle must not overlap, even when moved between native
threads; independent handles may operate concurrently.

Clients, errors, hello snapshots and status snapshots each have their own
matching free function. String views borrow their owner and may contain NUL
bytes; they need not be NUL terminated. Required outputs must be valid writable
slots, and arbitrary invalid non-null pointers are caller undefined behavior.
NULL getters are invalid usage with defensive fallbacks; free(NULL) is a no-op.

Call `airpods_client_close` before `airpods_client_free` for confirmed
unsubscribe. Close joins the worker even on failure and is idempotent. Free
alone provides best-effort disconnect cleanup and joins without awaiting a
daemon unsubscribe response. A finite poll timeout consumes no sample; 0 polls
immediately and `AIRPODS_WAIT_FOREVER` blocks indefinitely. Sample order,
duplicates, BPM 169 and unknown source raw bytes remain exact, with no medical
interpretation. Default connect errors mask the resolved runtime socket path.

The crate has `publish = false`. Libraries and header are repository/local SDK
outputs and are excluded from the five canonical release artifacts. C ABI 1
is separate from the crate version and is not a project-wide 1.0 stability
promise. Platform C bundles are outside the current v0.1 release scope.
