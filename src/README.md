# Source layout

This directory contains the production Python implementation for
`airpods-hr-linux`.

- [`airpods_hr/`](airpods_hr/) is the importable Python package.
- The distribution name on PyPI is `airpods-hr-linux`.
- The project intentionally uses Python's `src` layout to help tests and
  tooling import the installed distribution instead of accidentally importing
  an unchecked package from the repository root.
- Standalone client SDKs live outside this directory:
  - Python client: [`packages/airpods-client-python/`](../packages/airpods-client-python/)
  - Rust client: [`crates/airpods-client/`](../crates/airpods-client/)
- Rust protocol and parity components also live under [`crates/`](../crates/).
