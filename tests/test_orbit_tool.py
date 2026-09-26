"""The Orbit exec backend (``surfaces/orbit_tool.py``) and the plugin manifest around it.

These run the backend in-process; ``orbit plugin test .`` runs the same goldens
(``tests/conformance/``) through Orbit and its sandbox.
"""

import asyncio
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from pulsar.core.paths import Paths
from pulsar.surfaces import orbit_tool
from pulsar.surfaces.cli import main as cli_main
from pulsar.surfaces.ops import publish_report

from .conftest import ALIAS, SECRETS, register
from .media_samples import PNG
from .test_server import SECRET_PARAM

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text())
GOLDENS = yaml.safe_load((ROOT / "tests" / "conformance" / "pulsar.yaml").read_text())["tests"]


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    # PULSAR_HOME must not win over the plugin state; point it somewhere wrong.
    monkeypatch.setenv("PULSAR_HOME", str(tmp_path / "not-this-home"))
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
    state: Path, workspace: Path | None, tool: str, input_: Any = None, **envelope: Any
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
    code = orbit_tool.main(
        io.StringIO(json.dumps(request)), out, {"ORBIT_PLUGIN_STATE": str(state)}
    )
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
    schema = json.loads((ROOT / "schemas" / f"{tool}.{kind}.json").read_text())
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
    for tool in MANIFEST["spec"]["tools"]:
        assert tool["execution_kind"] == "read_only", "phase 3 ships no write tool"
        assert tool["input_schema"] == {"$ref": f"schemas/{tool['name']}.request.json"}
        assert tool["output_schema"] == {"$ref": f"schemas/{tool['name']}.response.json"}


def test_no_request_schema_takes_a_credential():
    for path in (ROOT / "schemas").glob("*.request.json"):
        schema = json.loads(path.read_text())
        assert schema.get("additionalProperties") is False, path.name
        for prop in schema["properties"]:
            assert not SECRET_PARAM.search(prop), f"{path.name}: {prop}"
    config = json.loads((ROOT / "schemas" / "config.json").read_text())
    assert config["additionalProperties"] is False and not config.get("properties")


def test_launcher_and_skill_ship_in_the_tree():
    launcher = ROOT / MANIFEST["spec"]["backend"]["command"]
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert "pulsar.surfaces.orbit_tool" in launcher.read_text()
    for skill in MANIFEST["spec"]["skills"]:
        text = (ROOT / skill / "SKILL.md").read_text()
        assert re.search(r"(?m)^name: pulsar-", text)
    # The plugin installer refuses symlinks anywhere in the tree.
    assert not (ROOT / "CLAUDE.md").is_symlink()


# -- envelope -------------------------------------------------------------------------


def _raw(text: str) -> dict[str, Any]:
    out = io.StringIO()
    orbit_tool.main(io.StringIO(text), out, {"ORBIT_PLUGIN_STATE": "/nonexistent"})
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
    async def boom(paths, call):
        raise RuntimeError("secret detail that must not leak")

    monkeypatch.setitem(orbit_tool.TOOLS, "status", boom)
    response = run(state, workspace, "status")
    assert response["error"] == {
        "code": "api_error",
        "message": "unexpected RuntimeError",
        "retryable": False,
    }


def test_the_home_is_the_plugin_state_not_pulsar_home(state, home, workspace):
    register(home, None, ALIAS)
    [account] = run(state, workspace, "status")["output"]["accounts"]
    assert account["alias"] == ALIAS


def test_cli_orbit_tool_reads_stdin(paths, monkeypatch, capsys):
    monkeypatch.delenv("ORBIT_PLUGIN_STATE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schema_version": 1, "tool": "pulsar.history"}'))
    assert cli_main(["orbit-tool"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "output": {"rows": []}}


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
    assert out["attention"] == [f"{ALIAS}: re-authorization required (`pulsar auth login`)"]


def test_history_and_status_see_what_was_published(
    state, home, bundle, workspace, fake_x, tmp_path
):
    register(home, bundle, ALIAS, handle="constworks", provider_user_id="1")
    plan = tmp_path / "plan.yaml"
    plan.write_text('account: x:constworks\nposts: [{text: "one"}, {text: "two"}]\n')
    receipt, code = asyncio.run(publish_report(home, plan, yes=True, transport=fake_x.transport()))
    assert code == 0
    requests = len(fake_x.requests)

    [row] = run(state, workspace, "history", {"limit": 5})["output"]["rows"]
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
    (workspace / "big.yaml").write_bytes(b"#" * (orbit_tool.SOURCE_MAX_BYTES + 1))
    (workspace / "bin.yaml").write_bytes(b"\xff\xfe")
    for name in ("big.yaml", "bin.yaml", "missing.yaml"):
        response = run(state, workspace, "validate", {"source": name})
        assert response["error"]["code"] == "invalid_argument", name


def test_validate_source_needs_a_workspace(state):
    response = run(state, None, "validate", {"source": "plan.yaml"})
    assert response["error"]["code"] == "invalid_argument"


def test_validate_restores_the_working_directory(state, workspace):
    before = os.getcwd()
    run(state, workspace, "validate", {"plan": {"account": ALIAS, "text": "hi"}})
    assert os.getcwd() == before
