"""Command-line surface for lesvi.

``version``, ``add``, ``list``, ``remove``, ``serve``, ``status`` and
``service`` exist so far; ``url`` remains and lands with its feature ticket.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import sys
from collections.abc import Callable
from pathlib import Path

from lesvi import __version__, service
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
from lesvi.watch import Watcher

PROG = "lesvi"


def log_level(verbose: int) -> int:
    """Map ``-v`` repeats to a log level: quiet, INFO, then DEBUG."""
    if verbose <= 0:
        return logging.WARNING
    if verbose == 1:
        return logging.INFO
    return logging.DEBUG


def _configure_logging(verbose: int) -> None:
    """Send logs to stderr (journald captures both streams for a unit)."""
    level = log_level(verbose)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )
    root.setLevel(level)


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


def _shelf_entries(config: Config) -> list[dict[str, object]]:
    """One summary per registered shelf: path, existence and category counts."""
    entries: list[dict[str, object]] = []
    for name, table in config.shelves().items():
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
    return entries


def _category_detail(entry: dict[str, object]) -> str:
    """A shelf's categories as ``lessons 3, reference 1`` (or ``missing``)."""
    categories = entry["categories"]
    assert isinstance(categories, dict)  # built by _shelf_entries
    if not entry["exists"]:
        return "missing"
    return ", ".join(f"{key} {value}" for key, value in categories.items())


def _cmd_list(args: argparse.Namespace) -> int:
    config = Config.load()
    entries = _shelf_entries(config)
    if not entries:
        print(f"no shelves registered in {config.path}")
        return 0

    if args.json:
        print(json.dumps(entries, indent=2, default=str))
        return 0

    for entry in entries:
        print(f"{entry['name']}  {entry['path']}  {_category_detail(entry)}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """What is registered, how many artifacts it holds, and is it running."""
    config = Config.load(Path(args.config).expanduser() if args.config else None)
    entries = _shelf_entries(config)
    artifacts = 0
    for entry in entries:
        categories = entry["categories"]
        assert isinstance(categories, dict)  # built by _shelf_entries
        artifacts += sum(categories.values())

    print(f"config:    {config.path}")
    print(f"server:    {config.host()}:{config.port()}")
    public_url = config.public_url()
    if public_url:
        print(f"public:    {public_url}")
    print(f"service:   {service.unit_state()}")
    print(f"shelves:   {len(entries)}")
    print(f"artifacts: {artifacts}")
    for entry in entries:
        print(f"{entry['name']}  {entry['path']}  {_category_detail(entry)}")
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
    watch_enabled = config.watch() and not args.no_watch
    poll_interval = (
        args.poll_interval
        if args.poll_interval is not None
        else config.poll_interval()
    )
    # Passing --poll-interval is a request for polling, watchfiles or not.
    prefer_watchfiles = args.poll_interval is None
    state = PinState.load()
    index = Index.build(config, state)
    try:
        server = make_server(index, host, port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise ConfigError(
                f"port {port} is already in use on {host}; "
                f"find the culprit with: ss -tlnp | grep {port}"
            ) from exc
        raise ConfigError(f"cannot bind {host}:{port}: {exc}") from exc

    watcher: Watcher | None = None
    if watch_enabled:

        def publish(updated: Index) -> None:
            server.index = updated

        watcher = Watcher(
            config,
            index,
            state=state,
            poll_interval=poll_interval,
            prefer_watchfiles=prefer_watchfiles,
            on_update=publish,
        )

    bound_host, bound_port = (
        str(server.server_address[0]),
        int(server.server_address[1]),
    )
    # Attach the watcher before announcing the URL: anything written once a
    # reader can reach the server is guaranteed to be seen.
    if watcher is not None:
        watcher.start()
    print(f"config:    {config.path}")
    print(f"shelves:   {len(index.shelves)}")
    print(f"artifacts: {len(index.artifacts)}")
    if watcher is None:
        print("watch:     off")
    elif watcher.using_watchfiles:
        print("watch:     native (watchfiles)")
    else:
        print(f"watch:     polling every {poll_interval:g}s")
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
        if watcher is not None:
            watcher.stop()
        server.server_close()
    return 0


def _cmd_service_install(args: argparse.Namespace) -> int:
    config = Config.load(Path(args.config).expanduser() if args.config else None)
    unit = service.install(config)
    print(f"Installed {unit}")
    print("Enable and start it now, and keep it running after logout:")
    print()
    print("  systemctl --user enable --now lesvi")
    print("  loginctl enable-linger $USER")
    return 0


def _cmd_service_uninstall(_args: argparse.Namespace) -> int:
    unit = service.uninstall()
    print(f"Removed {unit}")
    return 0


def _cmd_service_status(_args: argparse.Namespace) -> int:
    print(f"unit:  {service.unit_path()}")
    print(f"state: {service.unit_state()}")
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


def _positive_seconds(value: str) -> float:
    """Argparse type: a finite, positive number of seconds."""
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("interval must be positive")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Single-port local library for agent-generated lessons and reference notes."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log more: -v for info, -vv for debug (default: warnings only)",
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
    serve_parser.add_argument(
        "--no-watch",
        action="store_true",
        help="serve the startup index without watching shelves",
    )
    serve_parser.add_argument(
        "--poll-interval",
        type=_positive_seconds,
        metavar="S",
        help="poll mtimes every S seconds; forces polling over watchfiles",
    )
    serve_parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,  # let the root flag stand when not repeated
        help="log more: -v for info, -vv for debug",
    )
    serve_parser.set_defaults(handler=_cmd_serve)

    status_parser = subparsers.add_parser(
        "status", help="what is registered and whether the service is running"
    )
    status_parser.add_argument("--config", help="config file (default $LESVI_CONFIG)")
    status_parser.set_defaults(handler=_cmd_status)

    service_parser = subparsers.add_parser(
        "service", help="manage the always-on systemd user service"
    )
    service_actions = service_parser.add_subparsers(
        dest="service_command", metavar="ACTION", required=True
    )
    service_install = service_actions.add_parser(
        "install", help="write the user unit for this config"
    )
    service_install.add_argument(
        "--config", help="config file (default $LESVI_CONFIG)"
    )
    service_install.set_defaults(handler=_cmd_service_install)
    service_uninstall = service_actions.add_parser(
        "uninstall", help="stop and remove the user unit"
    )
    service_uninstall.set_defaults(handler=_cmd_service_uninstall)
    service_status = service_actions.add_parser(
        "status", help="report the user unit state"
    )
    service_status.set_defaults(handler=_cmd_service_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code (0 ok, 1 error, 2 usage)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:  # a subcommand was added without wiring a handler
        parser.error(f"command '{args.command}' has no handler")
    try:
        return handler(args)
    except (ConfigError, OSError, service.ServiceError) as exc:
        return _fail(str(exc))
