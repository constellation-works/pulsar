"""What every ``pulsar`` command is written against, and what ``cli.main``
parses, dispatches and reports with: ``parser`` (how a command declares
itself), ``context`` (what a handler gets and how it prints a payload),
``render`` and the per-command ``views`` (how a payload looks), and ``errors``
(how a failure is reported, and its exit code).

None of it is a command; the commands are in ``surfaces/cli/commands``, above it.
"""

from . import views
from .context import Context, Handler, emit, notice
from .errors import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    ParseError,
    UsageError,
    print_error,
    print_usage_error,
)
from .parser import ACCOUNT_DEFAULT, ACCOUNT_HELP, CommandParser, Commands, global_options
from .render import ModeConflict, json_requested, resolve_mode, resolve_terminal

__all__ = [
    "views",
    # context
    "Context",
    "Handler",
    "emit",
    "notice",
    # errors
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "ParseError",
    "UsageError",
    "print_error",
    "print_usage_error",
    # parser
    "ACCOUNT_DEFAULT",
    "ACCOUNT_HELP",
    "CommandParser",
    "Commands",
    "global_options",
    # render
    "ModeConflict",
    "json_requested",
    "resolve_mode",
    "resolve_terminal",
]
