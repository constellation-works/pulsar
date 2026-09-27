"""Deterministic lock handshakes for tests (no sleep-then-assert)."""

from __future__ import annotations

import threading

import pytest

from pulsar.internal.fs import files as fsutil


def blocked_on_lock(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """An event set the first time any pulsar lock wait finds its lock taken.

    Wraps ``fsutil._try_lock``: once it is set, the waiter is inside the
    bounded poll loop, provably waiting rather than not yet started.
    """
    blocked = threading.Event()
    real = fsutil._try_lock  # pyright: ignore[reportPrivateUsage]

    def probe(*args: object, **kwargs: object) -> bool:
        acquired = real(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        if not acquired:
            blocked.set()
        return acquired

    monkeypatch.setattr(fsutil, "_try_lock", probe)
    return blocked
