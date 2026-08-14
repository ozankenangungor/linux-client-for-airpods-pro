# airpodsctl 0.1.0

`airpodsctl` is an experimental, unpublished Rust command-line client for a
running `airpods-hubd`. It uses the public Rust `airpods-client` SDK and owns no
Bluetooth connection. It does not start, stop, or restart the daemon.

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
airpodsctl watch --reconnect
airpodsctl top
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
usage follows clap's nonzero usage exit behavior. Ordinary `watch` never retries a terminal daemon disconnect. `watch --reconnect`
opts into finite Unix reconnect (1, 2, 5, 10, 10 seconds). Human lifecycle
notices go to stderr; JSON lifecycle objects go to stdout. `--count` counts
only samples. Ctrl-C during backoff stops before another connection attempt.

`top` is an interactive dashboard using the existing resilient client. It
requires terminal stdin and stdout, and rejects `--json` before opening the
terminal or connecting. It does not start or restart the daemon. Its current
reading, connection status, sparkline, and recent rows show unmodified samples,
including duplicates and unknown source bytes. It retains at most 120 samples
for display and keeps a separate total count. It provides no medical
interpretation. Terminals smaller than 50 columns or 18 rows show compact help.

Press `q` or Esc for a clean exit (0), or Ctrl-C for an interrupt exit (130).
SIGTERM exits 143. On these and other catchable ordinary exits, the CLI
restores raw mode and the alternate screen before confirmed stream cleanup and
before printing an error. Cleanup failures exit 1. SIGKILL, kill -9, and
machine failure cannot be intercepted, so application-level restoration is
impossible in those cases. `top` is part of this unpublished CLI, not a
separate release artifact.

This v0.1 output is experimental. BPM values have not been medically validated.
This software is not a medical device and must not be used for diagnosis,
treatment, or safety-critical monitoring.
