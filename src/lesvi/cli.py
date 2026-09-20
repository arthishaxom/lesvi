"""Command-line surface for lesvi.

Only ``version`` exists so far; the remaining commands from the spec
(``serve``, ``add``, ``remove``, ``list``, ``url``, ``status``, ``service``)
land with their feature tickets.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from lesvi import __version__

PROG = "lesvi"


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Single-port local library for agent-generated lessons and reference notes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    version_parser = subparsers.add_parser("version", help="print the lesvi version")
    version_parser.set_defaults(handler=_cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code (0 ok, 1 error, 2 usage)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:  # a subcommand was added without wiring a handler
        parser.error(f"command '{args.command}' has no handler")
    return handler(args)
