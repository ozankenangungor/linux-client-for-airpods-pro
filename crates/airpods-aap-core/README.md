# airpods-aap-core

`airpods-aap-core` contains platform-independent, hardware-independent AAP
protocol types and parsers. It performs no I/O and has no dependency on BlueZ,
D-Bus, Bluetooth sockets, PipeWire, or an async runtime.

This crate is the authoritative production heart-rate parser. The separate
`airpods-aap-py` bridge exposes it to the public Python adapter without moving
Linux transport, session, or recovery behavior into this core.
