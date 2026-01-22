# airpods-aap-py

`airpods-aap-py` is a private development binding for real Python/Rust parity
testing. It exposes the top-level private module `_airpods_aap_core` and
depends on `airpods-aap-core` plus PyO3.

It contains no transport code and is not part of the public `airpods_hr` API.
The root setuptools project does not depend on this crate or its maturin
configuration.
