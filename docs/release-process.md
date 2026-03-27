# Release engineering

Current coordinated release candidate: `0.1.0`

Release validation defines one local, hardware-independent release gate for the
unpublished production package and both v0.1 client SDKs. It builds artifacts
and validates consumers; it never publishes, reads credentials, opens
Bluetooth, or starts the production daemon.

## Canonical validation

Run from a clean checkout after installing the development project and the
private parity binding as described in `docs/rust-core-architecture.md`:

```console
.venv/bin/python tools/validate_release.py \
  --output-dir ../airpods-hr-linux-release-0.1.0
```

The default `all` scope checks version and source policy, runs the complete
Python suite, compiles Python sources, runs static systemd unit parsing, runs
the Rust format/check/test/clippy gates, exercises Rust and Python clients
against the fake-backed Python daemon, builds all packages twice, audits their
contents, and runs clean consumer checks. Commands have finite timeouts and
use direct argument arrays.

The output directory must be outside the repository and absent or empty. It
contains:

- `airpods_hr_linux-0.1.0-py3-none-any.whl`
- `airpods_hr_linux-0.1.0.tar.gz`
- `airpods_client-0.1.0-py3-none-any.whl`
- `airpods_client-0.1.0.tar.gz`
- `airpods-client-0.1.0.crate`
- `release-manifest.json`
- `release-summary.txt`

Generated archives, temporary environments, and Cargo targets are not tracked.
The JSON manifest schema is version 1. It records the coordinated release
version, exact Git commit and clean-tree state, commit-derived
`SOURCE_DATE_EPOCH`, Python/Rust/Cargo/platform provenance, and each artifact's
kind, package identity, filename, version, byte size, and SHA-256. Its
`validation` object states that no Bluetooth hardware was used, the production
daemon was not started, and nothing was published.

Two ordinary wheel or sdist builds can differ byte-for-byte because ZIP and tar
metadata include timestamps. The canonical builder sets `SOURCE_DATE_EPOCH` to
the commit timestamp and repeats all five builds on the same host with the same
toolchain. The manifest reports the comparison. A match is a useful local
determinism check; it is not a reproducible-build guarantee across hosts,
operating systems, or toolchain versions.

The Release validation investigation found that the two wheels and the Cargo crate
match across same-host repeated builds with the commit-derived timestamp. The
two setuptools sdists still differ because their tar/gzip construction retains
build-time archive metadata. The manifest reports that result per artifact;
the release process does not rewrite backend output or claim full
reproducibility.

## Continuous integration

GitHub Actions divides the same project-owned checks into static policy,
production Python, standalone Python compatibility, Rust, cross-language, and
release-artifact jobs. CI uses fake sensor sessions and temporary Unix sockets.
Installer tests isolate `HOME`, `XDG_CONFIG_HOME`, and `XDG_RUNTIME_DIR` and
inject fake `systemctl` behavior. `systemd-analyze verify --user` performs only
static unit parsing. If the runner has no `systemd-analyze`, the validator and
unit tests print an explicit skip reason.

CI cannot validate BlueZ reachability, adapter state, a descriptor handshake,
real heart-rate delivery, systemd's runtime user-manager behavior, or A2DP
continuity. The accepted SDK packaging installed-distribution run remains the
current hardware evidence. Every future release still needs a separately
authorized manual hardware gate.

## Python version policy

The production `airpods-hr-linux` package remains Python 3.14 or newer. Its
complete production and dependency validation is established on Python 3.14;
no lower version is claimed. In particular, the current daemon uses the
`cleanup_socket` Unix-server behavior introduced after Python 3.11.

The standalone, standard-library-only Python `airpods-client` supports Python
3.11 through 3.14. On each version, CI installs the distribution in isolation
and runs the standalone protocol/lifecycle suite plus the frozen v0.1 public
contract. Cross-language tests remain on Python 3.14 because they instantiate
the production Python daemon. These policies are independent by design.

## Package and consumer gates

The production wheel must contain `airpods_hr`, the service installer, all
three console entrypoints, and the two declared runtime dependencies. It must
exclude `airpods_client`, tests, captures, logs, and credential-like material.
Its clean environment imports only installed code, resolves each entrypoint,
runs `pip check` and `compileall`, and confirms that the service dry run names
the installed interpreter while writing no user configuration.

The Python SDK wheel must contain only `airpods_client` and distribution
metadata, declare zero runtime dependencies, and exclude daemon and Bluetooth
code. Its clean environment checks the frozen public import surface and typed
missing-XDG and missing-daemon failures. The Rust crate excludes the
repository-only integration probe, `target`, and Python/daemon material. A
temporary external Cargo consumer compiles against the packaged v0.1 API.

## Release checklist

1. Confirm the intended coordinated version in both Python projects, the Rust
   client crate, and this document.
2. Start from a clean reviewed commit and run the canonical validation command.
3. Review `release-summary.txt` and parse `release-manifest.json`; confirm five
   unique artifacts, package identities, versions, hashes, and nonzero sizes.
4. Confirm the package-content, clean-install, SDK-contract, frozen-component,
   sensitive-data, `pip check`, `compileall`, systemd parser, Cargo, and
   cross-language gates passed.
5. Perform the separately authorized hardware release gate and record its
   evidence without addresses, keys, captures, or private paths.
6. Treat publication as a later, explicit operation. Release validation needs no
   registry credentials and does not establish that any package was published.
