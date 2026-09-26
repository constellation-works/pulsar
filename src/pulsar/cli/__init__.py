"""The ``pulsar`` command line: human auth management, the operator verbs and
the MCP server.

``run`` parses and dispatches against the app ``pulsar.main`` supplies; each
command lives in ``commands/``; a command's payload is printed through
``context.emit``, rendered by ``render`` through its view in ``views``, and
failures by ``errors``.
"""

from .main import build_parser, run

__all__ = ["build_parser", "run"]
