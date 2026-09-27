"""What a command handler gets besides its arguments, and how it prints.

Every command builds one payload and prints it on stdout in the mode
``render.resolve_mode`` picked: a table or key-value view on a terminal,
tab-separated lines when piped, or the payload as one JSON document.
Notices (empty results, deprecations, notes) go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from pulsar.app.interfaces import App

from . import views
from .render import Mode, Terminal, View, render


class Context:
    """The app the entry point supplied, and the output mode and terminal
    resolved once per invocation."""

    def __init__(self, app: App, mode: Mode = "json", term: Terminal | None = None) -> None:
        self.app = app
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
