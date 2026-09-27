"""The ``pulsar`` entry point: the one place that reads the process (its
environment, cwd and ``$HOME``), builds the app and supplies it to a front
end. Everything below receives what it needs from here. Orbit runs it too,
as ``pulsar orbit-tool`` (``bin/pulsar``)."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pulsar.app.facade import LocalApp
from pulsar.app.runtime import configure_logging, default_paths
from pulsar.cli.main import run


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    environ = os.environ
    app = LocalApp(default_paths(environ, Path.home()), environ=environ, cwd=Path.cwd())
    return run(argv, app)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
