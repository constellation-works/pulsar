"""The ``pulsar`` entry point: the one place that builds the app and supplies
it to a front end. Everything below receives what it needs from here."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from pulsar.app import App, configure_logging, default_paths
from pulsar.cli import run


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    return run(argv, App(default_paths()))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
