"""Command-line surface for lesvi.

``version``, ``add``, ``list``, ``remove`` and ``serve`` exist so far; the
remaining commands from the spec (``url``, ``status``, ``service``) land with
their feature tickets.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from lesvi import __version__
from lesvi.config import (
    Config,
    ConfigError,
    category_counts,
    matches_preset,
    resolve_path,
    shelf_categories,
    shelf_ignores,
    slugify,
    validate_host,
)
from lesvi.index import Index
from lesvi.server import make_server
from lesvi.state import PinState

PROG = "lesvi"


def _fail(message: str) -> int:
    """Print ``error: message`` on stderr and return the error exit code."""
    print(f"{PROG}: error: {message}", file=sys.stderr)
    return 1


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}")
    return 0


def _derived_name(raw: str) -> str:
    name = slugify(raw)
    if not name:
        raise ConfigError(
            f"cannot derive a shelf name from {raw!r}; register it with --name"
        )
    return name


def _cmd_add(args: argparse.Namespace) -> int:
    config = Config.load()
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        raise ConfigError(f"path does not exist: {args.path}")
    if not target.is_dir():
        raise ConfigError(f"not a directory: {args.path}")

    if args.single or matches_preset(target):
        candidates = [(args.name or target.name, target, args.title)]
    else:
        children = sorted(
            child
            for child in target.iterdir()
            if child.is_dir() and matches_preset(child)
        )
        if not children:
            raise ConfigError(
                f"nothing matching the preset under {target}; "
                "use --single to register it as one shelf"
            )
        if len(children) > 1 and (args.name or args.title):
            raise ConfigError(
                "--name and --title require --single when several shelves match"
            )
        if len(children) == 1:
            child = children[0]
            candidates = [(args.name or child.name, child, args.title)]
        else:
            candidates = [(child.name, child, None) for child in children]

    planned: list[tuple[str, Path, str | None]] = []
    notes: list[str] = []
    for raw_name, path, title in candidates:
        name = _derived_name(raw_name)
        existing = config.find_shelf_by_path(path)
        if existing is not None:
            notes.append(f"shelf {existing!r} is already registered -> {path}")
            continue
        if config.has_shelf(name):
            raise ConfigError(
                f"shelf {name!r} is already registered at a different path; "
                f"register {path} directly with --name"
            )
        if any(planned_name == name for planned_name, _, _ in planned):
            raise ConfigError(
                f"two matching folders map to the same shelf name {name!r}; "
                "pass --single to register the parent instead"
            )
        planned.append((name, path, title))

    if not planned:
        for note in notes:
            print(note)
        return 0

    for name, path, title in planned:
        config.add_shelf(name, path, title=title)
    config.save()
    shelves = config.shelves()
    for name, _path, _title in planned:
        print(f"Added shelf {name!r} -> {shelves[name].get('path', '')}")
    for note in notes:
        print(note)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    config = Config.load()
    shelves = config.shelves()
    if not shelves:
        print(f"no shelves registered in {config.path}")
        return 0

    entries: list[dict[str, object]] = []
    for name, table in shelves.items():
        stored = str(table.get("path", ""))
        path = resolve_path(stored, config.path.parent)
        exists = path.is_dir()
        counts = (
            category_counts(path, shelf_categories(table), shelf_ignores(table))
            if exists
            else {}
        )
        entries.append(
            {
                "name": str(name),
                "path": stored,
                "title": table.get("title"),
                "exists": exists,
                "categories": counts,
            }
        )

    if args.json:
        print(json.dumps(entries, indent=2, default=str))
        return 0

    for entry in entries:
        categories = entry["categories"]
        assert isinstance(categories, dict)  # built above
        detail = (
            ", ".join(f"{key} {value}" for key, value in categories.items())
            if entry["exists"]
            else "missing"
        )
        print(f"{entry['name']}  {entry['path']}  {detail}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    config = Config.load()
    target = args.target
    if not config.has_shelf(target):  # a path, resolved from the current dir
        target = str(Path(target).expanduser().resolve())
    name = config.remove_shelf(target)
    config.save()
    print(f"Removed shelf {name!r}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    config = Config.load(Path(args.config).expanduser() if args.config else None)
    if args.host is not None:
        try:
            host = validate_host(args.host)
        except ConfigError as exc:
            raise ConfigError(f"--host: {exc}") from exc
    else:
        host = config.host()
    port = args.port if args.port is not None else config.port()
    index = Index.build(config, PinState.load())
    try:
        server = make_server(index, host, port)
    except OSError as exc:
        raise ConfigError(
            f"cannot bind {host}:{port}: {exc}; is another process listening? "
            f"try: ss -tlnp | grep {port}"
        ) from exc

    bound_host, bound_port = (
        str(server.server_address[0]),
        int(server.server_address[1]),
    )
    print(f"config:    {config.path}")
    print(f"shelves:   {len(index.shelves)}")
    print(f"artifacts: {len(index.artifacts)}")
    print(f"local:     http://{bound_host}:{bound_port}")
    public_url = config.public_url()
    if public_url:
        print(f"public:    {public_url}")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()  # move the shell prompt off the ^C
    finally:
        server.server_close()
    return 0


def _port(value: str) -> int:
    """Argparse type: a TCP port, ``0`` meaning "pick an ephemeral port"."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return number


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

    add_parser = subparsers.add_parser(
        "add", help="register a shelf, or every matching child of a folder"
    )
    add_parser.add_argument("path", help="shelf folder, or a folder of shelves")
    add_parser.add_argument(
        "--name", help="shelf name (slugified); with several matches, use --single"
    )
    add_parser.add_argument("--title", help="display title override")
    add_parser.add_argument(
        "--single", action="store_true", help="register PATH itself as one shelf"
    )
    add_parser.set_defaults(handler=_cmd_add)

    list_parser = subparsers.add_parser("list", help="show the registered shelves")
    list_parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    list_parser.set_defaults(handler=_cmd_list)

    remove_parser = subparsers.add_parser("remove", help="forget a registered shelf")
    remove_parser.add_argument("target", help="shelf name or path")
    remove_parser.set_defaults(handler=_cmd_remove)

    serve_parser = subparsers.add_parser("serve", help="serve the library on one port")
    serve_parser.add_argument(
        "--port", type=_port, help="listen port, 0-65535 (default 8787)"
    )
    serve_parser.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve_parser.add_argument("--config", help="config file (default $LESVI_CONFIG)")
    serve_parser.set_defaults(handler=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code (0 ok, 1 error, 2 usage)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:  # a subcommand was added without wiring a handler
        parser.error(f"command '{args.command}' has no handler")
    try:
        return handler(args)
    except (ConfigError, OSError) as exc:
        return _fail(str(exc))
