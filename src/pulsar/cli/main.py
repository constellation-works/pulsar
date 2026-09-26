"""The ``pulsar`` command line: build the command tree, parse, resolve the
output mode once, dispatch to a command with the app the entry point
(``pulsar.main``) supplied, and map what a handler raises to an exit code.

``pulsar`` or ``pulsar auth`` alone prints that level's help on stderr and
exits 2. A closed stdout ends the process quietly with 0. The output and
error contract is in ``context`` and ``errors``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from pulsar import __version__
from pulsar.app import INTERNAL, App, PulsarError

from .commands import REGISTER
from .context import Context, Handler
from .errors import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    ParseError,
    UsageError,
    print_error,
    print_usage_error,
)
from .parser import CommandParser, Commands, global_options
from .render import ModeConflict, json_requested, resolve_mode, resolve_terminal

log = logging.getLogger(__name__)

# The root help's sections, in this order; each command names its own.
GROUPS = ("Accounts", "Publish", "Observe", "Maintenance", "Services")


def build_parser() -> CommandParser:
    """The whole command tree; the one declaration help and goldens come from."""
    common = global_options()
    parser = CommandParser(
        prog="pulsar",
        usage="%(prog)s [OPTIONS] COMMAND ...",
        description="Social write connector for the constellation: publish to X as accounts\n"
        "a human bound on this host, through one ledger and policy.",
        epilog="Exit codes: 0 ok, 1 failed or not settled, 2 usage error.\n"
        "Run `pulsar COMMAND --help` for a command's own options and examples.",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"pulsar {__version__}")
    parser.set_defaults(func=None, help_parser=parser)
    for title in GROUPS:
        parser.command_groups[title] = []
    commands = Commands(parser, common, dest="command")
    for register in REGISTER:
        register(commands)
    return parser


def run(argv: Sequence[str] | None, app: App) -> int:
    """Run one invocation against ``app``; the exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = json_requested(raw, os.environ)
    try:
        args = build_parser().parse_args(raw)
    except ParseError as exc:
        print_usage_error(exc, wants_json)
        raise SystemExit(EXIT_USAGE) from None
    handler: Handler | None = args.func
    if handler is None:
        # `pulsar` or `pulsar auth` alone: that level's help, as a usage error.
        args.help_parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        mode = resolve_mode(
            getattr(args, "format", None),
            getattr(args, "json", False),
            os.environ,
            sys.stdout.isatty(),
        )
    except ModeConflict as exc:
        print_error(UsageError(str(exc)), wants_json)
        return EXIT_USAGE
    ctx = Context(app, mode, resolve_terminal(sys.stdout, os.environ))
    json_mode = mode == "json"
    try:
        return handler(args, ctx)
    except UsageError as exc:
        print_error(exc, json_mode)
        return EXIT_USAGE
    except PulsarError as exc:
        print_error(exc, json_mode)
        return EXIT_FAILED
    except BrokenPipeError:
        # `pulsar history | head -1`: the reader left; that is not a failure.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except Exception as exc:
        log.exception("pulsar %s: unexpected error", args.command)
        bug = PulsarError(INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False)
        print_error(bug, json_mode)
        return EXIT_FAILED
