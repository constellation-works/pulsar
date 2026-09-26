"""The ``pulsar`` command line: human auth management, the operator verbs
(``app/ops.py``) and the MCP server.

``main`` parses and dispatches; each command lives in ``commands/``; a
command's payload is printed through ``context.emit``, rendered by
``render`` through its view in ``views``, and failures by ``errors``.
"""

from .main import build_parser, main

__all__ = ["build_parser", "main"]
