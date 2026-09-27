"""The argparse pieces every command declares itself with.

``CommandParser`` leaves printing a usage error to ``main`` and lists its
subcommands in titled groups the way ``orbit`` does; ``Commands`` adds a
level's subcommands, each with the global options and a place in its
parent's help. The group listing is built from each command's own
declaration.
"""

from __future__ import annotations

import argparse
from typing import Any, NoReturn

from .errors import ParseError
from .render import FORMAT_ENV, FORMATS

ACCOUNT_HELP = "an alias, provider:handle such as x:<handle>"
ACCOUNT_DEFAULT = "default: default_account in config.toml, else the only bound account"


class CommandParser(argparse.ArgumentParser):
    """argparse that raises ``ParseError`` instead of printing and exiting,
    and whose help lists subcommands under ``command_groups``. Subparsers
    inherit the class."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.command_groups: dict[str, list[str]] = {}

    def error(self, message: str) -> NoReturn:
        raise ParseError(message, self.format_usage().strip(), self.prog)

    def format_help(self) -> str:
        if not self.command_groups:
            return super().format_help()
        listed: dict[str, argparse.Action] = {}
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                pseudo = action._get_subactions()  # pyright: ignore[reportPrivateUsage]
                listed = {str(a.metavar): a for a in pseudo}
        fmt = self._get_formatter()
        fmt.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        fmt.add_text(self.description)
        for title, names in self.command_groups.items():
            fmt.start_section(title)
            fmt.add_arguments([listed[name] for name in names])
            fmt.end_section()
        fmt.start_section("Options")
        fmt.add_arguments([a for a in self._actions if a.option_strings])
        fmt.end_section()
        fmt.add_text(self.epilog)
        return fmt.format_help()


def global_options() -> CommandParser:
    """The global flags: declared once, accepted before and after
    the subcommand. SUPPRESS keeps a subcommand from resetting the root's value."""
    common = CommandParser(add_help=False)
    common.add_argument(
        "--format",
        choices=FORMATS,
        default=argparse.SUPPRESS,
        metavar="MODE",
        help="output: auto (a table on a terminal, tab-separated lines when piped), table or "
        f"json; default: ${FORMAT_ENV}, else auto",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="shorthand for --format json",
    )
    return common


class Commands:
    """One level's subcommands (``pulsar …`` or ``pulsar auth …``)."""

    def __init__(self, parent: CommandParser, common: CommandParser, *, dest: str) -> None:
        self.parent = parent
        self.common = common
        self._sub = parent.add_subparsers(dest=dest, metavar="COMMAND", prog=parent.prog)

    def add(self, name: str, *, help: str, group: str, epilog: str | None = None) -> CommandParser:
        """A subcommand listed in the parent's help under ``group``."""
        parser = self._sub.add_parser(
            name,
            help=help,
            description=help,
            epilog=epilog,
            parents=[self.common],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        self.parent.command_groups.setdefault(group, []).append(name)
        return parser
