"""The ``pulsar`` command line: build the command tree, parse, resolve the
output mode once, dispatch to a command with the app the entry point
(``pulsar.main``) supplied, and map what a handler raises to an exit code.

``pulsar`` or ``pulsar auth`` alone prints that level's help on stderr and
exits 2. A closed stdout ends the process quietly with 0. The output and
error contract is in ``toolkit.context`` and ``toolkit.errors``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from pulsar import __version__
from pulsar.app import INTERNAL, App, PulsarError

from . import commands, toolkit

log = logging.getLogger(__name__)

# The root help's sections, in this order; each command names its own.
GROUPS = ("Accounts", "Publish", "Observe", "Maintenance", "Services")


def build_parser() -> toolkit.CommandParser:
    """The whole command tree; the one declaration help and goldens come from."""
    common = toolkit.global_options()
    parser = toolkit.CommandParser(
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
    top = toolkit.Commands(parser, common, dest="command")
    for register in commands.REGISTER:
        register(top)
    return parser


def run(argv: Sequence[str] | None, app: App) -> int:
    """Run one invocation against ``app``; the exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = toolkit.json_requested(raw, os.environ)
    try:
        args = build_parser().parse_args(raw)
    except toolkit.ParseError as exc:
        toolkit.print_usage_error(exc, wants_json)
        raise SystemExit(toolkit.EXIT_USAGE) from None
    handler: toolkit.Handler | None = args.func
    if handler is None:
        # `pulsar` or `pulsar auth` alone: that level's help, as a usage error.
        args.help_parser.print_help(sys.stderr)
        return toolkit.EXIT_USAGE
    try:
        mode = toolkit.resolve_mode(
            getattr(args, "format", None),
            getattr(args, "json", False),
            os.environ,
            sys.stdout.isatty(),
        )
    except toolkit.ModeConflict as exc:
        toolkit.print_error(toolkit.UsageError(str(exc)), wants_json)
        return toolkit.EXIT_USAGE
    ctx = toolkit.Context(app, mode, toolkit.resolve_terminal(sys.stdout, os.environ))
    json_mode = mode == "json"
    try:
        return handler(args, ctx)
    except toolkit.UsageError as exc:
        toolkit.print_error(exc, json_mode)
        return toolkit.EXIT_USAGE
    except PulsarError as exc:
        toolkit.print_error(exc, json_mode)
        return toolkit.EXIT_FAILED
    except BrokenPipeError:
        # `pulsar history | head -1`: the reader left; that is not a failure.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return toolkit.EXIT_OK
    except Exception as exc:
        log.exception("pulsar %s: unexpected error", args.command)
        bug = PulsarError(INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False)
        toolkit.print_error(bug, json_mode)
        return toolkit.EXIT_FAILED
