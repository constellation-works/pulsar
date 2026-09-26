"""``pulsar`` command line: human auth management, operator verbs (``ops.py``)
and the MCP server.

Output contract (STD-01): every command builds one payload and prints it on
stdout in the mode ``output.resolve_mode`` picks: a table or key-value view
on a terminal, tab-separated lines when piped, or the payload as one JSON
document (``--json``, ``--format json``, ``PULSAR_FORMAT=json``). A failure
prints nothing on stdout and ``error: <message>`` on stderr, or in JSON mode
one object ``{error, code, retryable, detail}``. Exit codes: 0 success, 1 the
command failed (or reported something not settled or not healthy), 2 a
usage error. Notices (empty results, deprecations, notes) go to stderr. A
closed stdout ends the process quietly with 0. ``pulsar`` or ``pulsar auth``
alone prints that level's help on stderr and exits 2.
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
from . import ops, views
from .health import attention, auth_report
from .output import (
    FORMAT_ENV,
    FORMATS,
    Mode,
    ModeConflict,
    Terminal,
    View,
    json_requested,
    render,
    resolve_mode,
    resolve_terminal,
)
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


class _ParseError(Exception):
    """A usage error argparse found; ``main`` prints it in the requested mode."""

    def __init__(self, message: str, usage: str, prog: str) -> None:
        super().__init__(message)
        self.message, self.usage, self.prog = message, usage, prog


class CommandParser(argparse.ArgumentParser):
    """argparse that leaves printing a usage error to ``main`` (STD-01 §R19),
    and lists its subcommands in titled groups the way ``orbit`` does. The
    group listing is built from each subcommand's own declaration (§R25).
    Subparsers inherit the class."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.command_groups: dict[str, list[str]] = {}

    def error(self, message: str) -> NoReturn:
        raise _ParseError(message, self.format_usage().strip(), self.prog)

    def format_help(self) -> str:
        if not self.command_groups:
            return super().format_help()
        listed: dict[str, argparse.Action] = {}
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                pseudo = action._get_subactions()  # pyright: ignore[reportPrivateUsage]
                listed = {str(a.metavar): a for a in pseudo}
        fmt = self._get_formatter()
        fmt.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        fmt.add_text(self.description)
        for title, names in self.command_groups.items():
            fmt.start_section(title)
            fmt.add_arguments([listed[name] for name in names])
            fmt.end_section()
        fmt.start_section("Options")
        fmt.add_arguments([a for a in self._actions if a.option_strings])
        fmt.end_section()
        fmt.add_text(self.epilog)
        return fmt.format_help()


class Context:
    """What a command needs besides its arguments: the home, resolved once per
    invocation, the output mode and terminal, and the transport (tests pass a
    fake one)."""

    def __init__(
        self,
        paths: Paths,
        transport: httpx.AsyncBaseTransport | None,
        mode: Mode = "json",
        term: Terminal | None = None,
    ) -> None:
        self.paths = paths
        self.transport = transport
        self.mode: Mode = mode
        self.term = term or Terminal()


# -- output ---------------------------------------------------------------------------


def _out(payload: dict[str, Any], ctx: Context, view: View) -> None:
    if ctx.mode == "json":
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        note = payload.get(views.NOTE)
        if isinstance(note, str) and note:
            _notice(note)
        text = render(view(payload), ctx.mode, ctx.term)
    sys.stdout.write(text)
    sys.stdout.flush()


def _notice(message: str) -> None:
    print(f"pulsar: {message}", file=sys.stderr)


def _error(exc: PulsarError, json_mode: bool) -> None:
    if json_mode:
        body: dict[str, Any] = {
            "error": exc.message,
            "code": exc.code,
            "retryable": exc.retryable,
            "detail": exc.detail,
        }
        line = json.dumps(body, ensure_ascii=False)
    else:
        line = f"error: {exc.message}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _usage_error(exc: _ParseError, json_mode: bool) -> None:
    if json_mode:
        body = {
            "error": f"{exc.prog}: {exc.message}",
            "code": INVALID_ARGUMENT,
            "retryable": False,
            "detail": {"usage": exc.usage},
        }
        text = json.dumps(body, ensure_ascii=False) + "\n"
    else:
        hint = f"For more information, try '{exc.prog} --help'."
        text = f"error: {exc.message}\n\n{exc.usage}\n\n{hint}\n"
    sys.stderr.write(text)
    sys.stderr.flush()


def _emit(report: tuple[dict[str, Any], int], ctx: Context, view: View = views.detail) -> int:
    out, code = report
    _out(out, ctx, view)
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
        ),
        ctx,
    )


def _auth_status(args: argparse.Namespace, ctx: Context) -> int:
    if args.offline:
        _notice("--offline is deprecated and has no effect: the default makes no network call")
    out, code = asyncio.run(
        auth_report(ctx.paths, account=args.account, live=args.live, transport=ctx.transport)
    )
    if not out["accounts"]:
        _notice("no account is bound; a human runs `pulsar auth login --account x:<handle>`")
    if out["legacy"] is not None and out["legacy"]["message"]:
        _notice(out["legacy"]["message"])
    for entry in out["accounts"]:
        remedy = attention(entry, ctx.paths.home)
        if remedy is not None:
            _notice(remedy)
    return _emit((out, code), ctx, views.auth_status)


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
        ),
        ctx,
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
    return _emit((out, EXIT_OK if result.state in done else EXIT_FAILED), ctx)


# -- operator verbs -------------------------------------------------------------------


def _status(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ops.budget_report(ctx.paths, account=args.account)
    if not out["accounts"]:
        _notice("no account is bound; nothing to report")
    return _emit((out, code), ctx, views.status)


def _history(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ops.history_report(ctx.paths, account=args.account, limit=args.limit)
    if not out["writes"]:
        _notice("no ledger rows" + (f" for {args.account}" if args.account else ""))
    elif out["truncated"]:
        _notice(f"showing {len(out['writes'])} of {out['total']} rows; raise --limit for more")
    return _emit((out, code), ctx, views.history)


def _validate(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(
        asyncio.run(ops.validate_report(ctx.paths, args.plan, account=args.account)),
        ctx,
        views.plan_report,
    )


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
        ),
        ctx,
        views.publish,
    )


def _reconcile(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(
        asyncio.run(ops.reconcile_report(ctx.paths, account=args.account, transport=ctx.transport)),
        ctx,
        views.reconcile,
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
        ),
        ctx,
        views.import_posted,
    )


def _migrate(args: argparse.Namespace, ctx: Context) -> int:
    return _emit(ops.migrate_report(ctx.paths, confirm=args.confirm), ctx)


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
    # Global flags (STD-01 §R4): declared once, accepted before and after the
    # subcommand. SUPPRESS keeps a subcommand from resetting the root's value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--format",
        choices=FORMATS,
        default=argparse.SUPPRESS,
        metavar="MODE",
        help="output: auto (a table on a terminal, tab-separated lines when piped), table or "
        f"json; default: ${FORMAT_ENV}, else auto",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="shorthand for --format json",
    )

    def sub_parser(
        parent: Any,
        name: str,
        *,
        help: str,
        epilog: str | None = None,
        group: tuple[CommandParser, str] | None = None,
    ) -> CommandParser:
        parser: CommandParser = parent.add_parser(
            name,
            help=help,
            description=help,
            epilog=epilog,
            parents=[common],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        if group is not None:
            owner, title = group
            owner.command_groups.setdefault(title, []).append(name)
        return parser

    parser = CommandParser(
        prog="pulsar",
        usage="%(prog)s [OPTIONS] COMMAND ...",
        description="Social write connector for the constellation: publish to X as accounts\n"
        "a human bound on this host, through one ledger and policy.",
        epilog="Exit codes: 0 ok, 1 failed or not settled, 2 usage error.\n"
        "Run `pulsar COMMAND --help` for a command's own options and examples.",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"pulsar {__version__}")
    parser.set_defaults(func=None, help_parser=parser)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", prog="pulsar")
    root = parser
    # The root help's sections, in this order; each command names its own.
    for title in ("Accounts", "Publish", "Observe", "Maintenance", "Services"):
        root.command_groups[title] = []

    auth = sub_parser(
        sub,
        "auth",
        help="bind, check and log out the accounts on this host",
        group=(root, "Accounts"),
        epilog="Examples:\n  pulsar auth login --account x:<handle> --no-browser\n"
        "  pulsar auth status --live",
    )
    auth.usage = "pulsar auth [OPTIONS] COMMAND ..."
    auth.set_defaults(func=None, help_parser=auth)
    auth_sub = auth.add_subparsers(dest="auth_command", metavar="COMMAND", prog="pulsar auth")
    p_login = sub_parser(
        auth_sub,
        "login",
        group=(auth, "Commands"),
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
        group=(auth, "Commands"),
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
        group=(auth, "Commands"),
        help="delete an account's tokens (the account is kept as revoked); needs --confirm",
    )
    p_logout.add_argument("--account", help=f"the account to log out; {ACCOUNT_DEFAULT}")
    p_logout.add_argument("--confirm", action="store_true", help="actually delete the tokens")
    p_logout.set_defaults(func=_auth_logout)

    p_migrate = sub_parser(
        auth_sub,
        "migrate",
        group=(auth, "Commands"),
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
        sub,
        "status",
        help="budgets, today's posts, quiet hours and unresolved writes (offline)",
        group=(root, "Observe"),
    )
    p_st.add_argument("--account", help=f"report only this account ({ACCOUNT_HELP}); default: all")
    p_st.set_defaults(func=_status)

    p_hist = sub_parser(
        sub, "history", help="the newest ledger rows (offline)", group=(root, "Observe")
    )
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
        group=(root, "Publish"),
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
        group=(root, "Publish"),
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
        group=(root, "Publish"),
        help="settle writes whose outcome is unknown from the account's timeline "
        "(reads X only when something is unresolved)",
        epilog="Exit 0 only when every row it looked at is settled.",
    )
    p_rec.add_argument("--account", help=f"the account to reconcile; {ACCOUNT_DEFAULT}")
    p_rec.set_defaults(func=_reconcile)

    p_imp = sub_parser(
        sub,
        "import-posted",
        group=(root, "Maintenance"),
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
        group=(root, "Maintenance"),
        help="upgrade the home in place: the ledger schema, then phase 1 credentials; "
        "reports only without --confirm",
        epilog="An upgraded ledger is refused by older pulsar versions; moved credentials "
        "stay moved.",
    )
    p_mig.add_argument("--confirm", action="store_true", help="actually upgrade the home")
    p_mig.set_defaults(func=_migrate)

    serve = sub_parser(sub, "serve", help="run the MCP server", group=(root, "Services"))
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
        group=(root, "Services"),
        help="answer one Orbit plugin request (envelope on stdin, response on stdout)",
    )
    p_orbit.set_defaults(func=_orbit_tool)
    return parser


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = json_requested(raw, os.environ)
    try:
        args = build_parser().parse_args(raw)
    except _ParseError as exc:
        _usage_error(exc, wants_json)
        raise SystemExit(EXIT_USAGE) from None
    handler: Handler | None = args.func
    if handler is None:
        # `pulsar` or `pulsar auth` alone: that level's help, as a usage error.
        args.help_parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        mode = resolve_mode(
            getattr(args, "format", None),
            getattr(args, "json", False),
            os.environ,
            sys.stdout.isatty(),
        )
    except ModeConflict as exc:
        _error(UsageError(str(exc)), wants_json)
        return EXIT_USAGE
    configure_logging()
    ctx = Context(default_paths(), transport, mode, resolve_terminal(sys.stdout, os.environ))
    json_mode = mode == "json"
    try:
        return handler(args, ctx)
    except UsageError as exc:
        _error(exc, json_mode)
        return EXIT_USAGE
    except PulsarError as exc:
        _error(exc, json_mode)
        return EXIT_FAILED
    except BrokenPipeError:
        # `pulsar history | head -1`: the reader left; that is not a failure.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except Exception as exc:
        log.exception("pulsar %s: unexpected error", args.command)
        bug = PulsarError(INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False)
        _error(bug, json_mode)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
