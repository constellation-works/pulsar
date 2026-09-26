"""``pulsar`` through its entry point: the output and error contract, the auth verbs,
the operator verbs, and the help text (goldens)."""

import json
import os
import sys
from pathlib import Path

import pytest

from pulsar.core.accounts import AccountRegistry
from pulsar.core.ledger import SCHEMA_VERSION, Ledger
from pulsar.providers.x import auth as x_auth
from pulsar.surfaces.cli import build_parser, main

from .conftest import ALIAS, SECRETS, register

FIXTURE = Path(__file__).parent / "fixtures" / "posted.jsonl"
GOLDENS = Path(__file__).parent / "goldens" / "help"
UPDATE = os.environ.get("PULSAR_UPDATE_GOLDENS") == "1"

THREAD = """\
account: x:constworks
posts:
  - text: "Orbit v0.26 is out"
  - text: "Notes: https://example.com/notes"
"""


def _plan(tmp_path, text=THREAD) -> str:
    path = tmp_path / "plan.yaml"
    path.write_text(text)
    return str(path)


def _verified(paths, bundle, alias=ALIAS, handle="constworks", user_id="1234567890", **row):
    return register(paths, bundle, alias, handle=handle, provider_user_id=user_id, **row)


def _run(capsys, argv, **kwargs):
    code = main(argv, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _error(err: str) -> dict:
    """The one JSON error line on stderr (notices before it are prose)."""
    [line] = [ln for ln in err.splitlines() if ln.startswith("{")]
    body = json.loads(line)
    assert set(body) == {"error", "code", "retryable", "detail"}
    return body


# -- the output contract -------------------------------------------------------------


def test_a_failed_command_writes_json_to_stderr_and_nothing_to_stdout(paths, bundle, capsys):
    _verified(paths, bundle)
    code, out, err = _run(capsys, ["auth", "status", "--account", "x:nobody"])
    assert code == 1 and out == ""
    body = _error(err)
    assert body["code"] == "unknown_account" and "x:constworks" in body["error"]


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["history", "--limit", "0"], "must be 1..100"),
        (["publish"], "required: plan"),
        (["nope"], "invalid choice"),
    ],
)
def test_a_parse_error_is_the_same_json_error_and_exits_2(capsys, argv, says):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    body = _error(captured.err)
    assert body["code"] == "invalid_argument" and body["retryable"] is False
    assert says in body["error"] and body["detail"]["usage"].startswith("usage: pulsar")


@pytest.mark.parametrize("flag", [["--port", "9000"], ["--host", "::1"]])
def test_serve_refuses_http_flags_on_stdio(paths, capsys, flag):
    code, out, err = _run(capsys, ["serve", *flag])
    assert code == 2 and out == ""
    assert "--transport http" in _error(err)["error"]


def test_an_unexpected_exception_is_internal(paths, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("pulsar.surfaces.ops.budget_report", boom)
    code, out, err = _run(capsys, ["status"])
    assert code == 1 and out == ""
    body = _error(err)
    assert body["code"] == "internal" and body["retryable"] is False
    assert "RuntimeError" in body["error"]


def test_a_closed_stdout_exits_0(paths, authed, monkeypatch):
    read_end, write_end = os.pipe()
    os.close(read_end)
    saved = os.dup(1)
    stream = os.fdopen(write_end, "w")
    monkeypatch.setattr(sys, "stdout", stream)
    try:
        assert main(["status"]) == 0
    finally:
        os.dup2(saved, 1)
        os.close(saved)


def test_json_is_accepted_everywhere(paths, authed, capsys):
    code, out, _ = _run(capsys, ["--json", "status", "--json"])
    assert code == 0 and json.loads(out)["accounts"][0]["alias"] == ALIAS


def test_an_empty_result_says_so_on_stderr(paths, capsys):
    code, out, err = _run(capsys, ["history"])
    assert code == 0 and json.loads(out) == {"writes": [], "total": 0, "truncated": False}
    assert "no ledger rows" in err


# -- auth ----------------------------------------------------------------------------


@pytest.fixture
def fake_login(monkeypatch, bundle, fake_x):
    """Skip the browser: the code exchange yields ``bundle``; /users/me is the fake X."""
    real_fetch = x_auth.fetch_identity
    monkeypatch.setattr(x_auth, "authorize", lambda client_id, **_: bundle)
    monkeypatch.setattr(
        x_auth,
        "fetch_identity",
        lambda b, transport=None: real_fetch(b, transport=fake_x.transport()),
    )
    return fake_x


def _login(capsys, alias="x:constworks"):
    return _run(capsys, ["auth", "login", "--account", alias, "--client-id", "cid", "--no-browser"])


def test_login_binds_the_named_account_after_asking_x(paths, store, fake_login, capsys):
    code, out, _ = _login(capsys)
    assert code == 0
    row = AccountRegistry(paths).get(ALIAS)
    assert (row.handle, row.provider_user_id, row.status) == ("constworks", "1234567890", "active")
    assert row.binding_id and row.binding_id == store.load().binding_id
    assert row.bound_at and row.verified_at
    assert json.loads(out)["verified"] is True
    assert not any(s in out for s in SECRETS)
    assert fake_login.calls("GET", "/users/me")


def test_login_refuses_a_token_for_another_handle_and_stores_nothing(
    paths, store, fake_login, capsys
):
    fake_login.username = "someoneelse"
    code, out, err = _login(capsys)
    assert code == 1 and out == ""
    body = _error(err)
    assert body["code"] == "account_mismatch"
    assert "@constworks" in body["error"] and "@someoneelse" in body["error"]
    assert not store.token_file.exists()
    assert AccountRegistry(paths).accounts() == {}
    assert not paths.client_file.exists(), "a refused login leaves nothing behind"


def test_login_needs_an_account(paths, fake_login, capsys):
    code, out, err = _run(capsys, ["auth", "login", "--client-id", "cid", "--no-browser"])
    assert code == 2 and out == ""
    assert "--account" in _error(err)["error"]


def test_auth_status_is_offline_and_reports_health(paths, bundle, fake_x, capsys):
    _verified(paths, bundle)
    code, out, _ = _run(capsys, ["auth", "status"], transport=fake_x.transport())
    assert code == 0 and fake_x.requests == []
    assert json.loads(out)["accounts"][0]["health"] == "healthy"


def test_offline_is_a_deprecated_no_op(paths, bundle, fake_x, capsys):
    _verified(paths, bundle)
    code, out, err = _run(capsys, ["auth", "status", "--offline"], transport=fake_x.transport())
    assert code == 0 and fake_x.requests == [] and "deprecated" in err


def test_live_and_offline_are_mutually_exclusive(paths):
    with pytest.raises(SystemExit) as exc:
        main(["auth", "status", "--live", "--offline"])
    assert exc.value.code == 2


def test_logout_needs_confirm(paths, bundle, store, capsys):
    _verified(paths, bundle)
    code, out, err = _run(capsys, ["auth", "logout", "--account", ALIAS])
    assert code == 2 and out == "" and "--confirm" in _error(err)["error"]
    assert store.load() == bundle


def test_logout_without_confirm_leaves_a_legacy_home_untouched(paths, bundle, legacy_store, capsys):
    legacy_store.save(bundle)
    paths.settings_file.write_text(f'default_account = "{ALIAS}"\n')
    paths.settings_file.chmod(0o600)
    before = sorted(p.name for p in paths.home.iterdir())
    code, out, err = _run(capsys, ["auth", "logout"])
    assert code == 2 and out == "" and "--confirm" in _error(err)["error"]
    assert "migrated" not in err
    assert sorted(p.name for p in paths.home.iterdir()) == before


def test_logout_deletes_the_tokens_and_keeps_the_row_as_revoked(paths, bundle, store, capsys):
    _verified(paths, bundle)
    code, out, _ = _run(capsys, ["auth", "logout", "--account", ALIAS, "--confirm"])
    assert code == 0 and json.loads(out)["tokens_removed"] is True
    assert store.load() is None
    row = AccountRegistry(paths).get(ALIAS)
    assert row.status == "revoked" and row.handle == "constworks"
    code, out, _ = _run(capsys, ["auth", "logout", "--account", ALIAS, "--confirm"])
    assert code == 0 and json.loads(out)["tokens_removed"] is False, "says what it did"


def test_auth_migrate_moves_legacy_credentials(paths, bundle, legacy_store, store, capsys):
    legacy_store.save(bundle)
    code, out, _ = _run(capsys, ["auth", "migrate", "--account", ALIAS])
    preview = json.loads(out)
    assert code == 0 and preview["applied"] is False and preview["state"] == "pending"
    assert preview["alias"] == ALIAS and "--confirm" in preview["message"]
    assert paths.token_file.exists() and store.load() is None, "a report moves nothing (§R5)"
    code, out, _ = _run(capsys, ["auth", "migrate", "--account", ALIAS, "--confirm"])
    report = json.loads(out)
    assert code == 0 and report["applied"] is True and report["state"] == "migrated"
    assert store.load() == bundle and not paths.token_file.exists()
    code, out, _ = _run(capsys, ["auth", "migrate", "--account", ALIAS, "--confirm"])
    assert code == 0 and json.loads(out)["state"] == "none", "idempotent"


def test_migrate_upgrades_the_home(paths, bundle, legacy_store, store, capsys):
    legacy_store.save(bundle)
    paths.settings_file.write_text(f'default_account = "{ALIAS}"\n')
    paths.settings_file.chmod(0o600)
    code, out, _ = _run(capsys, ["migrate"])
    preview = json.loads(out)
    assert code == 0 and preview["applied"] is False and "--confirm" in preview["note"]
    assert preview["credentials"]["state"] == "pending"
    assert preview["credentials"]["alias"] == ALIAS
    assert preview["ledger"] == {"from_version": 0, "to_version": SCHEMA_VERSION}
    assert paths.token_file.exists() and not paths.ledger_db.exists(), "a report changes nothing"
    code, out, _ = _run(capsys, ["migrate", "--confirm"])
    report = json.loads(out)
    assert code == 0 and report["applied"] is True
    assert report["credentials"]["state"] == "migrated"
    assert report["ledger"] == {"from_version": 0, "to_version": SCHEMA_VERSION}
    assert store.load() == bundle
    code, out, _ = _run(capsys, ["migrate", "--confirm"])
    assert code == 0 and json.loads(out)["credentials"]["state"] == "none"  # idempotent
    code, out, _ = _run(capsys, ["migrate"])
    assert json.loads(out)["note"] == "nothing to migrate"


def test_migrate_says_when_nothing_names_the_legacy_account(paths, bundle, legacy_store, capsys):
    legacy_store.save(bundle)
    code, out, _ = _run(capsys, ["migrate", "--confirm"])
    report = json.loads(out)
    assert code == 1 and report["credentials"]["state"] == "needs_alias"
    assert "pulsar auth migrate --account" in report["credentials"]["message"]
    assert paths.token_file.exists(), "nothing moved"


# -- operator verbs ------------------------------------------------------------------


def test_validate_prints_json(paths, authed, tmp_path, capsys):
    code, out, _ = _run(capsys, ["validate", _plan(tmp_path)])
    assert code == 0 and json.loads(out)["accounts"][0]["account"] == ALIAS


def test_publish_without_confirm_sends_nothing(paths, authed, fake_x, tmp_path, capsys):
    code, out, _ = _run(capsys, ["publish", _plan(tmp_path)], transport=fake_x.transport())
    assert code == 0 and json.loads(out)["published"] is False
    assert fake_x.requests == []


@pytest.mark.parametrize(
    ("extra", "code_"),
    [
        (["--idempotency-key", "bad key with spaces"], "invalid_argument"),
        (["--caller", "ghp_" + "a" * 36], "secret_detected"),
    ],
)
def test_publish_without_confirm_checks_what_the_live_run_checks(
    paths, authed, fake_x, tmp_path, capsys, extra, code_
):
    code, out, err = _run(capsys, ["publish", _plan(tmp_path), *extra])
    assert code == 1 and out == "" and _error(err)["code"] == code_
    assert fake_x.requests == []


def test_publish_without_confirm_refuses_a_plan_that_is_not_due(
    paths, authed, fake_x, tmp_path, capsys
):
    plan = _plan(tmp_path, THREAD + "not_before: 2099-01-01T00:00:00Z\n")
    code, out, err = _run(capsys, ["publish", plan])
    assert code == 1 and out == "" and _error(err)["code"] == "not_due"
    assert not paths.ledger_db.exists(), "a preview creates no ledger"


def test_publish_with_confirm_posts_and_records(paths, authed, fake_x, tmp_path, capsys):
    argv = ["publish", _plan(tmp_path), "--confirm"]
    code, out, _ = _run(capsys, argv, transport=fake_x.transport())
    [receipt] = json.loads(out)["results"]
    assert code == 0 and receipt["state"] == "published"
    assert len(fake_x.calls("POST", "/tweets")) == 2
    [row] = Ledger(paths).history()
    assert row.caller == "pulsar-cli"


def test_publish_labels_writes_with_pulsar_caller(paths, authed, fake_x, tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_CALLER", "routine:release-notes")
    assert main(["publish", _plan(tmp_path), "--confirm"], transport=fake_x.transport()) == 0
    [row] = Ledger(paths).history()
    assert row.caller == "routine:release-notes"


def test_yes_is_a_deprecated_alias_of_confirm(paths, authed, fake_x, tmp_path, capsys):
    argv = ["publish", _plan(tmp_path), "--yes"]
    code, out, err = _run(capsys, argv, transport=fake_x.transport())
    assert code == 0 and json.loads(out)["published"] is True and "deprecated" in err


def test_reconcile_with_nothing_unresolved(paths, authed, fake_x, capsys):
    code, out, _ = _run(capsys, ["reconcile"], transport=fake_x.transport())
    assert code == 0 and json.loads(out) == {
        "account": ALIAS,
        "home": str(paths.home),
        "results": [],
    }
    assert fake_x.requests == []


def test_import_posted_needs_confirm_to_write(paths, authed, fake_x, capsys):
    argv = ["import-posted", str(FIXTURE)]
    code, out, _ = _run(capsys, argv, transport=fake_x.transport())
    assert code == 0 and json.loads(out)["applied"] is False
    assert Ledger(paths).history() == []
    code, out, _ = _run(capsys, [*argv, "--confirm"], transport=fake_x.transport())
    assert code == 0 and json.loads(out)["applied"] is True
    assert Ledger(paths).history() != []


def test_serve_binds_only_loopback():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["serve", "--transport", "http", "--host", "0.0.0.0"])
    assert exc.value.code == 2


# -- help (STD-01 §R24) -------------------------------------------------------------


def _commands():
    """Every command path in the tree, e.g. (), ("auth",), ("auth", "login")."""
    import argparse

    def walk(parser, prefix):
        yield prefix
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield from walk(child, (*prefix, name))

    return list(walk(build_parser(), ()))


COMMANDS = _commands()
assert COMMANDS, "the command tree is empty"


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: " ".join(c) or "pulsar")
def test_help_matches_its_golden(command, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "100")
    with pytest.raises(SystemExit) as exc:
        main([*command, "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    golden = GOLDENS / (("-".join(command) or "pulsar") + ".txt")
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text)
    assert golden.exists(), f"no golden for {command}; run with PULSAR_UPDATE_GOLDENS=1"
    assert text == golden.read_text(), f"help for {command} changed; review and regenerate"


def test_every_option_has_help():
    import argparse

    def walk(parser, prefix):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield from walk(child, f"{prefix} {name}")
            elif action.help is None:
                yield f"{prefix} {'/'.join(action.option_strings) or action.dest}"

    missing = list(walk(build_parser(), "pulsar"))
    assert missing == [], f"options without help text: {missing}"


# -- machine output goldens (STD-01 §R24) ---------------------------------------------

OUTPUT_GOLDENS = Path(__file__).parent / "goldens" / "cli"

OUTPUT_CASES = {
    "history-empty": (["history"], 0, "out"),
    "validate-thread": (["validate", "{plan}"], 0, "out"),
    "publish-preview": (["publish", "{plan}"], 0, "out"),
    "migrate-preview": (["migrate"], 0, "out"),
    "status-unknown-account": (["auth", "status", "--account", "x:nobody"], 1, "err"),
}


@pytest.mark.parametrize("name", sorted(OUTPUT_CASES))
def test_machine_output_matches_its_golden(name, paths, bundle, fake_x, tmp_path, capsys):
    argv, want_code, stream = OUTPUT_CASES[name]
    _verified(paths, bundle)
    plan = _plan(tmp_path)
    argv = [a.replace("{plan}", plan) for a in argv]
    code, out, err = _run(capsys, argv, transport=fake_x.transport())
    assert code == want_code
    text = (out if stream == "out" else err).replace(str(paths.home), "<home>")
    text = text.replace(plan, "<plan>")
    document = json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    golden = OUTPUT_GOLDENS / f"{name}.json"
    if UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(document)
    assert golden.exists(), f"no golden for {name}; run with PULSAR_UPDATE_GOLDENS=1"
    assert document == golden.read_text(), f"`pulsar {' '.join(argv)}` output changed"
    assert fake_x.requests == [], "none of these reach the network"
