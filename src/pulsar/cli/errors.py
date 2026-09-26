"""How a ``pulsar`` failure is printed and what it exits with (STD-01 §R19, §R20).

Exit codes: 0 success, 1 the command failed (or reported something not
settled or not healthy), 2 a usage error. A failure prints nothing on stdout
and ``error: <message>`` on stderr, or in JSON mode one object
``{error, code, retryable, detail}``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pulsar.core import INVALID_ARGUMENT, PulsarError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class UsageError(PulsarError):
    """A bad invocation found after parsing: exit 2, like argparse's own."""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(INVALID_ARGUMENT, message, detail=detail, retryable=False)


class ParseError(Exception):
    """A usage error argparse found; ``main`` prints it in the requested mode."""

    def __init__(self, message: str, usage: str, prog: str) -> None:
        super().__init__(message)
        self.message, self.usage, self.prog = message, usage, prog


def print_error(exc: PulsarError, json_mode: bool) -> None:
    if json_mode:
        body: dict[str, Any] = {
            "error": exc.message,
            "code": exc.code,
            "retryable": exc.retryable,
            "detail": exc.detail,
        }
        line = json.dumps(body, ensure_ascii=False)
    else:
        line = f"error: {exc.message}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def print_usage_error(exc: ParseError, json_mode: bool) -> None:
    if json_mode:
        body = {
            "error": f"{exc.prog}: {exc.message}",
            "code": INVALID_ARGUMENT,
            "retryable": False,
            "detail": {"usage": exc.usage},
        }
        text = json.dumps(body, ensure_ascii=False) + "\n"
    else:
        hint = f"For more information, try '{exc.prog} --help'."
        text = f"error: {exc.message}\n\n{exc.usage}\n\n{hint}\n"
    sys.stderr.write(text)
    sys.stderr.flush()
