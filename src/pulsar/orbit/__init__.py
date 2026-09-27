"""The Orbit plugin backend: one tool call per process, from the envelope
Orbit writes on stdin to the result on stdout (``backend``). ``bin/pulsar``
reaches it through ``pulsar orbit-tool``.
"""

from __future__ import annotations

from .backend import main

__all__ = [
    # backend
    "main",
]
