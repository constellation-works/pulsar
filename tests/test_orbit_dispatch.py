"""``pulsar.dispatch`` and the ``pulsar-dispatch`` routine: only approved, due,
unpublished plans from the configured location go out, each at most once however
many callers race for it, and unknown writes are reconciled."""

import asyncio
import io
import json
import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import httpx
import pytest
import yaml

from pulsar.app import ops, plugin
from pulsar.internal.fs import Paths
from pulsar.orbit import backend as orbit_tool

from .conftest import ALIAS, FakeX, FlakyX, collect, make_app, reap, register
from .test_orbit_tool import MANIFEST, PLUGIN, plugin_app, run

# What a routine's step sends: no task, no model.
ROUTINE = {"agent": "orbit", "model": "none", "config": {}}
PAST = "2026-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("PULSAR_HOME", raising=False)
    return tmp_path / "plugin-state"


@pytest.fixture
def home(state, bundle) -> Paths:
    paths = Paths(state / "home")
    register(paths, bundle, ALIAS, handle="constworks", provider_user_id="1234567890")
    configure(paths)
    return paths


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def configure(home: Paths, extra: str = "") -> None:
    config = home.home / "config.toml"
    config.write_text(f'[dispatch]\nplans = ["plans/**/*.yaml"]\n{extra}')
    config.chmod(0o600)


def dispatch(state, workspace, transport, input_=None):
    context = {**ROUTINE, "workspace_root": str(workspace)}
    return run(state, workspace, "dispatch", input_, transport=transport, context=context)


def draft(workspace: Path, name: str, text: str, not_before: str | None = PAST) -> str:
    source = f"plans/{name}.yaml"
    path = workspace / source
    path.parent.mkdir(parents=True, exist_ok=True)
    when = f'not_before: "{not_before}"\n' if not_before else ""
    path.write_text(f'account: {ALIAS}\n{when}text: "{text}"\n')
    return source


def approve(home: Paths, workspace: Path, source: str) -> None:
    """What a human's ``pulsar approve <workspace>/<source> --workspace <workspace>`` records."""
    app = make_app(home)
    path = workspace / source
    preview, _ = app.approve_preview(path, account=None, workspace=workspace)
    expect = {a["account"]: a["digest"] for a in preview["accounts"]}
    app.approve(path, expect=expect, account=None, workspace=workspace)


def reasons(out: dict) -> dict[str, str]:
    return {s["source"]: s["reason"] for s in out["skipped"]}


def rows(home: Paths, query: str) -> list[tuple]:
    with sqlite3.connect(home.ledger_db) as conn:
        return conn.execute(query).fetchall()


# -- what goes out -------------------------------------------------------------------------


def test_only_an_approved_due_plan_is_published_and_the_rest_say_why(
    state, home, workspace, fake_x
):
    due = draft(workspace, "due", "Approved and due.")
    later = draft(workspace, "later", "Approved, not due yet.", not_before=FUTURE)
    unapproved = draft(workspace, "unapproved", "Due, never approved.")
    unscheduled = draft(workspace, "unscheduled", "Approved, no slot.", not_before=None)
    for source in (due, later, unscheduled):
        approve(home, workspace, source)

    out = dispatch(state, workspace, fake_x.transport())["output"]
    [sent] = out["published"]
    assert sent["source"] == due and sent["ok"] and sent["state"] == "published"
    assert reasons(out) == {
        later: "not_due",
        unapproved: "approval_required",
        unscheduled: "unscheduled",
    }
    by_source = {s["source"]: s for s in out["skipped"]}
    assert by_source[later]["retry_after"] == "2099-01-01T00:00:00+00:00"
    assert "pulsar approve" in by_source[unapproved]["message"]
    assert out["errors"] == [] and out["scanned"] == 4
    [post] = fake_x.calls("POST", "/tweets")
    assert b"Approved and due." in post.content
    assert rows(home, "SELECT caller, state FROM writes") == [("orbit:orbit", "published")]

    again = dispatch(state, workspace, fake_x.transport())["output"]
    assert again["published"] == [] and reasons(again)[due] == "published"
    assert len(fake_x.calls("POST", "/tweets")) == 1


def test_an_edit_after_approval_is_skipped_as_stale(state, home, workspace, fake_x):
    source = draft(workspace, "edited", "As approved.")
    approve(home, workspace, source)
    draft(workspace, "edited", "Changed after approval.")
    out = dispatch(state, workspace, fake_x.transport())["output"]
    [skip] = out["skipped"]
    assert skip["reason"] == "approval_stale" and "changed after it was approved" in skip["message"]
    assert fake_x.calls("POST") == []
    assert rows(home, "SELECT used_key FROM approvals") == [(None,)], "nothing was consumed"


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("[policy]\ndaily_budget_usd = 0.001\n", "budget_exceeded"),
        ("[policy]\nmax_posts_per_day = 0\n", "daily_cap"),
        (None, "quiet_hours"),
    ],
)
def test_quiet_hours_and_budget_skip_and_a_later_call_publishes(
    state, home, workspace, fake_x, extra, reason
):
    if extra is None:
        now = datetime.now(UTC)
        start, end = now - timedelta(hours=1), now + timedelta(hours=1)
        extra = f'[policy]\nquiet_hours = "{start:%H:%M}-{end:%H:%M}"\n'
    configure(home, extra)
    source = draft(workspace, "held", "Held by the policy.")
    approve(home, workspace, source)

    out = dispatch(state, workspace, fake_x.transport())["output"]
    [skip] = out["skipped"]
    assert skip["reason"] == reason and out["published"] == []
    assert fake_x.calls("POST") == []
    assert rows(home, "SELECT count(*) FROM writes") == [(0,)]

    configure(home)  # the window moved on
    out = dispatch(state, workspace, fake_x.transport())["output"]
    assert [p["source"] for p in out["published"]] == [source]
    assert len(fake_x.calls("POST", "/tweets")) == 1


def test_at_most_max_publish_plans_go_out_per_call(state, home, workspace, fake_x):
    sources = [draft(workspace, f"p{i}", f"Post number {i}.") for i in range(3)]
    for source in sources:
        approve(home, workspace, source)
    out = dispatch(state, workspace, fake_x.transport(), {"max_publish": 2})["output"]
    assert [p["source"] for p in out["published"]] == sources[:2]
    assert reasons(out) == {sources[2]: "tick_limit"}
    out = dispatch(state, workspace, fake_x.transport(), {"max_publish": 2})["output"]
    assert [p["source"] for p in out["published"]] == sources[2:]
    assert len(fake_x.calls("POST", "/tweets")) == 3


def test_a_call_past_its_deadline_starts_nothing(home, workspace, fake_x):
    source = draft(workspace, "late", "Out of time.")
    approve(home, workspace, source)

    async def go() -> dict:
        rt = make_app(home, transport=fake_x.transport()).runtime(media_base=workspace)
        async with rt:
            return await plugin.dispatch(
                rt, workspace=workspace, max_publish=3, dry_run=False, caller="test",
                deadline=0.0, clock=lambda: 1.0,
            )  # fmt: skip

    out = asyncio.run(go())
    assert reasons(out) == {source: "deadline"} and fake_x.requests == []


def test_a_dry_run_reports_what_is_ready_and_changes_nothing(state, home, workspace, fake_x):
    source = draft(workspace, "ready", "Ready to go.")
    approve(home, workspace, source)
    before = home.ledger_db.read_bytes()
    out = dispatch(state, workspace, fake_x.transport(), {"dry_run": True})["output"]
    [ready] = out["ready"]
    assert ready["source"] == source and ready["accounts"][0]["account"] == ALIAS
    assert out["published"] == [] and fake_x.requests == []
    assert home.ledger_db.read_bytes() == before


# -- where plans come from -----------------------------------------------------------------


def test_dispatch_reads_only_the_configured_location_inside_the_workspace(
    state, home, workspace, fake_x, tmp_path
):
    elsewhere = draft(workspace, "x", "Not under plans/.").replace("plans/", "other/")
    (workspace / "other").mkdir()
    (workspace / "plans" / "x.yaml").rename(workspace / elsewhere)
    approve(home, workspace, elsewhere)
    outside = tmp_path / "outside.yaml"
    outside.write_text(f'account: {ALIAS}\nnot_before: "{PAST}"\ntext: "Outside."\n')
    (workspace / "plans").mkdir(exist_ok=True)
    (workspace / "plans" / "escape.yaml").symlink_to(outside)
    hidden = workspace / "plans" / ".orbit" / "copy.yaml"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(outside.read_text())

    out = dispatch(state, workspace, fake_x.transport())["output"]
    assert out["scanned"] == 1 and out["published"] == []
    [error] = out["errors"]
    assert error["source"] == "plans/escape.yaml"
    assert error["error"]["code"] == "invalid_argument"
    assert fake_x.calls("POST") == []


def test_dispatch_takes_no_path_key_or_approval_input(state, home, workspace, fake_x):
    for extra in ({"source": "plans/a.yaml"}, {"plans": ["**"]}, {"approved": True},
                  {"idempotency_key": "k"}):  # fmt: skip
        response = dispatch(state, workspace, fake_x.transport(), extra)
        assert response["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("max_publish", [0, 11, True, "3"])
def test_max_publish_is_bounded(state, home, workspace, fake_x, max_publish):
    response = dispatch(state, workspace, fake_x.transport(), {"max_publish": max_publish})
    assert response["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("pattern", ["/etc/*.yaml", "~/plans/*.yaml", "../plans/*.yaml",
                                     "plans/../../x.yaml", ""])  # fmt: skip
def test_a_plan_pattern_outside_the_workspace_is_invalid_config(
    state, home, workspace, fake_x, pattern
):
    config = home.home / "config.toml"
    config.write_text(f"[dispatch]\nplans = [{json.dumps(pattern)}]\n")
    response = dispatch(state, workspace, fake_x.transport())
    assert response["error"]["code"] == "invalid_config"
    assert response["error"]["detail"]["key"] == "dispatch.plans"


# -- reconcile -----------------------------------------------------------------------------


def test_dispatch_reconciles_an_unknown_write(state, home, workspace):
    flaky = FlakyX(tweet_raise=httpx.ReadTimeout)
    plan = workspace / "manual.yaml"
    plan.write_text(f'account: {ALIAS}\ntext: "Lost response."\n')
    asyncio.run(make_app(home, transport=flaky.transport()).publish(plan, confirm=True))
    [(key, state_)] = rows(home, "SELECT idempotency_key, state FROM writes")
    assert state_ == "unknown"

    created = (datetime.now(UTC) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    flaky.own_tweets = [{"id": "101", "text": "Lost response.", "created_at": created}]
    out = dispatch(state, workspace, flaky.transport())["output"]
    [entry] = out["reconciled"]
    assert entry["account"] == ALIAS and entry["pending"] == [key]
    assert entry["results"][0]["state"] == "published"
    assert rows(home, "SELECT state FROM writes") == [("published",)]
    assert len(flaky.posts()) == 1, "reconcile reads; it never posts"


# -- racing --------------------------------------------------------------------------------


def _dispatch_in_process(state: str, workspace: str, go, results) -> None:
    fake = FakeX()
    app = plugin_app({"ORBIT_PLUGIN_STATE": state}, fake.transport())
    request = {
        "schema_version": 1,
        "tool": "pulsar.dispatch",
        "input": {},
        "context": {**ROUTINE, "workspace_root": workspace},
    }
    out = io.StringIO()
    go.wait(30)
    orbit_tool.main(app, io.StringIO(json.dumps(request)), out)
    response = json.loads(out.getvalue())
    results.put((len(fake.calls("POST", "/tweets")), response))


def test_concurrent_dispatch_calls_post_each_plan_once(state, home, workspace):
    sources = [draft(workspace, f"race{i}", f"Raced post {i}.") for i in range(2)]
    for source in sources:
        approve(home, workspace, source)
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_dispatch_in_process, args=(str(state), str(workspace), go, results))
        for _ in range(4)
    ]
    try:
        for p in procs:
            p.start()
        go.set()
        outcomes = collect(results, procs)
        for p in procs:
            p.join(timeout=30)
    finally:
        reap(procs)
    assert all(response["ok"] for _, response in outcomes), outcomes
    assert sum(posts for posts, _ in outcomes) == 2, "one provider call per plan"
    sent = [p["source"] for _, r in outcomes for p in r["output"]["published"] if not p["replayed"]]
    assert sorted(sent) == sources
    assert rows(home, "SELECT state FROM writes") == [("published",), ("published",)]


@pytest.mark.parametrize("first", ["dispatch", "manual"])
def test_dispatch_racing_a_manual_publish_posts_once(home, workspace, first):
    source = draft(workspace, "contested", "Contested post.")
    approve(home, workspace, source)
    flaky = FlakyX()
    flaky.tweet_gate = anyio.Event()
    arrived = anyio.Event()
    handle = flaky.handle_async

    async def spy(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tweets"):
            arrived.set()
        return await handle(request)

    flaky.handle_async = spy
    app = make_app(home, transport=flaky.transport())

    async def by_dispatch() -> dict:
        rt = app.runtime(media_base=workspace)
        async with rt:
            return await plugin.dispatch(
                rt, workspace=workspace, max_publish=3, dry_run=False, caller="orbit",
                deadline=float("inf"),
            )  # fmt: skip

    async def by_hand() -> dict:
        rt = app.runtime(media_base=workspace)
        async with rt:
            report, _ = await ops.publish_report(rt, workspace / source, confirm=True)
            return report

    async def race() -> tuple[dict, dict]:
        one, two = (by_dispatch, by_hand) if first == "dispatch" else (by_hand, by_dispatch)
        got: dict[str, dict] = {}

        async def lead() -> None:
            got["first"] = await one()

        async with anyio.create_task_group() as tg:
            tg.start_soon(lead)
            with anyio.fail_after(5):
                await arrived.wait()  # the first caller's row is claimed and its POST in flight
            got["second"] = await two()
            flaky.tweet_gate.set()
        return got["first"], got["second"]

    lead, follow = anyio.run(race)
    assert len(flaky.posts()) == 1
    if first == "dispatch":
        assert lead["published"][0]["ok"] and lead["published"][0]["state"] == "published"
        assert follow["results"][0]["error"]["code"] == "outcome_unknown"
    else:
        assert lead["results"][0]["ok"]
        assert reasons(follow) == {source: "in_flight"} and follow["published"] == []
    assert rows(home, "SELECT state FROM writes") == [("published",)]


# -- the approval rule -----------------------------------------------------------------


def test_dispatch_never_records_or_skips_an_approval(state, home, workspace, fake_x):
    source = draft(workspace, "plain", "Never approved.")
    for _ in range(2):
        out = dispatch(state, workspace, fake_x.transport())["output"]
        assert reasons(out) == {source: "approval_required"}
    assert rows(home, "SELECT count(*) FROM approvals") == [(0,)]
    assert fake_x.calls("POST") == []


# -- the routine ---------------------------------------------------------------------------

DEFINITIONS = PLUGIN / "definitions"


def test_the_routine_is_a_disabled_deterministic_tool_call_with_no_model():
    routine = yaml.safe_load((DEFINITIONS / "routines" / "dispatch.yaml").read_text())
    assert routine["name"] == "dispatch" and routine["enabled"] is False
    assert routine["trigger"]["cron"] == "*/5 * * * *"
    assert routine["policy"]["overlap"] == "forbid"
    job = yaml.safe_load((DEFINITIONS / "jobs" / "dispatch.yaml").read_text())
    assert routine["target"] == f"job:{job['metadata']['name']}"
    [step] = job["spec"]["steps"]
    activity = yaml.safe_load((DEFINITIONS / "activities" / "dispatch.yaml").read_text())
    assert step["target"] == f"activity:{activity['metadata']['name']}"
    spec = activity["spec"]
    assert spec["type"] == "deterministic" and spec["action"] == "plugin.tool_call"
    assert spec["config"]["tool"] == "pulsar.dispatch"
    assert not {"prompt", "model", "allowed_tools"} & set(spec), "no model in the loop"
    max_publish = spec["config"]["input"]["max_publish"]
    low, _, high = plugin.DISPATCH_PUBLISHES
    assert low <= max_publish <= high


def test_the_dispatch_schema_advertises_the_limits_the_code_enforces():
    from .test_orbit_tool import _schema

    prop = _schema("dispatch", "request").schema["properties"]["max_publish"]
    assert (prop["minimum"], prop["default"], prop["maximum"]) == plugin.DISPATCH_PUBLISHES
    assert MANIFEST["spec"]["backend"]["timeout_ms"] / 1000 > orbit_tool.DISPATCH_WORK_SECONDS
