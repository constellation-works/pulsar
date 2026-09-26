"""``pulsar`` command line: human auth management and the MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx

from . import __version__
from .auth import load_client_id, login
from .config import CALLBACK_HOST, Paths, default_paths
from .errors import PulsarError
from .store import TokenStore, cached_identity
from .xapi import REFRESH_AHEAD_SECONDS


def _auth_login(args: argparse.Namespace) -> int:
    paths = default_paths()
    client_id = args.client_id or load_client_id(paths)
    if not client_id:
        print(
            "error: --client-id is required the first time (the X app's OAuth 2.0 client id)",
            file=sys.stderr,
        )
        return 2
    try:
        bundle = login(paths, client_id, open_browser=not args.no_browser)
    except PulsarError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(
        f"authorized; scopes: {bundle.scope or '(unreported)'}; "
        f"token bundle stored under {paths.home}"
    )
    # Always probe live here: the whole point is to show which account the
    # human just bound, not what a previous login cached. No forced refresh:
    # the token is minutes old, so /users/me alone proves it.
    out, code = asyncio.run(status_report(paths))
    print(json.dumps(out, indent=2))
    return code


def _token_state(expires_in_s: int) -> str:
    if expires_in_s <= 0:
        return "expired"
    return "expiring" if expires_in_s <= REFRESH_AHEAD_SECONDS else "valid"


async def status_report(
    paths: Paths,
    *,
    live: bool = False,
    offline: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], int]:
    """What ``pulsar auth status`` prints, and its exit code.

    The default reads the cached account: it costs nothing, but says nothing
    about whether the refresh token still works (an expired access token is
    reported as ``verified: false``). ``live`` proves it: a forced refresh,
    which rotates the token pair, then ``GET /2/users/me`` bypassing the
    cache. ``offline`` makes no network call at all.
    """
    store = TokenStore(paths)
    try:
        bundle = store.load()
    except PulsarError as exc:  # insecure_storage: a chmod away, not a re-login
        return {"home": str(paths.home), "error": exc.to_result()}, 1
    out: dict[str, Any] = {
        "home": str(paths.home),
        "client_id": load_client_id(paths),
        "authorized": bundle is not None,
        "reauth_required": bundle is None or not bundle.refresh_token,
        "access_token_expires_in_s": None,
        "token_state": None,
        "scope": None,
        "account": None,
        "account_source": None,
        "verified": False,
    }
    if bundle is None:
        return out, 1
    expires_in = int(bundle.expires_at - time.time())
    out.update(
        access_token_expires_in_s=expires_in,
        token_state=_token_state(expires_in),
        scope=bundle.scope,
    )
    cached = cached_identity(paths, bundle)
    if offline:
        if cached is not None:
            out["account"] = cached
            out["account_source"] = "cache"
        return _finish(out)
    from .server import Runtime

    try:
        rt = Runtime(paths, transport=transport)
    except PulsarError as exc:  # invalid_config: the tokens are fine, the settings are not
        out["error"] = exc.to_result()
        return _finish(out)
    try:
        if live:
            fresh = await rt.client.refresh(bundle)
            out["refreshed"] = True
            out["access_token_expires_in_s"] = int(fresh.expires_at - time.time())
            out["token_state"] = _token_state(out["access_token_expires_in_s"])
            out["account"] = await rt.whoami(live=True)
            out["account_source"] = "live"
            out["verified"] = True
            out["whoami_cache"] = "refreshed" if cached is not None else "created"
        else:
            out["account"] = await rt.whoami()
            out["account_source"] = "cache" if cached is not None else "live"
            out["verified"] = cached is None
    except PulsarError as exc:
        out["error"] = exc.to_result()
        if exc.code == "auth_expired":
            out["reauth_required"] = True
    finally:
        await rt.client.aclose()
    return _finish(out)


def _finish(out: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not out["verified"] and not out["reauth_required"]:
        out["note"] = (
            "account read from cache; refresh not exercised"
            + (" and the access token has expired" if out["token_state"] == "expired" else "")
            + " — run `pulsar auth status --live` to prove the binding"
        )
    ok = out["authorized"] and not out["reauth_required"] and "error" not in out
    return out, 0 if ok else 1


def _auth_status(args: argparse.Namespace) -> int:
    out, code = asyncio.run(
        status_report(default_paths(), live=args.live, offline=getattr(args, "offline", False))
    )
    print(json.dumps(out, indent=2))
    return code


def _auth_logout(_: argparse.Namespace) -> int:
    try:
        TokenStore(default_paths()).clear()
    except PulsarError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print("token bundle removed")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import Runtime, build_server

    try:
        rt = Runtime()
    except PulsarError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    roots = rt.settings.media_roots
    # stderr: stdout is the MCP stream under --transport stdio.
    print(
        "pulsar: media path uploads "
        + (f"confined to {', '.join(map(str, roots))}" if roots else "off (no [media] roots)"),
        file=sys.stderr,
    )
    server = build_server(rt)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pulsar", description="X write connector for the constellation"
    )
    parser.add_argument("--version", action="version", version=f"pulsar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="manage the X authorization on this host")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    p_login = auth_sub.add_parser("login", help="run the OAuth 2.0 PKCE flow in a browser")
    p_login.add_argument(
        "--client-id", help="X app OAuth 2.0 client id (remembered after first use)"
    )
    p_login.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening a browser"
    )
    p_login.set_defaults(func=_auth_login)
    p_status = auth_sub.add_parser("status", help="show the bound account and token health")
    status_mode = p_status.add_mutually_exclusive_group()
    status_mode.add_argument(
        "--offline", action="store_true", help="do not call X; report stored state only"
    )
    status_mode.add_argument(
        "--live",
        action="store_true",
        help="prove the binding: force a token refresh (rotates the pair), then GET /users/me",
    )
    p_status.set_defaults(func=_auth_status)
    auth_sub.add_parser("logout", help="delete the stored token bundle").set_defaults(
        func=_auth_logout
    )

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument(
        "--host", default=CALLBACK_HOST, help="bind address for --transport http (default loopback)"
    )
    serve.add_argument("--port", type=int, default=8977)
    serve.set_defaults(func=_serve)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
