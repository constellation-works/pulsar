"""``pulsar`` command line: human auth management, operator verbs (``ops.py``)
and the MCP server.

Output contract (STD-01): every command writes one JSON document to stdout
on success, and on failure one JSON object ``{error, code, retryable,
detail}`` to stderr with nothing on stdout. Exit codes: 0 success, 1 the
command failed (or reported something not settled or not healthy), 2 a
usage error. Notices (empty results, deprecations) go to stderr. A closed
stdout ends the process quietly with 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx

from .. import __version__
from ..core.accounts import AccountRegistry
from ..core.errors import INTERNAL, INVALID_ARGUMENT, PulsarError
from ..core.paths import Paths
from ..core.settings import Settings, load_settings
from ..providers.x.auth import load_client_id, login
from . import ops
from .health import auth_report
from .runtime import configure_logging, default_paths

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8977

Handler = Callable[[argparse.Namespace, "Context"], int]


class UsageError(PulsarError):
    """A bad invocation found after parsing: exit 2, like argparse's own."""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(INVALID_ARGUMENT, message, detail=detail, retryable=False)


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse whose usage errors are the same JSON object as every other
    error (STD-01 §R19): ``{error, code, retryable, detail}`` on stderr, exit 2.
    Subparsers inherit the class."""

    def error(self, message: str) -> NoReturn:
        body = {
            "error": f"{self.prog}: {message}",
            "code": INVALID_ARGUMENT,
            "retryable": False,
            "detail": {"usage": self.format_usage().strip()},
        }
        sys.stderr.write(json.dumps(body, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        raise SystemExit(EXIT_USAGE)


class Context:
    """What a command needs besides its arguments: the home, resolved once per
    invocation, and the transport (tests pass a fake one)."""

    def __init__(self, paths: Paths, transport: httpx.AsyncBaseTransport | None) -> None:
        self.paths = paths
        self.transport = transport


# -- output ---------------------------------------------------------------------------


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _notice(message: str) -> None:
    print(f"pulsar: {message}", file=sys.stderr)


def _error(exc: PulsarError) -> None:
    body: dict[str, Any] = {
        "error": exc.message,
        "code": exc.code,
        "retryable": exc.retryable,
        "detail": exc.detail,
    }
    sys.stderr.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def _emit(report: tuple[dict[str, Any], int]) -> int:
    out, code = report
    _out(out)
    return code


# -- auth -----------------------------------------------------------------------------


def _migrate_quietly(registry: AccountRegistry, settings: Settings) -> None:
    """First-use migration of a phase 1 home before a write; a problem there
    must not block a login or logout."""
    try:
        result = registry.migrate_legacy(settings)
    except PulsarError as exc:
        _notice(f"legacy credentials not migrated [{exc.code}]: {exc.message}")
        return
    if result.state == "migrated":
        _notice(f"migrated the legacy credentials to {result.alias}")


def _auth_login(args: argparse.Namespace, ctx: Context) -> int:
    paths = ctx.paths
    settings = load_settings(paths)
    alias = args.account or settings.default_account
    if not alias:
        raise UsageError("--account is required: the account to bind, e.g. --account x:<handle>")
    client_id = args.client_id or load_client_id(paths)
    if not client_id:
        raise UsageError("--client-id is required the first time: the X app's OAuth 2.0 client id")
    _migrate_quietly(AccountRegistry(paths), settings)
    account = login(paths, settings, alias, client_id, open_browser=not args.no_browser)
    # login() asked X with the new token before storing it, so this is live proof.
    return _emit(
        (
            {
                "alias": account.alias,
                "account": {"user_id": account.provider_user_id, "username": account.handle},
                "account_source": "live",
                "verified": True,
                "scope": " ".join(account.scopes) or None,
                "home": str(paths.home),
            },
            EXIT_OK,
        )
    )


def _auth_status(args: argparse.Namespace, ctx: Context) -> int:
    if args.offline:
        _notice("--offline is deprecated and has no effect: the default makes no network call")
    out, code = asyncio.run(
        auth_report(ctx.paths, account=args.account, live=args.live, transport=ctx.transport)
    )
    if not out["accounts"]:
        _notice("no account is bound; a human runs `pulsar auth login --account x:<handle>`")
    return _emit((out, code))


def _auth_logout(args: argparse.Namespace, ctx: Context) -> int:
    # Refuse before touching anything: not even the first-use migration runs
    # without --confirm (STD-01 §R5).
    if not args.confirm:
        raise UsageError(
            f"logging out {args.account or 'the default account'} deletes its tokens and "
            "needs a human to log in again; pass --confirm to proceed",
            detail={"account": args.account},
        )
    paths = ctx.paths
    settings = load_settings(paths)
    registry = AccountRegistry(paths)
    _migrate_quietly(registry, settings)
    account = registry.resolve(args.account, settings)
    had_tokens = registry.store(account.alias).exists()
    registry.logout(account.alias)
    return _emit(
        (
            {
                "alias": account.alias,
                "home": str(paths.home),
                "tokens_removed": had_tokens,
                "status": "revoked",
            },
            EXIT_OK,
        )
    )


def _auth_migrate(args: argparse.Namespace, ctx: Context) -> int:
    """Report what the move would do; ``--confirm`` makes it (STD-01 §R5)."""
    paths = ctx.paths
    registry, settings = AccountRegistry(paths), load_settings(paths)
    if args.confirm:
        result = registry.migrate_legacy(settings, args.account)
    else:
        result = registry.legacy_status(settings, args.account)
    out = {
        "applied": args.confirm,
        "state": result.state,
        "alias": result.alias,
        "adopted": list(result.adopted),
        "message": result.message
        or {
            "migrated": f"legacy credentials are now {result.alias}; run "
            f"`pulsar auth status --live --account {result.alias}` to prove the binding",
            "none": "no legacy credentials to migrate",
        }.get(result.state),
        "home": str(paths.home),
    }
    done = ("migrated", "none") if args.confirm else ("pending", "none")
    return _emit((out, EXIT_OK if result.state in done else EXIT_FAILED))


# -- operator verbs -------------------------------------------------------------------


def _status(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ops.budget_report(ctx.paths, account=args.account)
    if not out["accounts"]:
        _notice("no account is bound; nothing to report")
    return _emit((out, code))


def _history(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ops.history_report(ctx.paths, account=args.account, limit=args.limit)
    if not out["writes"]:
        _notice("no ledger rows" + (f" for {args.account}" if args.account else ""))
    elif out["truncated"]:
        _notice(f"showing {len(out['writes'])} of {out['total']} rows; raise --limit for more")
    return _emit((out, code))


def _validate(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(asyncio.run(ops.validate_report(ctx.paths, args.plan, account=args.account)))


def _publish(args: argparse.Namespace, ctx: Context) -> int:
    if args.yes:
        _notice("--yes is deprecated; use --confirm")
    return _emit(
        asyncio.run(
            ops.publish_report(
                ctx.paths,
                args.plan,
                account=args.account,
                idempotency_key=args.idempotency_key,
                caller=args.caller,
                confirm=args.confirm or args.yes,
                transport=ctx.transport,
            )
        )
    )


def _reconcile(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(
        asyncio.run(ops.reconcile_report(ctx.paths, account=args.account, transport=ctx.transport))
    )


def _import_posted(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(
        asyncio.run(
            ops.import_report(
                ctx.paths,
                args.source,
                account=args.account,
                confirm=args.confirm,
                transport=ctx.transport,
            )
        )
    )


def _migrate(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(ops.migrate_report(ctx.paths, confirm=args.confirm))


def _orbit_tool(_args: argparse.Namespace, _ctx: Context) -> int:
    from .orbit_tool import main as orbit_tool_main

    return orbit_tool_main()


def _serve(args: argparse.Namespace, ctx: Context) -> int:
    from .mcp import build_server, loopback_security
    from .runtime import Runtime

    if args.transport != "http" and (args.host is not None or args.port is not None):
        # stdio has no address: a --host or --port here would be silently unused (STD-01 §R28).
        raise UsageError(
            "--host and --port apply only to --transport http",
            detail={"transport": args.transport},
        )
    rt = Runtime(ctx.paths)
    roots = rt.settings.media_roots
    # stderr: stdout is the MCP stream under --transport stdio.
    _notice(
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


# -- parser ---------------------------------------------------------------------------


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= limit <= ops.HISTORY_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"must be 1..{ops.HISTORY_LIMIT_MAX}, got {limit}")
    return limit


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"must be 1..65535, got {port}")
    return port


ACCOUNT_HELP = "an alias, provider:handle such as x:<handle>"
ACCOUNT_DEFAULT = "default: default_account in config.toml, else the only bound account"


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree; the one declaration help and goldens come from."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine output (the default and only mode: every command prints JSON)",
    )

    def sub_parser(
        parent: Any, name: str, *, help: str, epilog: str | None = None
    ) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = parent.add_parser(
            name,
            help=help,
            description=help,
            epilog=epilog,
            parents=[common],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        return parser

    parser = JsonArgumentParser(
        prog="pulsar",
        description="Social write connector for the constellation: publish to X as accounts "
        "a human bound on this host, through one ledger and policy.",
        epilog="Output is JSON on stdout; errors are JSON on stderr. "
        "Exit codes: 0 ok, 1 failed or not settled, 2 usage error.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"pulsar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    auth = sub_parser(sub, "auth", help="manage the account authorizations on this host")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True, metavar="VERB")
    p_login = sub_parser(
        auth_sub,
        "login",
        help="bind an account: OAuth 2.0 PKCE in a browser, then verify the handle with X",
        epilog="Examples:\n  pulsar auth login --account x:<handle> --client-id <CLIENT_ID>\n"
        "  pulsar auth login --account x:<handle> --no-browser   # over SSH",
    )
    p_login.add_argument(
        "--account", help=f"the account to bind ({ACCOUNT_HELP}); {ACCOUNT_DEFAULT}"
    )
    p_login.add_argument(
        "--client-id",
        help="the X app's OAuth 2.0 client id; default: the one remembered from the last login",
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL on stderr instead of opening a browser",
    )
    p_login.set_defaults(func=_auth_login)

    p_status = sub_parser(
        auth_sub,
        "status",
        help="each bound account and its health, from local state (no network)",
        epilog="Exit 0 only when every reported account is healthy.",
    )
    p_status.add_argument(
        "--account", help=f"report only this account ({ACCOUNT_HELP}); default: all"
    )
    status_mode = p_status.add_mutually_exclusive_group()
    status_mode.add_argument(
        "--live",
        action="store_true",
        help="prove each binding: force a token refresh (rotates the pair), then GET /users/me",
    )
    status_mode.add_argument(
        "--offline", action="store_true", help="deprecated, no effect: the default is offline"
    )
    p_status.set_defaults(func=_auth_status)

    p_logout = sub_parser(
        auth_sub,
        "logout",
        help="delete an account's tokens (the account is kept as revoked); needs --confirm",
    )
    p_logout.add_argument("--account", help=f"the account to log out; {ACCOUNT_DEFAULT}")
    p_logout.add_argument("--confirm", action="store_true", help="actually delete the tokens")
    p_logout.set_defaults(func=_auth_logout)

    p_migrate = sub_parser(
        auth_sub,
        "migrate",
        help="move single-account (phase 1) credentials into the account layout; "
        "reports only without --confirm",
    )
    p_migrate.add_argument(
        "--account",
        help="the alias they belong to, x:<handle>; default: default_account, else the "
        "handle whoami.json recorded",
    )
    p_migrate.add_argument("--confirm", action="store_true", help="actually move them")
    p_migrate.set_defaults(func=_auth_migrate)

    p_st = sub_parser(
        sub, "status", help="budgets, today's posts, quiet hours and unresolved writes (offline)"
    )
    p_st.add_argument("--account", help=f"report only this account ({ACCOUNT_HELP}); default: all")
    p_st.set_defaults(func=_status)

    p_hist = sub_parser(sub, "history", help="the newest ledger rows (offline)")
    p_hist.add_argument(
        "--account", help=f"only this account's rows ({ACCOUNT_HELP}); default: all"
    )
    p_hist.add_argument(
        "--limit",
        type=_limit,
        default=ops.HISTORY_LIMIT_DEFAULT,
        help=f"rows to show, 1..{ops.HISTORY_LIMIT_MAX}; default: {ops.HISTORY_LIMIT_DEFAULT}",
    )
    p_hist.set_defaults(func=_history)

    p_val = sub_parser(
        sub,
        "validate",
        help="check a plan offline: per account, what would post, its digest and cost",
    )
    p_val.add_argument("plan", type=Path, help="plan file (YAML)")
    p_val.add_argument(
        "--account",
        help="one of the plan's accounts, or the one to bind a plan that names none; "
        + ACCOUNT_DEFAULT,
    )
    p_val.set_defaults(func=_validate)

    p_pub = sub_parser(
        sub,
        "publish",
        help="publish a plan through the ledger and policy; validates only without --confirm",
        epilog="Examples:\n"
        "  pulsar publish plan.yaml            # what would post, and its cost\n"
        "  pulsar publish plan.yaml --confirm  # post it (re-running replays the receipt)",
    )
    p_pub.add_argument("plan", type=Path, help="plan file (YAML)")
    p_pub.add_argument(
        "--account",
        help="one of the plan's accounts, or the one to bind a plan that names none; "
        + ACCOUNT_DEFAULT,
    )
    p_pub.add_argument(
        "--idempotency-key",
        help="the write's key: re-running with it replays the receipt instead of posting again; "
        "default: derived from the plan's digest and the account",
    )
    p_pub.add_argument(
        "--caller",
        help=f"audit label recorded in the ledger; default: $PULSAR_CALLER, else {ops.CLI_CALLER}",
    )
    p_pub.add_argument("--confirm", action="store_true", help="actually publish (costs money)")
    p_pub.add_argument("--yes", action="store_true", help="deprecated alias of --confirm")
    p_pub.set_defaults(func=_publish)

    p_rec = sub_parser(
        sub,
        "reconcile",
        help="settle writes whose outcome is unknown from the account's timeline "
        "(reads X only when something is unresolved)",
        epilog="Exit 0 only when every row it looked at is settled.",
    )
    p_rec.add_argument("--account", help=f"the account to reconcile; {ACCOUNT_DEFAULT}")
    p_rec.set_defaults(func=_reconcile)

    p_imp = sub_parser(
        sub,
        "import-posted",
        help="import a retired routine's posted.jsonl into the ledger (idempotent); "
        "reports only without --confirm",
    )
    p_imp.add_argument("source", type=Path, help="path to posted.jsonl")
    p_imp.add_argument("--account", help=f"the account those posts were made as; {ACCOUNT_DEFAULT}")
    p_imp.add_argument("--confirm", action="store_true", help="actually write the rows")
    p_imp.set_defaults(func=_import_posted)

    p_mig = sub_parser(
        sub,
        "migrate",
        help="upgrade the home in place: the ledger schema, then phase 1 credentials; "
        "reports only without --confirm",
        epilog="An upgraded ledger is refused by older pulsar versions; moved credentials "
        "stay moved.",
    )
    p_mig.add_argument("--confirm", action="store_true", help="actually upgrade the home")
    p_mig.set_defaults(func=_migrate)

    serve = sub_parser(sub, "serve", help="run the MCP server")
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

    p_orbit = sub_parser(
        sub,
        "orbit-tool",
        help="answer one Orbit plugin request (envelope on stdin, response on stdout)",
    )
    p_orbit.set_defaults(func=_orbit_tool)
    return parser


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    handler: Handler = args.func
    try:
        return handler(args, Context(default_paths(), transport))
    except UsageError as exc:
        _error(exc)
        return EXIT_USAGE
    except PulsarError as exc:
        _error(exc)
        return EXIT_FAILED
    except BrokenPipeError:
        # `pulsar history | head -1`: the reader left; that is not a failure.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except Exception as exc:
        log.exception("pulsar %s: unexpected error", args.command)
        _error(PulsarError(INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False))
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
