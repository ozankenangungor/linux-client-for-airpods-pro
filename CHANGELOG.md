# Changelog

## 0.1.0 release candidate

This repository prepares, but has not published, the 0.1.0 release candidate.

### Capabilities

- Experimental Linux heart-rate interoperability with tested AirPods Pro 3.
- A persistent `airpods-hubd` process that shares one production session with
  local clients through Unix IPC.
- BlueZ and A2DP coexistence on the tested hardware and firmware setup.
- Rust and standard-library-only Python `airpods-client` v0.1 SDKs.
- A relocatable systemd user-service installer for the production Python
  distribution.
- Hardware-independent CI and a provenance-bearing local release validator for
  the production package and both SDKs.

### Limitations

- The project is unofficial, reverse-engineered, and experimental.
- Compatibility evidence is limited to the tested AirPods Pro 3 setup; other
  models, firmware, controllers, and Linux distributions are not established.
- Heart-rate values carry no medical or clinical accuracy guarantee.
- Automatic reconnect and suspend/resume recovery are not implemented.
- IPC version 1 is coordinated across the v0.1 clients and daemon but remains
  experimental.
- The Python packages and Rust crate are not published at this RC-preparation
  stage.
