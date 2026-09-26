"""Orbit exec backend: one process per ``pulsar.<verb>`` call.

Orbit writes one request envelope to stdin::

    {"schema_version": 1, "tool": "pulsar.status", "input": {...},
     "context": {"workspace_root": ..., "agent": ..., "model": ..., "config": {...}}}

and reads exactly one JSON object from stdout: ``{"ok": true, "output": ...}``
or ``{"ok": false, "error": {"code", "message", "retryable", "detail"?}}``.
Nothing else may reach stdout; diagnostics go to stderr.

The pulsar home is ``$ORBIT_PLUGIN_STATE/home``: the plugin sandbox can
write only ``{{plugin_state}}``, and the CLI and standalone MCP server reach
the same home (so the same ledger) with ``PULSAR_HOME``. Outside Orbit (no
``ORBIT_PLUGIN_STATE``) the usual ``PULSAR_HOME`` / ``~/.config/pulsar``
applies, which is what ``pulsar orbit-tool`` uses for local debugging.

Settings come from the home's ``config.toml``, as for every other surface:
policy is enforced against one ledger, so it must have one source. The
``[plugins.pulsar]`` section is closed (its schema allows no keys).

Paths in a call (a plan ``source``, plan media) are relative to the
workspace root, and media must resolve inside it: the sandbox grants the
workspace read-only and nothing else outside the plugin's state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

from ..core.errors import API_ERROR, INVALID_ARGUMENT, PulsarError
from ..core.jsonx import as_object
from ..core.ledger import PUBLISHED, PlanRecord
from ..core.paths import Paths, pulsar_home
from ..core.plan import Plan
from ..core.settings import Settings, load_settings
from .mcp import Runtime

log = logging.getLogger(__name__)

NAMESPACE = "pulsar"
ENVELOPE_VERSION = 1
HISTORY_LIMIT_MAX = 100
# A plan source is small YAML; refuse anything that is plainly not one.
SOURCE_MAX_BYTES = 256 * 1024

Output = dict[str, Any]


class Call:
    """One request: its input, where it runs, and the home it acts on."""

    def __init__(self, tool: str, input_: Mapping[str, Any], context: Mapping[str, Any]) -> None:
        self.tool = tool
        self.input = input_
        self.context = context
        root = context.get("workspace_root")
        self.workspace = Path(root).resolve() if isinstance(root, str) and root else None

    def string(self, name: str) -> str | None:
        value = self.input.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise PulsarError(INVALID_ARGUMENT, f"`{name}` must be a non-empty string")
        return value.strip()


def plugin_paths(environ: Mapping[str, str]) -> Paths:
    state = environ.get("ORBIT_PLUGIN_STATE")
    if state:
        return Paths(home=Path(state) / "home")
    return Paths(home=pulsar_home())


def _settings(paths: Paths, call: Call) -> Settings:
    settings = load_settings(paths)
    # The sandbox can read only the workspace; configured roots elsewhere
    # would be unreadable, so plugin calls confine media to the workspace.
    roots = (call.workspace,) if call.workspace is not None else ()
    return replace(settings, media_roots=roots)


# -- tools ----------------------------------------------------------------------------


async def status(paths: Paths, call: Call) -> Output:
    """Accounts, token health, budget use, unresolved writes, last publication. Offline."""
    from .cli import status_report
    from .ops import budget_report

    account = call.string("account")
    auth, _ = await status_report(paths, account=account, offline=True)
    if "error" in auth:
        raise _from_result(auth["error"])
    budget, code = budget_report(paths, account=account)
    if code != 0:
        raise _from_result(budget)
    usage = {entry["alias"]: entry for entry in budget["accounts"]}
    rt = Runtime(paths)
    try:
        accounts: list[dict[str, Any]] = []
        attention: list[str] = []
        for entry in auth["accounts"]:
            alias = entry["alias"]
            spent = usage.get(alias, {})
            row = {
                "alias": alias,
                "status": entry["status"],
                "expected_handle": entry["expected_handle"],
                "authorized": entry["authorized"],
                "reauth_required": entry["reauth_required"],
                "token_state": entry["token_state"],
                "access_token_expires_in_s": entry["access_token_expires_in_s"],
                "healthy": entry["healthy"],
                "posts": spent.get("posts"),
                "day": spent.get("day"),
                "month": spent.get("month"),
                "quiet": spent.get("quiet"),
                "unresolved": spent.get("unresolved", []),
                "last_published": _last_published(rt, alias),
            }
            accounts.append(row)
            if entry["reauth_required"] or not entry["authorized"]:
                attention.append(f"{alias}: re-authorization required (`pulsar auth login`)")
            elif not entry["healthy"]:
                attention.append(f"{alias}: {entry['status']}")
            if row["unresolved"]:
                attention.append(
                    f"{alias}: {len(row['unresolved'])} write(s) with an unknown outcome "
                    "(`pulsar reconcile`)"
                )
        if not accounts:
            attention.append("no account is bound (`pulsar auth login --account x:<handle>`)")
    finally:
        await rt.aclose()
    return {
        "default_account": auth["default_account"],
        "healthy": not attention,
        "attention": attention,
        "accounts": accounts,
    }


def _last_published(rt: Runtime, alias: str) -> dict[str, Any] | None:
    for record in rt.ledger.history(limit=50, account_alias=alias):
        if record.state == PUBLISHED:
            return {"key": record.key, "url": record.url, "at": record.updated_at}
    return None


async def validate(paths: Paths, call: Call) -> Output:
    """What publishing a plan would send, per account. Offline; claims and sends nothing.

    A plan that fails validation is a result (``valid: false``), not a tool
    error: an agent drafting a post needs the code and detail to fix it.
    """
    raw_plan = call.input.get("plan")
    source = call.string("source")
    if (raw_plan is None) == (source is None):
        raise PulsarError(INVALID_ARGUMENT, "pass exactly one of `plan` or `source`")
    plan_input = as_object(raw_plan)
    if raw_plan is not None and plan_input is None:
        raise PulsarError(INVALID_ARGUMENT, "`plan` must be an object")
    # An unreadable source is a bad argument (a tool error); what it says is the plan.
    text = _read_source(_source_path(call, source), source) if source is not None else None
    account = call.string("account")
    rt = Runtime(paths, settings=_settings(paths, call))
    try:
        if text is not None:
            plan = Plan.from_yaml(text)
        else:
            plan = Plan.from_mapping(plan_input or {})
        with _in_workspace(call):
            plan, targets = rt.plan_targets(plan, account)
            reports = [rt.publisher.prepare(plan, rt.offline_bound(a)).report() for a in targets]
    except PulsarError as exc:
        return {"valid": False, "error": exc.to_envelope()["error"]}
    finally:
        await rt.aclose()
    return {"valid": True, "accounts": reports}


def _source_path(call: Call, source: str) -> Path:
    if call.workspace is None:
        raise PulsarError(INVALID_ARGUMENT, "`source` needs a workspace")
    path = (call.workspace / source).resolve()
    if not path.is_relative_to(call.workspace):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"`source` must be inside the workspace ({call.workspace})",
            detail={"source": source},
        )
    return path


def _read_source(path: Path, source: str) -> str:
    try:
        with path.open("rb") as fh:
            data = fh.read(SOURCE_MAX_BYTES + 1)
    except OSError as exc:
        raise PulsarError(
            INVALID_ARGUMENT, f"cannot read {source}: {exc.strerror}", detail={"source": source}
        ) from exc
    if len(data) > SOURCE_MAX_BYTES:
        raise PulsarError(INVALID_ARGUMENT, f"{source} is over {SOURCE_MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PulsarError(INVALID_ARGUMENT, f"{source} is not UTF-8") from exc


class _in_workspace:
    """Resolve relative media paths against the workspace root for the call."""

    def __init__(self, call: Call) -> None:
        self.target = call.workspace
        self.previous: str | None = None

    def __enter__(self) -> None:
        if self.target is not None:
            self.previous = os.getcwd()
            os.chdir(self.target)

    def __exit__(self, *_: object) -> None:
        if self.previous is not None:
            os.chdir(self.previous)


async def history(paths: Paths, call: Call) -> Output:
    """The newest ledger rows, flattened for a table. Offline."""
    account = call.string("account")
    limit = call.input.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= HISTORY_LIMIT_MAX:
        raise PulsarError(INVALID_ARGUMENT, f"`limit` must be an integer 1..{HISTORY_LIMIT_MAX}")
    rt = Runtime(paths)
    try:
        alias = rt.account(account).alias if account is not None else None
        rows = [_row(r) for r in rt.ledger.history(limit=limit, account_alias=alias)]
    finally:
        await rt.aclose()
    return {"rows": rows}


def _row(record: PlanRecord) -> dict[str, Any]:
    return {
        "key": record.key,
        "tool": record.tool,
        "account": record.account_alias,
        "state": record.state,
        "posts": len(record.items),
        "published": sum(1 for i in record.items if i.state == PUBLISHED),
        "url": record.url,
        "cost_usd": round(sum(i.est_cost_usd for i in record.items), 6),
        "error_code": record.error_code,
        "caller": record.caller,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


Handler = Callable[[Paths, Call], Coroutine[Any, Any, Output]]

TOOLS: dict[str, Handler] = {"status": status, "validate": validate, "history": history}


# -- protocol ---------------------------------------------------------------------------


def _from_result(result: Mapping[str, Any]) -> PulsarError:
    """A ``to_result`` dict (the ops/CLI reports) back as the error it was."""
    return PulsarError(
        str(result.get("code", API_ERROR)),
        str(result.get("message", "")),
        detail=result.get("detail"),
        retryable=bool(result.get("retryable", False)),
    )


def _failure(exc: PulsarError) -> dict[str, Any]:
    return exc.to_envelope()


def handle(envelope: object, environ: Mapping[str, str]) -> dict[str, Any]:
    """One envelope in, one response out. Never raises."""
    try:
        request = _parse(envelope, environ)
        handler = TOOLS[request.tool]
        output = asyncio.run(handler(plugin_paths(environ), request))
    except PulsarError as exc:
        return _failure(exc)
    except Exception as exc:
        log.exception("pulsar orbit-tool: unexpected error")
        return _failure(
            PulsarError(API_ERROR, f"unexpected {exc.__class__.__name__}", retryable=False)
        )
    return {"ok": True, "output": output}


def _parse(envelope: object, environ: Mapping[str, str]) -> Call:
    request = as_object(envelope)
    if request is None:
        raise PulsarError(INVALID_ARGUMENT, "the request envelope is not a JSON object")
    version = request.get("schema_version")
    if version != ENVELOPE_VERSION:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"unsupported envelope schema_version {version!r}; this backend speaks "
            f"{ENVELOPE_VERSION}",
        )
    name = request.get("tool") or environ.get("ORBIT_TOOL_NAME")
    if not isinstance(name, str):
        raise PulsarError(INVALID_ARGUMENT, "the request names no tool")
    verb = name.removeprefix(f"{NAMESPACE}.")
    if verb not in TOOLS:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"unknown tool {name!r}; this backend serves "
            + ", ".join(f"{NAMESPACE}.{t}" for t in TOOLS),
        )
    input_ = as_object({} if request.get("input") is None else request.get("input"))
    if input_ is None:
        raise PulsarError(INVALID_ARGUMENT, "`input` must be a JSON object")
    context = as_object({} if request.get("context") is None else request.get("context"))
    if context is None:
        raise PulsarError(INVALID_ARGUMENT, "`context` must be a JSON object")
    if context.get("config") not in (None, {}):
        raise PulsarError(
            INVALID_ARGUMENT,
            "[plugins.pulsar] takes no keys: pulsar reads its settings from config.toml in its "
            "home, the one source every surface shares",
        )
    return Call(verb, input_, context)


def main(
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read one envelope, write one response. The exit code is 0 whenever a
    response was written: the envelope's ``ok`` carries the outcome."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    environ = os.environ if environ is None else environ
    raw = source.read()
    try:
        envelope: object = json.loads(raw)
    except ValueError:
        response = _failure(PulsarError(INVALID_ARGUMENT, "the request envelope is not JSON"))
    else:
        response = handle(envelope, environ)
    sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sink.flush()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    raise SystemExit(main())
