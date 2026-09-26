"""What a command handler gets besides its arguments, and how it prints.

Every command builds one payload and prints it on stdout in the mode
``render.resolve_mode`` picked: a table or key-value view on a terminal,
tab-separated lines when piped, or the payload as one JSON document.
Notices (empty results, deprecations, notes) go to stderr (STD-01 §R12).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

import httpx

from pulsar.core import Paths

from . import views
from .render import Mode, Terminal, View, render


class Context:
    """The home, resolved once per invocation, the output mode and terminal,
    and the transport (tests pass a fake one)."""

    def __init__(
        self,
        paths: Paths,
        transport: httpx.AsyncBaseTransport | None,
        mode: Mode = "json",
        term: Terminal | None = None,
    ) -> None:
        self.paths = paths
        self.transport = transport
        self.mode: Mode = mode
        self.term = term or Terminal()


Handler = Callable[[argparse.Namespace, Context], int]


def emit(report: tuple[dict[str, Any], int], ctx: Context, view: View = views.detail) -> int:
    """Print a ``(payload, exit_code)`` report through ``view``; return its code."""
    payload, code = report
    if ctx.mode == "json":
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        note = payload.get(views.NOTE)
        if isinstance(note, str) and note:
            notice(note)
        text = render(view(payload), ctx.mode, ctx.term)
    sys.stdout.write(text)
    sys.stdout.flush()
    return code


def notice(message: str) -> None:
    print(f"pulsar: {message}", file=sys.stderr)
