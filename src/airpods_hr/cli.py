"""Command-line entry point for airpods-hr."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from typing import TextIO

from airpods_hr.monitor_cli import LiveRunner, run_live_monitor, run_monitor_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airpods-hr",
        description="Experimental heart-rate interoperability for AirPods Pro 3.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    monitor = commands.add_parser(
        "monitor",
        help="continuously display heart-rate samples",
        description="Continuously display experimental heart-rate samples.",
    )
    monitor.add_argument(
        "--dry-run",
        action="store_true",
        help="print the monitor plan without accessing Bluetooth",
    )
    monitor.add_argument(
        "--diagnostic",
        action="store_true",
        help="display evidence-preserving fields for every parsed sample",
    )
    monitor.add_argument(
        "--output",
        metavar="PATH",
        help="write diagnostic session events as UTF-8 JSON Lines",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    live_runner: LiveRunner = run_live_monitor,
) -> int:
    """Parse one product command and preserve no-access help behavior."""

    stdout_stream = stdout or sys.stdout
    stderr_stream = stderr or sys.stderr
    parser = build_parser()
    try:
        with redirect_stdout(stdout_stream), redirect_stderr(stderr_stream):
            args = parser.parse_args(argv)
            if (
                args.command == "monitor"
                and args.output is not None
                and not args.diagnostic
            ):
                parser.error("--output requires --diagnostic")
    except SystemExit as error:
        return int(error.code)

    if args.command is None:
        parser.print_help(file=stdout_stream)
        return 0

    sample_output = lambda message: print(message, file=stdout_stream, flush=True)
    status_output = lambda message: print(message, file=stderr_stream, flush=True)
    try:
        return asyncio.run(
            run_monitor_command(
                dry_run=args.dry_run,
                diagnostic=args.diagnostic,
                output_path=args.output,
                stdout=sample_output,
                stderr=status_output,
                live_runner=live_runner,
            )
        )
    except KeyboardInterrupt:
        status_output("Interrupted while starting the monitor.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
