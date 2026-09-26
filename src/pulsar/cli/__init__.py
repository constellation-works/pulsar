"""The ``pulsar`` command line: human auth management, the operator verbs and
the MCP server.

``run`` (``main.py``) parses and dispatches against the app ``pulsar.main``
supplies. Beneath it, ``commands/`` holds the commands, one module each, and
beneath them ``toolkit/`` holds what they are written against: a payload is
printed through ``context.emit``, rendered by ``render`` through its view in
``views``, and failures by ``errors``.
"""

from .main import build_parser, run

__all__ = ["build_parser", "run"]
