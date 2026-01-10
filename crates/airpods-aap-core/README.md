# airpods-aap-core

`airpods-aap-core` contains platform-independent, hardware-independent AAP
protocol types and parsers. It performs no I/O and has no dependency on BlueZ,
D-Bus, Bluetooth sockets, PipeWire, or an async runtime.

The Python implementation remains authoritative for production behavior. This
crate is a parity foundation and is not connected to the Python runtime.
