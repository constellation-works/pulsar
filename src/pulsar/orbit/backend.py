"""Orbit exec backend: one process per ``pulsar.<verb>`` call.

Orbit writes one request envelope to stdin::

    {"schema_version": 1, "tool": "pulsar.status", "input": {...},
     "context": {"workspace_root": ..., "agent": ..., "model": ..., "config": {...}}}

and reads exactly one JSON object from stdout: ``{"ok": true, "output": ...}``
or ``{"ok": false, "error": {"code", "message", "retryable", "detail"?}}``.
Nothing else may reach stdout; diagnostics go to stderr.

Orbit runs it as ``pulsar orbit-tool`` (``bin/pulsar``), so the entry point
(``pulsar.main``) builds the ``App`` it answers with. Its home is
``$ORBIT_PLUGIN_STATE/home`` (``pulsar.app.default_paths``): the plugin
sandbox can write only ``{{plugin_state}}``, and the CLI and standalone MCP
server reach the same home (so the same ledger) with ``PULSAR_HOME``. Outside
Orbit (no ``ORBIT_PLUGIN_STATE``) the usual ``PULSAR_HOME`` /
``~/.config/pulsar`` applies, which is what ``pulsar orbit-tool`` uses for
local debugging.

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
import sys
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

from pulsar.app import plugin
from pulsar.app.interfaces import App
from pulsar.app.ops import HISTORY_LIMIT_DEFAULT, HISTORY_LIMIT_MAX
from pulsar.app.runtime import PLUGIN_STATE_ENV
from pulsar.app.settings import Settings, load_settings
from pulsar.internal.errors import INTERNAL, INVALID_ARGUMENT, INVALID_CONFIG, PulsarError
from pulsar.internal.fs import as_object, resolve_home

log = logging.getLogger(__name__)

NAMESPACE = "pulsar"
ENVELOPE_VERSION = 1

# Each tool's input keys: the request schemas' properties (a test holds them
# equal). Anything else is refused, not ignored.
INPUTS: dict[str, frozenset[str]] = {
    "status": frozenset({"account"}),
    "validate": frozenset({"plan", "source", "account"}),
    "history": frozenset({"account", "limit"}),
    "engagements": frozenset({"account", "hours", "limit"}),
    "metrics": frozenset({"account", "days", "limit"}),
    "publish": frozenset({"source", "account", "dry_run"}),
}

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

    def integer(self, name: str, limits: tuple[int, int, int]) -> int:
        """An integer input within ``limits`` (min, default, max); the default when absent."""
        low, default, high = limits
        value = self.input.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise PulsarError(INVALID_ARGUMENT, f"`{name}` must be an integer {low}..{high}")
        return plugin.bounded(name, value, limits)

    def flag(self, name: str) -> bool:
        value = self.input.get(name, False)
        if not isinstance(value, bool):
            raise PulsarError(INVALID_ARGUMENT, f"`{name}` must be true or false")
        return value

    @property
    def caller(self) -> str:
        """The audit label for what this call writes: the Orbit task it serves,
        else the agent. Self-asserted by the host, recorded, never authority."""
        task = self.context.get("task_id")
        agent = self.context.get("agent")
        if isinstance(task, str) and task:
            return f"orbit:{task}"
        return f"orbit:{agent}" if isinstance(agent, str) and agent else "orbit"


def check_plugin_home(app: App) -> None:
    """Under Orbit, refuse a ``PULSAR_HOME`` that names another home than the plugin's.

    The sandbox can write only the plugin state, so that home cannot be
    honoured; it is refused before any work rather than ignored, which would
    give this call a different ledger from the CLI's and defeat its duplicate
    guard.
    """
    other = app.environ.get("PULSAR_HOME")
    if not app.environ.get(PLUGIN_STATE_ENV) or not other:
        return
    user_home = app.paths.user_home
    named = resolve_home(app.environ, user_home) if user_home is not None else Path(other)
    if named != app.paths.home:
        raise PulsarError(
            INVALID_CONFIG,
            f"PULSAR_HOME={other} names a different home from this plugin's ({app.home}); under "
            "Orbit pulsar can use only its plugin state. Unset PULSAR_HOME for the Orbit "
            f"host, or set it to {app.home}",
            detail={"pulsar_home": other, "plugin_home": str(app.home)},
        )


def _settings(app: App, call: Call) -> Settings:
    settings = load_settings(app.paths)
    # The sandbox can read only the workspace; configured roots elsewhere
    # would be unreadable, so plugin calls confine media to the workspace.
    roots = (call.workspace,) if call.workspace is not None else ()
    return replace(settings, media_roots=roots)


# -- tools ----------------------------------------------------------------------------


async def status(app: App, call: Call) -> Output:
    """Accounts, token health, budget use, unresolved writes, last publication. Offline."""
    account = call.string("account")
    async with app.runtime(read_only=True) as rt:
        return await plugin.status(rt, account=account)


async def validate(app: App, call: Call) -> Output:
    """What publishing a plan would send, per account. Offline; claims and sends nothing."""
    raw_plan = call.input.get("plan")
    source = call.string("source")
    if (raw_plan is None) == (source is None):
        raise PulsarError(INVALID_ARGUMENT, "pass exactly one of `plan` or `source`")
    plan_input = as_object(raw_plan)
    if raw_plan is not None and plan_input is None:
        raise PulsarError(INVALID_ARGUMENT, "`plan` must be an object")
    text = None
    if source is not None:
        if call.workspace is None:
            raise PulsarError(INVALID_ARGUMENT, "`source` needs a workspace")
        text = plugin.read_source(call.workspace, source)
    account = call.string("account")
    # Relative media paths start at the workspace, never at this process's cwd.
    rt = app.runtime(read_only=True, settings=_settings(app, call), media_base=call.workspace)
    async with rt:
        approvable = (call.workspace, source) if source is not None and call.workspace else None
        return plugin.validate(
            rt, plan=plan_input, source_text=text, account=account, approvable=approvable
        )


async def history(app: App, call: Call) -> Output:
    """The newest ledger rows, flattened for a table, and how many there are. Offline."""
    account = call.string("account")
    limit = call.input.get("limit", HISTORY_LIMIT_DEFAULT)
    if not isinstance(limit, int):
        raise PulsarError(INVALID_ARGUMENT, f"`limit` must be an integer 1..{HISTORY_LIMIT_MAX}")
    async with app.runtime(read_only=True) as rt:
        return plugin.history(rt, account=account, limit=limit)


async def engagements(app: App, call: Call) -> Output:
    """Others' posts mentioning the account. A paid read, budgeted and recorded."""
    account = call.string("account")
    hours = call.integer("hours", plugin.MENTION_HOURS)
    limit = call.integer("limit", plugin.READ_POSTS)
    async with app.runtime() as rt:
        return await plugin.engagements(
            rt, account=account, hours=hours, limit=limit, caller=call.caller
        )


async def metrics(app: App, call: Call) -> Output:
    """The account's own recent posts with their metrics. A paid read."""
    account = call.string("account")
    days = call.integer("days", plugin.METRIC_DAYS)
    limit = call.integer("limit", plugin.READ_POSTS)
    async with app.runtime() as rt:
        return await plugin.metrics(rt, account=account, days=days, limit=limit, caller=call.caller)


async def publish(app: App, call: Call) -> Output:
    """Publish a workspace plan under a human approval of its digest."""
    source = call.string("source")
    if source is None:
        raise PulsarError(INVALID_ARGUMENT, "`source` names the plan file to publish")
    if call.workspace is None:
        raise PulsarError(INVALID_ARGUMENT, "`source` needs a workspace")
    account = call.string("account")
    dry_run = call.flag("dry_run")
    rt = app.runtime(read_only=dry_run, settings=_settings(app, call), media_base=call.workspace)
    async with rt:
        return await plugin.publish(
            rt,
            workspace=call.workspace,
            source=source,
            account=account,
            dry_run=dry_run,
            caller=call.caller,
        )


Handler = Callable[[App, Call], Coroutine[Any, Any, Output]]

TOOLS: dict[str, Handler] = {
    "status": status,
    "validate": validate,
    "history": history,
    "engagements": engagements,
    "metrics": metrics,
    "publish": publish,
}


# -- protocol ---------------------------------------------------------------------------


def _failure(exc: PulsarError) -> dict[str, Any]:
    return exc.to_envelope()


def handle(envelope: object, app: App) -> dict[str, Any]:
    """One envelope in, one response out. Never raises."""
    try:
        request = _parse(envelope, app.environ)
        handler = TOOLS[request.tool]
        check_plugin_home(app)
        output = asyncio.run(handler(app, request))
    except PulsarError as exc:
        return _failure(exc)
    except Exception as exc:
        log.exception("pulsar orbit-tool: unexpected error")
        return _failure(
            PulsarError(INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False)
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
    unknown = sorted(set(input_) - INPUTS[verb])
    if unknown:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"{NAMESPACE}.{verb} does not take " + ", ".join(f"`{k}`" for k in unknown),
            detail={"unknown": unknown, "accepted": sorted(INPUTS[verb])},
        )
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


def main(app: App, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read one envelope, write one response. The exit code is 0 whenever a
    response was written: the envelope's ``ok`` carries the outcome."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    raw = source.read()
    try:
        envelope: object = json.loads(raw)
    except ValueError:
        response = _failure(PulsarError(INVALID_ARGUMENT, "the request envelope is not JSON"))
    else:
        response = handle(envelope, app)
    sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sink.flush()
    return 0
