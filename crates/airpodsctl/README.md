# airpodsctl 0.1.0

`airpodsctl` is an experimental, unpublished Rust command-line client for a
running `airpods-hubd`. It uses the public Rust `airpods-client` SDK and owns no
Bluetooth connection. It does not start, stop, restart, or reconnect the daemon.

Build it from this repository with `cargo build --locked -p airpodsctl`, then
run `target/debug/airpodsctl`. The daemon must already be running. By default,
the CLI connects to exactly `$XDG_RUNTIME_DIR/airpods-hubd.sock`. Use
`--socket PATH` to connect to another Unix socket; no discovery is performed.

```console
airpodsctl hello
airpodsctl ping
airpodsctl status
airpodsctl status --json
airpodsctl watch
airpodsctl watch --count 10 --json
airpodsctl --socket /path/to/hubd.sock watch
```

`hello` reports the daemon service and experimental flag. `ping` checks the
running daemon. `status` reports its state and subscriber count. `watch` prints
each heart-rate sample in daemon order, including duplicates and unknown source
bytes. `--json` emits one compact JSON object per result or sample on stdout.
`--count N` accepts a positive number of samples, then waits for confirmed
unsubscribe before exiting. Without a count, `watch` continues until Ctrl-C,
stream termination, or an error.

Normal success exits 0. After an active subscription, Ctrl-C requests
unsubscribe and waits for the daemon response before exiting 130. Failed
cleanup and other runtime errors print to stderr and exit 1. Invalid command
usage follows clap's nonzero usage exit behavior. The CLI never retries a
terminal daemon disconnect.

This v0.1 output is experimental. BPM values have not been medically validated.
This software is not a medical device and must not be used for diagnosis,
treatment, or safety-critical monitoring.
