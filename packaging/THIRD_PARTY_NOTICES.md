# Third-party notices

Linux Client for AirPods Pro is MIT licensed. The AppImage also contains
software under other licenses; the project license does not replace them.

The build keeps license texts and dependency sources under
`usr/share/licenses/airpods-hr-linux` in the AppImage:

- Python 3.14 and its linked libraries: license texts and build metadata from
  the matching python-build-standalone full archive. Python's own license
  also remains in its standard library. The unused optional `_dbm` extension
  is excluded from the AppImage, so its Berkeley DB code is not distributed.
  Upstream `PYTHON.json` and license texts are retained unchanged as build
  records; they describe the original upstream build, including optional
  components. `payload-inventory.json` records the actual distributed ELF
  files and exclusions separately.
- Python packages: their wheel license files, including vendored notices,
  remain in the installed packages and are copied into the license directory.
  pySerial's omitted wheel notice comes from its matching pinned source archive.
- Rust dependencies: the complete Cargo-verified source tree, including
  license, font, and notice files, plus the lockfile.
- Bundled system libraries: the installed RPM license texts, exact package
  identities, and the exact corresponding source RPMs identified by each binary
  package's `SOURCERPM` metadata. `system-packages.json` maps each bundled
  library to its binary RPM, source RPM, and verified source file hash.
- AppImage runtime: its MIT source and build scripts, the patched libfuse
  3.15.0 source (LGPL-2.1), and squashfuse, musl, zlib, and zstd sources.
  The runtime is redistributed unmodified. Its source includes the libfuse
  patch and instructions for rebuilding/relinking it. Mimalloc sources and
  the libusb sources used by the Python USB package are included too.

The package includes the project's source archive as well. Sources can be
read by extracting the AppImage with `--appimage-extract`. Nothing prevents
modifying or replacing the extracted libraries or rebuilding the application.

The build tools are not included in the user payload. The manifest records
which pinned Rust, Python, appimagetool, and AppImage runtime artifacts were
used. Review the generated license inventory with the artifact before public
release; writing a build script is not a completed redistribution review.
