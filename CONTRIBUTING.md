# Contributing

The production package requires Linux and Python 3.14 or newer. The standalone
Python client supports Python 3.11 through 3.14. Rust work uses the stable
toolchain and the checked-in `Cargo.lock`.

Create an isolated development environment and install the project plus the
pinned private-binding builder:

```console
python3.14 -m venv .venv
.venv/bin/python -m pip install -e . maturin==1.15.0
mkdir -p /tmp/airpods-aap-py-wheel
.venv/bin/maturin build --release --locked \
  --manifest-path crates/airpods-aap-py/Cargo.toml \
  --interpreter .venv/bin/python \
  --out /tmp/airpods-aap-py-wheel
.venv/bin/python -m pip install --no-deps /tmp/airpods-aap-py-wheel/*.whl
```

Run the hardware-independent checks before submitting a change:

```console
.venv/bin/python -m unittest discover -s tests -v
cargo fmt --check
cargo check --workspace --locked
cargo test --workspace --locked
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
```

The canonical release gate requires a clean commit and an empty output path
outside the repository:

```console
.venv/bin/python tools/validate_release.py \
  --scope all \
  --output-dir ../airpods-hr-linux-release-0.1.0
```

Hardware tests require explicit authorization from the hardware owner and are
never part of routine CI. Do not commit packet captures, logs, Bluetooth
LinkKeys, pairing files, credentials, generated packages, or private hardware
identifiers. Protocol claims should cite controlled evidence and preserve
unknown fields without invented meanings. BPM observations must not be
presented as medical measurements or clinical claims.
