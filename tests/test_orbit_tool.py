"""The Orbit exec backend (``orbit/backend.py``) and the plugin manifest around it.

These run the backend in-process; ``orbit plugin test <clean export> --grant fs,network``
runs the same goldens (``tests/conformance/``) through Orbit and its sandbox.
"""

import asyncio
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from pulsar.app import ops, plugin
from pulsar.app.facade import LocalApp
from pulsar.app.runtime import default_paths
from pulsar.internal.fs import Paths
from pulsar.main import main as pulsar_main
from pulsar.orbit import backend as orbit_tool

from .conftest import ALIAS, SECRETS, make_app, register
from .media_samples import PNG
from .test_server import SECRET_PARAM

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".orbit-plugin"
MANIFEST = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
GOLDENS = yaml.safe_load((PLUGIN / "tests" / "conformance" / "pulsar.yaml").read_text())["tests"]
assert GOLDENS, ".orbit-plugin/tests/conformance/pulsar.yaml has no cases"


def plugin_app(environ: dict[str, str], transport: Any = None) -> LocalApp:
    """The app ``pulsar.main`` builds for ``pulsar orbit-tool`` under ``environ``."""
    return LocalApp(
        default_paths(environ, Path.home()), environ=environ, cwd=Path.cwd(), transport=transport
    )


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    # Orbit's backend environment: no PULSAR_HOME (a different one is refused).
    monkeypatch.delenv("PULSAR_HOME", raising=False)
    return tmp_path / "plugin-state"


@pytest.fixture
def home(state) -> Paths:
    return Paths(state / "home")


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def run(
    state: Path,
    workspace: Path | None,
    tool: str,
    input_: Any = None,
    *,
    transport: Any = None,
    **envelope: Any,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema_version": 1,
        "tool": f"pulsar.{tool}",
        "input": {} if input_ is None else input_,
        "context": {
            "workspace_root": None if workspace is None else str(workspace),
            "agent": "test-agent",
            "model": "test",
            "config": {},
        },
    }
    request.update(envelope)
    out = io.StringIO()
    app = plugin_app({"ORBIT_PLUGIN_STATE": str(state)}, transport)
    code = orbit_tool.main(app, io.StringIO(json.dumps(request)), out)
    assert code == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, "exactly one response line"
    response = json.loads(lines[0])
    assert not any(s in lines[0] for s in SECRETS)
    if response["ok"]:
        _schema(tool, "response").validate(response["output"])
    else:
        assert set(response["error"]) <= {"code", "message", "retryable", "detail"}
    return response


def _schema(tool: str, kind: str) -> jsonschema.protocols.Validator:
    schema = json.loads((PLUGIN / "schemas" / f"{tool}.{kind}.json").read_text())
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


# -- goldens --------------------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDENS, ids=[c["name"] for c in GOLDENS])
def test_conformance_golden(case, state, workspace):
    _schema(case["tool"], "request").validate(case.get("input", {}))
    response = run(state, workspace, case["tool"], case.get("input"))
    expect = case["expect"]
    if "error" in expect:
        assert not response["ok"] and response["error"]["code"] == expect["error"]["code"]
    else:
        assert response == {"ok": True, "output": expect["output"]}


# -- manifest -------------------------------------------------------------------------


def test_manifest_declares_exactly_the_backend_tools():
    assert {t["name"] for t in MANIFEST["spec"]["tools"]} == set(orbit_tool.TOOLS)
    # Spend money: callable only when required.
    paid = {"engagements", "metrics", "publish", "dispatch"}
    for tool in MANIFEST["spec"]["tools"]:
        kind = "mutating" if tool["name"] in paid else "read_only"
        assert tool["execution_kind"] == kind, tool["name"]
        assert tool["input_schema"] == {"$ref": f"schemas/{tool['name']}.request.json"}
        assert tool["output_schema"] == {"$ref": f"schemas/{tool['name']}.response.json"}


def test_publish_passes_upload_cap_below_backend_timeout(state, workspace, monkeypatch):
    observed = []

    async def fake_publish(rt, **_kwargs):
        observed.append(rt._client_kwargs["upload_deadline"])
        return {}

    monkeypatch.setattr(plugin, "publish", fake_publish)
    app = plugin_app({"ORBIT_PLUGIN_STATE": str(state)})
    call = orbit_tool.Call("publish", {"source": "plan.yaml"}, {"workspace_root": str(workspace)})
    assert asyncio.run(orbit_tool.publish(app, call)) == {}
    remaining = observed[0] - orbit_tool.time.monotonic()
    assert 0 < remaining <= orbit_tool.PUBLISH_UPLOAD_TIMEOUT_SECONDS
    assert remaining * 1000 < MANIFEST["spec"]["backend"]["timeout_ms"]


def test_no_request_schema_takes_a_credential():
    for path in (PLUGIN / "schemas").glob("*.request.json"):
        schema = json.loads(path.read_text())
        assert schema.get("additionalProperties") is False, path.name
        for prop in schema["properties"]:
            assert not SECRET_PARAM.search(prop), f"{path.name}: {prop}"
    config = json.loads((PLUGIN / "schemas" / "config.json").read_text())
    assert config["additionalProperties"] is False and not config.get("properties")


def test_launcher_and_skill_ship_in_the_tree():
    launcher = PLUGIN / MANIFEST["spec"]["backend"]["command"]
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert "-m pulsar orbit-tool" in launcher.read_text(), (
        "the manifest's backend command must exec the Orbit backend through the entry point"
    )
    for skill in MANIFEST["spec"]["skills"]:
        text = (PLUGIN / skill / "SKILL.md").read_text()
        # Orbit links skills/<dir> as <namespace>-<dir>; the skill must answer to that name.
        linked = f"{MANIFEST['metadata']['name']}-{Path(skill).name}"
        assert re.search(rf"(?m)^name: {re.escape(linked)}$", text), (
            f"{skill} links as {linked}; its SKILL.md must be named that"
        )
    # The plugin installer refuses symlinks anywhere in the tree.
    assert not any(path.is_symlink() for path in PLUGIN.rglob("*"))


# -- envelope -------------------------------------------------------------------------


def _raw(text: str) -> dict[str, Any]:
    out = io.StringIO()
    orbit_tool.main(plugin_app({"ORBIT_PLUGIN_STATE": "/nonexistent"}), io.StringIO(text), out)
    return json.loads(out.getvalue())


@pytest.mark.parametrize(
    "text, needle",
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        ('{"schema_version": 2, "tool": "pulsar.status"}', "schema_version 2"),
        ('{"schema_version": 1, "tool": "pulsar.create_post"}', "unknown tool"),
        ('{"schema_version": 1, "tool": "pulsar.status", "input": []}', "`input`"),
        ('{"schema_version": 1, "tool": "pulsar.status", "context": 3}', "`context`"),
    ],
)
def test_malformed_envelopes_are_invalid_argument(text, needle):
    response = _raw(text)
    assert response["ok"] is False and response["error"]["code"] == "invalid_argument"
    assert needle in response["error"]["message"]


def test_plugin_config_keys_are_refused(state, workspace):
    response = run(
        state,
        workspace,
        "status",
        context={"workspace_root": str(workspace), "config": {"daily_budget_usd": 5}},
    )
    assert response["error"]["code"] == "invalid_argument"
    assert "config.toml" in response["error"]["message"]


def test_an_unexpected_failure_is_still_one_envelope(state, workspace, monkeypatch):
    async def boom(app, call):
        raise RuntimeError("secret detail that must not leak")

    monkeypatch.setitem(orbit_tool.TOOLS, "status", boom)
    response = run(state, workspace, "status")
    assert response["error"] == {
        "code": "internal",
        "message": "internal error: RuntimeError",
        "retryable": False,
    }


def test_the_home_is_the_plugin_state_not_pulsar_home(state, home, workspace):
    register(home, None, ALIAS)
    [account] = run(state, workspace, "status")["output"]["accounts"]
    assert account["alias"] == ALIAS


def test_cli_orbit_tool_reads_stdin(paths, monkeypatch, capsys):
    monkeypatch.delenv("ORBIT_PLUGIN_STATE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schema_version": 1, "tool": "pulsar.history"}'))
    assert pulsar_main(["orbit-tool"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "output": {"rows": [], "total": 0, "truncated": False},
    }


def test_a_different_pulsar_home_under_orbit_is_refused_before_any_work(state):
    elsewhere = state.parent / "elsewhere"
    environ = {"ORBIT_PLUGIN_STATE": str(state), "PULSAR_HOME": str(elsewhere)}
    response = orbit_tool.handle(
        {"schema_version": 1, "tool": "pulsar.history"}, plugin_app(environ)
    )
    assert response["ok"] is False and response["error"]["code"] == "invalid_config"
    assert response["error"]["detail"] == {
        "pulsar_home": str(elsewhere),
        "plugin_home": str(state / "home"),
    }
    assert not state.exists() and not elsewhere.exists()


def test_pulsar_home_naming_the_plugin_home_is_accepted(state):
    environ = {"ORBIT_PLUGIN_STATE": str(state), "PULSAR_HOME": str(state / "home")}
    response = orbit_tool.handle(
        {"schema_version": 1, "tool": "pulsar.history"}, plugin_app(environ)
    )
    assert response["ok"] is True


@pytest.mark.parametrize("tool", sorted(orbit_tool.TOOLS))
def test_each_tool_accepts_exactly_its_schema_properties(tool):
    schema = json.loads((PLUGIN / "schemas" / f"{tool}.request.json").read_text())
    assert orbit_tool.INPUTS[tool] == set(schema["properties"])


def test_an_unknown_input_key_is_refused(state, workspace):
    response = run(state, workspace, "history", {"limt": 5})
    assert response["error"]["code"] == "invalid_argument"
    assert "`limt`" in response["error"]["message"]
    assert response["error"]["detail"] == {
        "unknown": ["limt"],
        "accepted": ["account", "keys", "limit"],
    }


# -- tools ----------------------------------------------------------------------------


def test_status_reports_a_bound_account_without_its_credentials(state, home, bundle, workspace):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    out = run(state, workspace, "status")["output"]
    [account] = out["accounts"]
    assert account["authorized"] is True and account["reauth_required"] is False
    assert account["posts"] == {"used": 0, "cap": 5, "remaining": 5}
    assert account["unresolved"] == [] and account["last_published"] is None
    assert out["healthy"] is True and out["attention"] == []


def test_status_asks_for_a_login_when_the_token_is_gone(state, home, workspace):
    register(home, None, ALIAS)
    out = run(state, workspace, "status")["output"]
    assert out["healthy"] is False
    assert out["attention"] == [
        f"{ALIAS}: re-authorization required (`PULSAR_HOME={home.home} pulsar auth login "
        f"--account {ALIAS}`)"
    ]
    [account] = out["accounts"]
    assert account["health"] == "unhealthy"


def test_status_says_when_offline_health_is_unverified(state, home, bundle, workspace):
    register(home, bundle, ALIAS)  # bound, identity never looked up
    out = run(state, workspace, "status")["output"]
    [account] = out["accounts"]
    assert account["health"] == "unverified" and account["healthy"] is False
    assert out["healthy"] is False
    assert "--live" in out["attention"][0]


def test_status_changes_nothing_on_disk(state, home, bundle, workspace):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    before = {p: p.stat().st_mtime_ns for p in home.home.rglob("*")}
    run(state, workspace, "status")
    run(state, workspace, "history")
    assert {p: p.stat().st_mtime_ns for p in home.home.rglob("*")} == before


def test_history_and_status_see_what_was_published(
    state, home, bundle, workspace, fake_x, tmp_path
):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    plan = tmp_path / "plan.yaml"
    plan.write_text('account: x:constworks\nposts: [{text: "one"}, {text: "two"}]\n')
    receipt, code = asyncio.run(
        make_app(home, transport=fake_x.transport()).publish(plan, confirm=True)
    )
    assert code == 0
    requests = len(fake_x.requests)

    history = run(state, workspace, "history", {"limit": 5})["output"]
    assert (history["total"], history["truncated"]) == (1, False)
    [row] = history["rows"]
    assert row["account"] == ALIAS and row["state"] == "published"
    assert (row["posts"], row["published"]) == (2, 2)
    assert row["cost_usd"] == pytest.approx(0.03)
    assert row["url"] == receipt["results"][0]["items"][0]["url"]

    [account] = run(state, workspace, "status")["output"]["accounts"]
    assert account["posts"]["used"] == 2
    assert account["last_published"]["key"] == row["key"]
    assert len(fake_x.requests) == requests, "the plugin tools are offline"


@pytest.mark.parametrize("limit", [0, 101, "5", True])
def test_history_limit_is_bounded(state, workspace, limit):
    response = run(state, workspace, "history", {"limit": limit})
    assert response["error"]["code"] == "invalid_argument"


def test_validate_reads_a_source_with_media_in_the_workspace(state, workspace):
    (workspace / "art").mkdir()
    (workspace / "art" / "banner.png").write_bytes(PNG)
    (workspace / "plan.yaml").write_text(
        "account: x:constworks\ntext: launch\nmedia: [{path: art/banner.png, alt: The banner}]\n"
    )
    out = run(state, workspace, "validate", {"source": "plan.yaml"})["output"]
    assert out["valid"] is True
    [report] = out["accounts"]
    [media] = report["posts"][0]["media"]
    assert media["mime"] == "image/png" and media["alt"] == "The banner"
    # The same plan inline gives the same digest: the source path is not in it.
    inline = run(
        state,
        workspace,
        "validate",
        {
            "plan": {
                "account": ALIAS,
                "text": "launch",
                "media": [{"path": "art/banner.png", "alt": "The banner"}],
            }
        },
    )["output"]
    assert inline["accounts"][0]["digest"] == report["digest"]


def test_validate_refuses_media_outside_the_workspace(state, workspace, tmp_path):
    (tmp_path / "outside.png").write_bytes(PNG)
    out = run(
        state,
        workspace,
        "validate",
        {
            "plan": {
                "account": ALIAS,
                "text": "x",
                "media": [{"path": str(tmp_path / "outside.png"), "alt": "A"}],
            }
        },
    )["output"]
    assert out["valid"] is False and out["error"]["code"] == "invalid_media"


def test_validate_refuses_a_source_that_escapes_by_symlink(state, workspace, tmp_path):
    (tmp_path / "secret.yaml").write_text("text: hi\n")
    (workspace / "plan.yaml").symlink_to(tmp_path / "secret.yaml")
    response = run(state, workspace, "validate", {"source": "plan.yaml"})
    assert response["error"]["code"] == "invalid_argument"


def test_validate_refuses_an_oversized_or_binary_source(state, workspace):
    (workspace / "big.yaml").write_bytes(b"#" * (plugin.SOURCE_MAX_BYTES + 1))
    (workspace / "bin.yaml").write_bytes(b"\xff\xfe")
    for name in ("big.yaml", "bin.yaml", "missing.yaml"):
        response = run(state, workspace, "validate", {"source": name})
        assert response["error"]["code"] == "invalid_argument", name


def test_validate_source_needs_a_workspace(state):
    response = run(state, None, "validate", {"source": "plan.yaml"})
    assert response["error"]["code"] == "invalid_argument"


def test_validate_never_changes_the_working_directory(state, workspace, monkeypatch):
    (workspace / "banner.png").write_bytes(PNG)

    def refuse(_path):
        raise AssertionError("the backend must not chdir")

    monkeypatch.setattr(os, "chdir", refuse)
    plan = {"account": ALIAS, "text": "hi", "media": [{"path": "banner.png", "alt": "B"}]}
    out = run(state, workspace, "validate", {"plan": plan})["output"]
    assert out["valid"] is True, "relative media resolves against the workspace"


def test_validate_refuses_a_source_that_is_a_directory_or_the_workspace(state, workspace):
    (workspace / "plans").mkdir()
    for source in ("plans", "."):
        response = run(state, workspace, "validate", {"source": source})
        assert response["error"]["code"] == "invalid_argument", source


def test_validate_answers_a_bad_plan_as_a_verdict_and_a_bad_account_as_an_error(state, workspace):
    bad_plan = run(state, workspace, "validate", {"plan": {"account": "x:constworks", "posts": []}})
    assert bad_plan["ok"] is True and bad_plan["output"]["valid"] is False
    plan = {"account": "x:constworks", "posts": [{"text": "hi"}]}
    unknown = run(state, workspace, "validate", {"plan": plan, "account": "x:nobody"})
    assert unknown["ok"] is False and unknown["error"]["code"] == "unknown_account"


def test_the_history_schema_advertises_the_limits_the_code_enforces():
    limit = _schema("history", "request").schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (
        1,
        ops.HISTORY_LIMIT_MAX,
        ops.HISTORY_LIMIT_DEFAULT,
    )


# -- auto-tasks -----------------------------------------------------------------------

AUTO_TASKS = sorted((PLUGIN / "definitions" / "auto_tasks").glob("*.yaml"))


def test_the_manifest_ships_the_auto_task_definitions():
    assert MANIFEST["spec"]["definitions"] == {
        "activities": ["definitions/activities/*.yaml"],
        "jobs": ["definitions/jobs/*.yaml"],
        "routines": ["definitions/routines/*.yaml"],
        "auto_tasks": ["definitions/auto_tasks/*.yaml"],
    }
    assert [p.stem for p in AUTO_TASKS] == [
        "auth-health",
        "engager",
        "post-proposer",
        "weekly-report",
        "x-updates",
    ]


@pytest.mark.parametrize("path", AUTO_TASKS, ids=[p.stem for p in AUTO_TASKS])
def test_an_auto_task_may_call_only_what_it_requires(path):
    definition = yaml.safe_load(path.read_text())
    assert definition["schemaVersion"] == 1 and definition["name"] == path.stem
    assert definition["enabled"] is False, "Orbit seeds them disabled; a human enables one"
    assert definition["dedupe"] == "skip_if_open"
    assert list(definition["schedule"]) == ["cron"]
    template = definition["template"]
    required = {name.removeprefix("pulsar.") for name in template["required_tools"]}
    # Every pulsar.* tool the instructions call, except publish, is in required_tools.
    called = set(re.findall(r"`pulsar\.(\w+)`", template["description"]))
    assert "publish" not in required, "an auto-task only drafts; a human-promoted task publishes"
    assert called - {"publish"} <= required
    assert "never publish" in template["description"].lower()
    # Definitions leave files for pipeline delivery and never instruct committing.
    cleaned = path.read_text().replace("uncommitted", "").replace("commit permalink", "")
    assert "commit" not in cleaned.lower()
    # The account alarm delivers tool state; drafting/reporting may deliver no files.
    assert "no-diff-expected" in template["tags"], (
        "ORB-13375: a documented no-file stop must survive Orbit's empty-stage gate"
    )
    # Engager's and x-updates' publish tasks write nothing (keep no-diff-expected);
    # post-proposer's records receipts in content records (drops no-diff-expected).
    for spawned in re.findall(r"tags `\[([^\]]*)\]`", template["description"]):
        tags = [t.strip() for t in spawned.split(",")]
        if path.stem == "post-proposer":
            assert "no-diff-expected" not in tags
        elif path.stem in ("engager", "x-updates"):
            assert "no-diff-expected" in tags


AUTH_HEALTH = PLUGIN / "definitions" / "auto_tasks" / "auth-health.yaml"
AUTH_PLANNER = PLUGIN / "skills" / "publish" / "scripts" / "auth_health.py"


def test_auth_health_definition_is_a_disabled_daily_offline_alarm():
    definition = yaml.safe_load(AUTH_HEALTH.read_text())
    assert definition["enabled"] is False
    assert definition["schedule"] == {"cron": "0 8 * * *"}
    assert definition["dedupe"] == "skip_if_open"
    assert definition["template"]["required_tools"] == ["pulsar.status"]
    assert "no-diff-expected" in definition["template"]["tags"]
    # Exercise the same shipped helper the executor is directed to use.
    assert "scripts/auth_health.py" in definition["template"]["description"]
    assert AUTH_PLANNER.is_file()


def auth_health_plan(status, workspace, tasks=None):
    listed = [] if tasks is None else tasks
    result = subprocess.run(
        [sys.executable, "-B", str(AUTH_PLANNER)],
        input=json.dumps(
            {"status": status, "tasks": {"tasks": listed, "total": len(listed), "truncated": False}}
        ),
        capture_output=True,
        text=True,
        check=True,
        cwd=workspace,
    )
    assert not any(secret in result.stdout for secret in SECRETS)
    return json.loads(result.stdout)


@pytest.mark.parametrize("health", ["healthy", "unverified", "reauth_required"])
def test_auth_health_plans_human_followups_from_offline_status(
    state, home, bundle, workspace, fake_x, health
):
    register(
        home,
        replace(bundle, expires_at=0) if health == "unverified" else bundle,
        ALIAS,
        handle="constworks",
        provider_user_id="1",
        status="reauth_required" if health == "reauth_required" else "active",
    )
    before = {p: p.stat().st_mtime_ns for p in state.rglob("*")}
    status = run(state, workspace, "status", transport=fake_x.transport())["output"]
    result = auth_health_plan(status, workspace)
    assert result["updates"] == result["skipped"] == []
    if health == "healthy":
        assert result["followups"] == []
    else:
        [followup] = result["followups"]
        [account] = status["accounts"]
        assert ALIAS in followup["title"]
        assert status["attention"][0] in followup["description"]
        for key in ("alias", "health", "token_state", "reason", "reauth_required"):
            assert f"- {key}: {json.dumps(account[key])}" in followup["description"]
        if health == "unverified":
            assert account["token_state"] == "expired" and account["reauth_required"] is False
            assert "unverified" in followup["title"]
            assert followup["priority"] == "medium"
            assert f"auth status --live --account {ALIAS}" in followup["description"]
            assert "pulsar auth login" not in followup["description"]
        else:
            assert "reauth_required" in followup["title"]
            assert followup["priority"] == "high"
            assert f"pulsar auth login --account {ALIAS}" in followup["description"]
        assert f"PULSAR_HOME={home.home}" in followup["description"]
        assert f"pulsar-auth-health:{ALIAS}" in followup["tags"]
        assert "no-diff-expected" in followup["tags"]
        assert followup["required_tools"] == ["pulsar.status"]
        assert "status" not in followup, "orbit.task.add leaves a follow-up proposed"
    assert fake_x.requests == [], "the alarm must never call X"
    assert {p: p.stat().st_mtime_ns for p in state.rglob("*")} == before
    assert list(workspace.iterdir()) == [], "the alarm delivers no files"


@pytest.mark.parametrize(
    "status", ["proposed", "backlog", "in-progress", "review", "blocked", "someday"]
)
def test_auth_health_rerun_dedupes_an_open_reauth_task(state, home, bundle, workspace, status):
    register(home, bundle, ALIAS, status="reauth_required")
    observed = run(state, workspace, "status")["output"]
    [created] = auth_health_plan(observed, workspace)["followups"]
    existing = {**created, "id": "fixture-reauth", "status": status, "terminal": False}
    result = auth_health_plan(observed, workspace, [existing])
    assert result == {
        "followups": [],
        "updates": [],
        "skipped": [{"alias": ALIAS, "task_ids": ["fixture-reauth"]}],
    }


def test_auth_health_escalates_verification_without_creating_a_second_task(
    state, home, bundle, workspace
):
    register(home, replace(bundle, expires_at=0), ALIAS)
    [verification] = auth_health_plan(run(state, workspace, "status")["output"], workspace)[
        "followups"
    ]
    existing = {**verification, "id": "fixture-verify", "status": "proposed", "terminal": False}
    register(home, bundle, ALIAS, status="reauth_required")
    observed = run(state, workspace, "status")["output"]
    result = auth_health_plan(observed, workspace, [existing])
    assert result["followups"] == []
    [update] = result["updates"]
    assert update["id"] == existing["id"] and update["priority"] == "high"
    assert "reauth_required" in update["title"]
    assert observed["attention"][0] in update["comment"]
    assert "description" not in update and "status" not in update


@pytest.mark.parametrize("terminal_status", ["done", "rejected"])
def test_auth_health_closed_tasks_do_not_suppress_a_new_alarm(
    state, home, bundle, workspace, terminal_status
):
    register(home, bundle, ALIAS, status="reauth_required")
    observed = run(state, workspace, "status")["output"]
    [created] = auth_health_plan(observed, workspace)["followups"]
    closed = {**created, "id": "fixture-closed", "status": terminal_status, "terminal": True}
    assert auth_health_plan(observed, workspace, [closed])["followups"] == [created]


def test_auth_health_dedupe_is_per_account_and_ignores_non_auth_attention(
    state, home, bundle, workspace
):
    register(home, bundle, ALIAS, status="reauth_required")
    other = "x:other"
    register(home, bundle, other)  # unverified, independently needs attention
    observed = run(state, workspace, "status")["output"]
    created = auth_health_plan(observed, workspace)["followups"]
    assert len(created) == 2
    existing = {**created[0], "id": "fixture-first", "terminal": False}
    [remaining] = auth_health_plan(observed, workspace, [existing])["followups"]
    assert f"pulsar-auth-health:{other}" in remaining["tags"]
    # Overall healthy can be false for a write issue while account health is healthy.
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    status = run(state, workspace, "status", {"account": ALIAS})["output"]
    status["healthy"] = False
    status["attention"] = [f"{ALIAS}: 1 write(s) with an unknown outcome"]
    assert auth_health_plan(status, workspace)["followups"] == []


def test_auth_health_refuses_truncated_task_lists(workspace):
    result = subprocess.run(
        [sys.executable, "-B", str(AUTH_PLANNER)],
        input=json.dumps(
            {
                "status": {"accounts": [], "attention": []},
                "tasks": {"tasks": [], "total": 1, "truncated": True},
            }
        ),
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    assert result.returncode != 0
    assert "complete task list is required" in result.stderr
    assert result.stdout == ""


# -- x-updates ------------------------------------------------------------------------

X_UPDATES = PLUGIN / "definitions" / "auto_tasks" / "x-updates.yaml"
X_PLANNER = PLUGIN / "skills" / "publish" / "scripts" / "x_updates.py"
POSTED = ROOT / "tests" / "fixtures" / "posted.jsonl"


def test_x_updates_definition_drafts_keyed_plans_and_never_publishes():
    definition = yaml.safe_load(X_UPDATES.read_text())
    assert definition["enabled"] is False and definition["dedupe"] == "skip_if_open"
    assert definition["schedule"] == {"cron": "0 10 * * *"}
    template = definition["template"]
    assert template["required_tools"] == ["pulsar.history", "pulsar.status", "pulsar.validate"]
    assert "no-diff-expected" in template["tags"], "nothing new delivers without a file"
    description = template["description"]
    for key in ("release:<repo>:<tag>", "repo:<name>", "pr:<repo>:<n>"):
        assert key in description
    assert "scripts/x_updates.py" in description and X_PLANNER.is_file()
    assert "`keys`" in description, "the ledger lookup goes through pulsar.history keys"
    assert "no file" in description.lower(), "nothing new writes no file (ORB-13375)"
    plan = re.search(r"```yaml\n(.*?)```", description, re.S)
    assert plan is not None and "key:" in plan.group(1) and "not_before" not in plan.group(1)


def x_updates(mode, request, workspace, *, check=True):
    result = subprocess.run(
        [sys.executable, "-B", str(X_PLANNER), mode],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=check,
        cwd=workspace,
    )
    return json.loads(result.stdout) if check else result


CANDIDATES = [
    {"kind": "pr", "repo": "orbit", "number": 41, "title": "Faster drains", "at": "2026-10-02"},
    {"kind": "pr", "repo": "example", "number": 5, "title": "Provenance", "at": "2026-10-01"},
    {"kind": "repo", "repo": "example", "at": "2026-09-30"},
    {"kind": "release", "repo": "orbit", "tag": "v0.21.0", "at": "2026-09-29"},
    {"kind": "release", "repo": "orbit", "tag": "v0.26.0", "at": "2026-10-01"},
    {"kind": "release", "repo": "pulsar", "tag": "v0.2.0", "at": "2026-10-02"},
    {"kind": "repo", "repo": "nebula", "at": "2026-10-02"},
    {"kind": "pr", "repo": "pulsar", "number": 7, "title": "Keyed plans", "at": "2026-10-03"},
]


def tasks_envelope(tasks):
    return {"tasks": tasks, "total": len(tasks), "truncated": False}


def test_x_updates_keys_are_the_routines_keys(workspace):
    out = x_updates("keys", {"candidates": CANDIDATES + CANDIDATES[:1]}, workspace)
    assert out["keys"] == [
        "pr:orbit:41",
        "pr:example:5",
        "repo:example",
        "release:orbit:v0.21.0",
        "release:orbit:v0.26.0",
        "release:pulsar:v0.2.0",
        "repo:nebula",
        "pr:pulsar:7",
    ]
    for bad in (
        {"kind": "release", "repo": "constellation-works/orbit", "tag": "v1"},
        {"kind": "release", "repo": "orbit", "tag": "v 1"},
        {"kind": "pr", "repo": "orbit", "number": "41"},
        {"kind": "issue", "repo": "orbit"},
    ):
        failed = x_updates("keys", {"candidates": [bad]}, workspace, check=False)
        assert failed.returncode != 0 and failed.stdout == ""
    too_many = [{"kind": "pr", "repo": "orbit", "number": n} for n in range(1, 102)]
    assert x_updates("keys", {"candidates": too_many}, workspace, check=False).returncode != 0


def test_x_updates_skips_keys_in_the_ledger_or_already_drafted(
    state, home, bundle, workspace, fake_x
):
    register(home, bundle, ALIAS)
    # The retired routine's history, imported: published and skipped keys alike.
    asyncio.run(make_app(home, transport=fake_x.transport()).import_posted(POSTED, confirm=True))
    keys = x_updates("keys", {"candidates": CANDIDATES}, workspace)["keys"]
    history = run(state, workspace, "history", {"keys": keys, "limit": 100})["output"]
    assert {row["key"] for row in history["rows"]} == {
        "pr:example:5",  # published by the routine
        "repo:example",  # published by the routine
        "release:orbit:v0.21.0",  # skipped by the routine (a baseline)
    }
    assert history["truncated"] is False
    # Drafted by an earlier run: a delivered plan file, and an open task not yet delivered.
    (workspace / "x-updates" / "2026-10-01").mkdir(parents=True)
    (workspace / "x-updates" / "2026-10-01" / "release-orbit-v0.26.0.yaml").write_text(
        f'account: {ALIAS}\nkey: "release:orbit:v0.26.0"\ntext: "Orbit v0.26.0"\n'
    )
    open_task = {
        "id": "fixture-open",
        "terminal": False,
        "description": "- idempotency key `release:pulsar:v0.2.0`, plan x-updates/...",
    }
    closed_task = {"id": "fixture-done", "terminal": True, "description": "`repo:nebula`"}
    before = sorted(p for p in workspace.rglob("*"))
    out = x_updates(
        "plan",
        {
            "candidates": CANDIDATES,
            "history": history,
            "tasks": tasks_envelope([open_task, closed_task]),
            "date": "2026-10-03",
        },
        workspace,
    )
    assert sorted(p for p in workspace.rglob("*")) == before, "the helper writes nothing"
    skipped = {s["key"]: s["reason"] for s in out["skipped"]}
    drafted = "x-updates/2026-10-01/release-orbit-v0.26.0.yaml"
    assert skipped == {
        "pr:example:5": "in the ledger (published, import:posted.jsonl)",
        "repo:example": "in the ledger (published, import:posted.jsonl)",
        "release:orbit:v0.21.0": "in the ledger (skipped, import:posted.jsonl)",
        "release:orbit:v0.26.0": f"already drafted in {drafted}",
        "release:pulsar:v0.2.0": "an open task lists it",
    }
    # Three at most, releases before repos before PRs, oldest first; the rest wait.
    assert [(d["key"], d["plan"]) for d in out["drafts"]] == [
        ("repo:nebula", "x-updates/2026-10-03/repo-nebula.yaml"),
        ("pr:orbit:41", "x-updates/2026-10-03/pr-orbit-41.yaml"),
        ("pr:pulsar:7", "x-updates/2026-10-03/pr-pulsar-7.yaml"),
    ]
    assert out["drafts"][1]["title"] == "Faster drains"
    assert out["deferred"] == []
    capped = x_updates(
        "plan",
        {"candidates": CANDIDATES, "history": history, "tasks": tasks_envelope([]),
         "date": "2026-10-03", "max_drafts": 1},
        workspace,
    )  # fmt: skip
    assert [d["key"] for d in capped["drafts"]] == ["release:pulsar:v0.2.0"]
    assert capped["deferred"] == ["repo:nebula", "pr:orbit:41", "pr:pulsar:7"]


def test_x_updates_with_nothing_new_drafts_nothing(state, home, bundle, workspace, fake_x):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    asyncio.run(make_app(home, transport=fake_x.transport()).import_posted(POSTED, confirm=True))
    known = [{"kind": "repo", "repo": "example"}, {"kind": "pr", "repo": "example", "number": 4}]
    keys = x_updates("keys", {"candidates": known}, workspace)["keys"]
    history = run(state, workspace, "history", {"keys": keys})["output"]
    out = x_updates(
        "plan", {"candidates": known, "history": history, "tasks": tasks_envelope([])}, workspace
    )
    assert out["drafts"] == [] and out["deferred"] == [] and len(out["skipped"]) == 2
    empty = {"rows": [], "total": 0, "truncated": False}
    out = x_updates("plan", {"candidates": [], "history": empty, "tasks": tasks_envelope([])},
                    workspace)  # fmt: skip
    assert out == {"drafts": [], "skipped": [], "deferred": []}
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    ("history", "tasks"),
    [
        ({"rows": [], "total": 1, "truncated": True}, tasks_envelope([])),
        (
            {"rows": [], "total": 0, "truncated": False},
            {"tasks": [], "total": 1, "truncated": True},
        ),
    ],
)
def test_x_updates_refuses_an_incomplete_lookup(workspace, history, tasks):
    request = {"candidates": CANDIDATES, "history": history, "tasks": tasks}
    failed = x_updates("plan", request, workspace, check=False)
    assert failed.returncode != 0 and failed.stdout == ""


def test_history_looks_up_keys_however_old(state, home, bundle, workspace, fake_x):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    asyncio.run(make_app(home, transport=fake_x.transport()).import_posted(POSTED, confirm=True))
    newest = run(state, workspace, "history", {"limit": 1})["output"]
    assert newest["truncated"] is True
    out = run(state, workspace, "history", {"keys": ["release:orbit:v0.20.0", "pr:none:1"]})
    [row] = out["output"]["rows"]
    assert row["key"] == "release:orbit:v0.20.0" and row["state"] == "skipped"
    assert out["output"]["total"] == 1 and out["output"]["truncated"] is False
    other = run(state, workspace, "history", {"keys": ["release:orbit:v0.20.0"], "account": ALIAS})
    assert other["output"]["total"] == 1
    for bad in ([], ["k"] * 101, [""], "release:orbit:v0.20.0", [7]):
        response = run(state, workspace, "history", {"keys": bad})
        assert response["error"]["code"] == "invalid_argument", bad
