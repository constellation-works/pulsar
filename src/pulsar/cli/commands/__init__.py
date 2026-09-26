"""The ``pulsar`` commands, one module each (or one per help group of a few),
and beneath them what every command is written against: ``context`` (what a
handler gets and how it prints), ``parser`` (how a command declares itself),
``errors``, ``render`` and ``views``.

Each command module declares its commands in ``register`` and holds their
handlers. ``REGISTER`` is the order argparse lists them in. The names below
``REGISTER`` are what ``cli.main`` needs to parse, dispatch and report.
"""

from . import auth, history, maintenance, publish, reconcile, services, status
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

REGISTER = (
    auth.register,
    status.register,
    history.register,
    publish.register,
    reconcile.register,
    maintenance.register,
    services.register,
)

__all__ = [
    "REGISTER",
    # context
    "Context",
    "Handler",
    # errors
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "ParseError",
    "UsageError",
    "print_error",
    "print_usage_error",
    # parser
    "CommandParser",
    "Commands",
    "global_options",
    # render
    "ModeConflict",
    "json_requested",
    "resolve_mode",
    "resolve_terminal",
]
