# Daemon distribution and user-service installation

Iteration 9.10A packages a relocatable systemd user-service installer with the
unpublished `airpods-hr-linux` 0.1.0 release candidate. This is an experimental
RC, not a 1.0 stability promise. Real installed-service hardware validation is
reserved for SDK packaging.

## Environment requirement

Install `airpods-hr-linux` into its own Python 3.14 environment using pipx or an
equivalent isolated package-environment tool. The installed environment must
provide all declared production dependencies, including `bumble==0.0.234` and
`dbus-next>=0.2.3`.

The package provides three commands:

- `airpods-hr` for the existing research CLI
- `airpods-hubd` for the production daemon process
- `airpods-hubd-service` for its systemd user-service definition

The service installer records the absolute Python interpreter belonging to the
environment in which it runs. The generated command is equivalent to:

```text
"/absolute/path/to/the/environment/bin/python" -m airpods_hr._hubd.main
```

It does not depend on the repository, the current working directory, a shell
wrapper, or a daemon-time `PATH` lookup.

Interpreter paths containing spaces are supported, and literal percent signs
are escaped for systemd specifier processing. The installer rejects paths
containing control characters, single or double quotes, backslashes, dollar
signs, or systemd glob metacharacters (`*`, `?`, `[`) before writing a unit or
calling systemctl because those executable tokens cannot be represented
reliably for the systemd service parser.

## Inspect and install

Preview the resolved unit destination, `ExecStart`, and systemctl actions:

```console
airpods-hubd-service install --dry-run
```

Dry run writes no file, invokes no systemctl command, starts no daemon, and
touches no Bluetooth state.

Install the service definition:

```console
airpods-hubd-service install
```

The default destination is
`~/.config/systemd/user/airpods-hubd.service`. If `XDG_CONFIG_HOME` is set, the
destination is
`$XDG_CONFIG_HOME/systemd/user/airpods-hubd.service`. Installation needs no
`sudo`. It writes the unit atomically with mode `0600` and then runs
`systemctl --user daemon-reload` with a bounded, noninteractive command.

The default install does not enable or start the daemon. To enable activation
at login without starting it immediately, request that action explicitly:

```console
airpods-hubd-service install --enable
```

Starting the service remains a separate user action after installation:

```console
systemctl --user start airpods-hubd.service
```

Do not perform that start as part of Iteration 9.10A validation. SDK packaging will
validate the installed-service path with real AirPods hardware.

## Verify installation state

Inspect the unit without requiring a running daemon:

```console
airpods-hubd-service verify
```

The command reports whether the expected unit exists, whether its project
ownership marker is present, and whether its `ExecStart` matches the current
installed Python environment. A missing, foreign, or mismatched unit returns a
nonzero status.

The installer refuses to overwrite an unrecognized unit. A deliberate
replacement is available with `install --force`; inspect the destination and
dry-run output first.

## Remove the service definition

Remove an owned unit and reload the user manager:

```console
airpods-hubd-service uninstall
```

This removes only the project-owned unit. It does not uninstall the Python
environment, stop or disable an already running service, change pairing data,
reset AirPods, or remove unrelated units. It refuses to delete an unrecognized
unit. To disable login activation as part of removal, request it explicitly:

```console
airpods-hubd-service uninstall --disable
```

Use `uninstall --dry-run` to inspect the destination and planned systemctl
actions without changing anything. Service installation and removal never
pair, reset, reconnect, or otherwise configure AirPods.
