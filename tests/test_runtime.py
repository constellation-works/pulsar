"""The composition root's own contracts: log redaction and the caller label."""

from __future__ import annotations

import asyncio
import io
import logging

import pytest

from pulsar.app.runtime import CALLER_ENV, RedactingFilter, configure_logging
from pulsar.core.errors import AuthExpired, PulsarError
from pulsar.core.settings import Settings
from pulsar.core.store import FernetFileStore

from .conftest import ALIAS, make_runtime, register

LEAKED_VALUE = "abcdefghijklmnopqrstuvwxyz0123456789"
LEAKED = f"Authorization: Bearer {LEAKED_VALUE}"


@pytest.fixture
def captured() -> tuple[logging.Logger, io.StringIO]:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("pulsar.test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, sink
    logger.removeHandler(handler)


def test_the_log_filter_masks_messages_and_their_arguments(captured):
    logger, sink = captured
    logger.warning(LEAKED)
    logger.warning("the provider answered %r", {"echo": LEAKED})
    out = sink.getvalue()
    assert LEAKED_VALUE not in out
    assert out.count("[redacted:bearer header]") == 2


def test_the_log_filter_masks_tracebacks(captured):
    logger, sink = captured
    try:
        raise RuntimeError(f"request failed: {LEAKED}")
    except RuntimeError:
        logger.exception("upload failed")
    out = sink.getvalue()
    assert "Traceback" in out and "upload failed" in out
    assert LEAKED_VALUE not in out


def test_configure_logging_installs_one_redacting_stderr_handler(monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.NOTSET)
    configure_logging()
    configure_logging()
    (handler,) = root.handlers
    assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    logging.getLogger("pulsar.test").warning(LEAKED)
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout carries the payload
    assert "[redacted:bearer header]" in captured.err and LEAKED_VALUE not in captured.err


def test_the_caller_label_prefers_the_argument_then_the_environment(paths, monkeypatch):
    runtime = make_runtime(paths)
    assert runtime.caller(None) == "unknown"
    monkeypatch.setenv(CALLER_ENV, "nightly-routine")  # read live, after construction
    assert runtime.caller(None) == "nightly-routine"
    assert runtime.caller("release-bot") == "release-bot"


def test_a_credential_shaped_caller_is_refused(paths):
    with pytest.raises(PulsarError) as exc:
        make_runtime(paths).caller(f"bot {LEAKED}")
    assert exc.value.code == "secret_detected"
    assert LEAKED_VALUE not in str(exc.value)


def _off_the_loop(calls: list[str], name: str, fn):
    """``fn``, recording ``name`` and failing if it runs on the event loop's thread."""

    def wrapper(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return fn(*args, **kwargs)
        raise AssertionError(f"{name} ran on the event loop")

    return wrapper


@pytest.mark.anyio
async def test_account_and_credential_io_runs_off_the_event_loop(
    paths, authed, fake_x, monkeypatch
):
    rt = make_runtime(paths, settings=Settings(), transport=fake_x.transport())
    calls: list[str] = []
    monkeypatch.setattr(rt, "account", _off_the_loop(calls, "account", rt.account))
    registry = rt.registry
    for name in ("mark_verified", "mark_status"):
        monkeypatch.setattr(registry, name, _off_the_loop(calls, name, getattr(registry, name)))
    load = FernetFileStore.load
    monkeypatch.setattr(
        FernetFileStore, "load", lambda self: _off_the_loop(calls, "load", load)(self)
    )
    try:
        await rt.identity()  # unverified: asks X, then records the identity
        fake_x.fail_auth_once, fake_x.refresh_status = True, 401
        with pytest.raises(AuthExpired):
            await rt.identity(live=True)
    finally:
        await rt.aclose()
    assert {"account", "load", "mark_verified", "mark_status"} <= set(calls)


@pytest.mark.parametrize("status", ["active", "revoked"])
@pytest.mark.anyio
async def test_a_missing_login_names_the_home_to_log_in_to(paths, status):
    """Under Orbit the home is the plugin's, so a bare ``pulsar auth login``
    would bind the operator's default home instead."""
    register(paths, None, status=status)
    rt = make_runtime(paths, settings=Settings())
    try:
        with pytest.raises(AuthExpired) as exc:
            await rt.identity(ALIAS)
    finally:
        await rt.aclose()
    assert f"`PULSAR_HOME={paths.home} pulsar auth login --account {ALIAS}`" in exc.value.message
