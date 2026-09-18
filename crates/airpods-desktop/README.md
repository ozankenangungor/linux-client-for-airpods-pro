# Linux Client for AirPods Pro — desktop app

A native Rust desktop app over `airpods-client-resilient`. The daemon owns
Bluetooth; the app prepares its local user service and displays the stream.

```console
cargo run --locked -p airpods-desktop
cargo run --locked -p airpods-desktop -- --demo
```

Pair AirPods normally in Linux Bluetooth settings, then run the app. It installs
and starts the daemon when needed. First setup needs Python 3.14, a systemd user
session, and network access for uncached Python dependencies.

`--demo` runs the same dashboard with a repeatable connection/recovery script.
It needs no AirPods or daemon and performs no setup. `--socket PATH` selects an
externally managed daemon and bypasses automatic setup.

This crate is unpublished. Source builds use the managed daemon environment;
the AppImage uses its bundled runtime. There is no public download yet.
