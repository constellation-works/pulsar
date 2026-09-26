"""``pulsar serve`` and ``pulsar orbit-tool``: pulsar's other front ends, run
from the CLI. They are imported only when invoked."""

from __future__ import annotations

import argparse

from ..context import Context, notice
from ..errors import EXIT_OK, UsageError
from ..parser import Commands

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8977


def register(commands: Commands) -> None:
    serve = commands.add("serve", help="run the MCP server", group="Services")
    serve.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio, or streamable HTTP on a loopback port; default: stdio",
    )
    serve.add_argument(
        "--host",
        choices=LOOPBACK_HOSTS,
        default=None,
        help=f"loopback address for --transport http; default: {LOOPBACK_HOSTS[0]}",
    )
    serve.add_argument(
        "--port",
        type=_port,
        default=None,
        help=f"port for --transport http; default: {DEFAULT_PORT}",
    )
    serve.set_defaults(func=_serve)

    p_orbit = commands.add(
        "orbit-tool",
        group="Services",
        help="answer one Orbit plugin request (envelope on stdin, response on stdout)",
    )
    p_orbit.set_defaults(func=_orbit_tool)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"must be 1..65535, got {port}")
    return port


def _orbit_tool(_args: argparse.Namespace, _ctx: Context) -> int:
    from pulsar.orbit_tool import main as orbit_tool_main

    return orbit_tool_main()


def _serve(args: argparse.Namespace, ctx: Context) -> int:
    from pulsar.app.runtime import Runtime
    from pulsar.mcp import build_server, loopback_security

    if args.transport != "http" and (args.host is not None or args.port is not None):
        # stdio has no address: a --host or --port here would be silently unused (STD-01 §R28).
        raise UsageError(
            "--host and --port apply only to --transport http",
            detail={"transport": args.transport},
        )
    rt = Runtime(ctx.paths)
    roots = rt.settings.media_roots
    # stderr: stdout is the MCP stream under --transport stdio.
    notice(
        "media path uploads "
        + (f"confined to {', '.join(map(str, roots))}" if roots else "off (no [media] roots)")
    )
    server = build_server(rt)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        host = args.host or LOOPBACK_HOSTS[0]
        port = args.port or DEFAULT_PORT
        server.run(
            "streamable-http",
            host=host,
            port=port,
            transport_security=loopback_security(host, port),
        )
    return EXIT_OK
