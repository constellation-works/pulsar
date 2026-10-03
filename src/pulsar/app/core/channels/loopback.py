"""The loopback redirect a human login comes back to, whatever its provider.

A login opens the provider's consent page in a browser and waits here for the
redirect: ``http://127.0.0.1:8976/callback`` (on a remote host the human
forwards it with ``ssh -L 8976:127.0.0.1:8976``). Only one login runs at a
time, so every provider shares the address.

The listener is not trusted because it is loopback: a request counts only
with a ``Host`` naming the exact authority it bound (``127.0.0.1:<port>`` or
``localhost:<port>``), no ``Origin`` other than that same loopback origin,
and the ``state`` this login sent. Anything else is refused and recorded
nowhere, so another local page or process cannot end the wait or feed in a
code. The consent URL goes to ``notify`` (stderr by default), never to stdout.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from pulsar.internal.errors import API_ERROR, PulsarError

HOST = "127.0.0.1"
PORT = 8976
PATH = "/callback"
# How long a login waits for the human to approve.
WAIT_SECONDS = 300.0


def redirect_uri() -> str:
    return f"http://{HOST}:{PORT}{PATH}"


class CallbackServer(HTTPServer):
    """The loopback listener for one login: accepts only the redirect it is waiting for."""

    def __init__(self, state: str, *, host: str = HOST, port: int = PORT, path: str = PATH) -> None:
        super().__init__((host, port), _Callback)
        self.expected_state = state
        self.callback_path = path
        self.result: dict[str, list[str]] | None = None
        self.timeout = 1.0
        self.port: int = self.server_address[1]
        # The exact authorities the browser may name: what we bound, nothing wider.
        self.authorities = frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def wait(self, timeout: float) -> dict[str, list[str]]:
        """Serve until a request with the right ``state`` arrives; ``api_error`` on timeout."""
        deadline = time.monotonic() + timeout
        while self.result is None and time.monotonic() < deadline:
            self.handle_request()
        if self.result is None:
            raise PulsarError(API_ERROR, "timed out waiting for the browser redirect")
        return self.result


class _Callback(BaseHTTPRequestHandler):
    server: CallbackServer  # pyright: ignore[reportIncompatibleVariableOverride]
    # A connection that sends nothing (a browser's preconnect) is dropped after
    # this long, so it cannot hold ``wait`` past its deadline.
    timeout = 5.0

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            self._reply(400, b"pulsar: missing Host header.")
            return
        if host not in self.server.authorities:
            self._reply(421, b"pulsar: this listener only answers to its own loopback address.")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() != f"http://{host}":
            self._reply(403, b"pulsar: cross-origin requests are refused.")
            return
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self._reply(404, b"pulsar: not found.")
            return
        query = parse_qs(parsed.query)
        if not secrets.compare_digest(query.get("state", [""])[0], self.server.expected_state):
            # Not our redirect: refuse it and keep waiting for the real one.
            self._reply(400, b"pulsar: state mismatch; this is not the login in progress.")
            return
        self.server.result = query
        if "code" in query:
            self._reply(200, b"pulsar: authorization received, you can close this tab.")
        else:
            self._reply(400, b"pulsar: authorization failed; check the terminal.")

    def log_message(self, format: str, *args: object) -> None:  # keep the terminal quiet
        return


def notify_stderr(message: str) -> None:
    """The default ``notify``: the human reads the consent URL on stderr."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def open_quietly(url: str) -> None:
    """``webbrowser.open`` with file descriptor 1 on ``/dev/null``.

    A launched browser inherits stdout, and some launchers print to it
    ("Opening in existing browser session."), which would land before the
    command's JSON. Login writes nothing to stdout until the
    human has approved, so the brief swap cannot hide pulsar's own output.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        webbrowser.open(url)
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(devnull)
