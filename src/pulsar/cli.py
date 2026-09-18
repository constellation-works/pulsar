"""``pulsar`` command line: human auth management and the MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from . import __version__
from .auth import load_client_id, login
from .config import CALLBACK_HOST, default_paths
from .errors import PulsarError
from .store import TokenStore


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
    # human just bound, not what a previous login cached.
    args.offline = False
    return _auth_status(args)


def _auth_status(args: argparse.Namespace) -> int:
    paths = default_paths()
    store = TokenStore(paths)
    bundle = store.load()
    out = {
        "home": str(paths.home),
        "client_id": load_client_id(paths),
        "authorized": bundle is not None,
        "reauth_required": bundle is None or not bundle.refresh_token,
        "access_token_expires_in_s": None
        if bundle is None
        else int(bundle.expires_at - time.time()),
        "scope": None if bundle is None else bundle.scope,
        "account": None,
    }
    if bundle is not None and not getattr(args, "offline", False):
        from .server import Runtime

        rt = Runtime(paths)

        async def probe() -> None:
            try:
                out["account"] = await rt.whoami()
            except PulsarError as exc:
                out["error"] = exc.to_result()
                if exc.code == "auth_expired":
                    out["reauth_required"] = True
            finally:
                await rt.client.aclose()

        asyncio.run(probe())
    print(json.dumps(out, indent=2))
    return 0 if out["authorized"] and not out["reauth_required"] else 1


def _auth_logout(_: argparse.Namespace) -> int:
    TokenStore(default_paths()).clear()
    print("token bundle removed")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import build_server

    server = build_server()
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
    p_status.add_argument(
        "--offline", action="store_true", help="do not call X; report stored state only"
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
