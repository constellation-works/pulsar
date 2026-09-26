"""Operator verbs (``surfaces/ops.py``) and the policy on the legacy ``create_post``."""

import json
from pathlib import Path

import pytest

from pulsar.core.ledger import Ledger
from pulsar.surfaces.cli import main
from pulsar.surfaces.mcp import Runtime
from pulsar.surfaces.ops import (
    budget_report,
    history_report,
    import_report,
    publish_report,
    reconcile_report,
    validate_report,
)

from .conftest import ALIAS, SECRETS
from .test_ledger import call, open_session

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "posted.jsonl"

THREAD = """\
account: x:constworks
posts:
  - text: "Orbit v0.26 is out"
  - text: "Notes: https://example.com/notes"
"""


def _plan(tmp_path, text=THREAD) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(text)
    return path


def _config(paths, text):
    paths.ensure()
    paths.settings_file.write_text(text)


def _clean(out) -> None:
    blob = json.dumps(out)
    assert not any(s in blob for s in SECRETS)


async def test_validate_is_offline_and_reports_digest_and_cost(paths, authed, fake_x, tmp_path):
    out, code = await validate_report(paths, _plan(tmp_path))
    assert code == 0 and fake_x.requests == []
    [report] = out["accounts"]
    assert report["account"] == ALIAS and report["digest"].startswith("sha256:")
    assert [p["has_url"] for p in report["posts"]] == [False, True]
    assert report["estimated_cost_usd"] == pytest.approx(0.015 + 0.2)


async def test_validate_needs_no_credentials(paths, tmp_path):
    out, code = await validate_report(paths, _plan(tmp_path))
    assert code == 0 and out["accounts"][0]["account"] == ALIAS


async def test_validate_binds_a_plan_without_accounts_to_the_default(paths, authed, tmp_path):
    bare = await validate_report(paths, _plan(tmp_path, 'text: "hello"'))
    named = await validate_report(paths, _plan(tmp_path, 'account: x:constworks\ntext: "hello"'))
    assert bare[0]["accounts"][0]["digest"] == named[0]["accounts"][0]["digest"]


async def test_validate_refuses_an_account_outside_the_plan(paths, authed, tmp_path):
    register_other = _plan(tmp_path, 'account: x:constworks\ntext: "hi"')
    from .conftest import register

    register(paths, None, "x:other")
    out, code = await validate_report(paths, register_other, account="x:other")
    assert code == 1 and out["code"] == "invalid_argument"


async def test_validate_reports_plan_errors(paths, authed, tmp_path):
    out, code = await validate_report(paths, _plan(tmp_path, "posts: []"))
    assert code == 1 and out["code"] == "invalid_plan"


async def test_publish_without_yes_only_validates(paths, authed, fake_x, tmp_path):
    out, code = await publish_report(paths, _plan(tmp_path), transport=fake_x.transport())
    assert code == 0 and out["published"] is False and "--yes" in out["note"]
    assert fake_x.requests == []
    assert Ledger(paths).history() == []


async def test_publish_posts_a_thread_once_then_replays(paths, authed, fake_x, tmp_path):
    plan = _plan(tmp_path)
    out, code = await publish_report(paths, plan, yes=True, transport=fake_x.transport())
    _clean(out)
    assert code == 0
    [receipt] = out["results"]
    assert receipt["ok"] and receipt["state"] == "published" and not receipt["replayed"]
    first, second = receipt["items"]
    posts = fake_x.calls("POST", "/tweets")
    assert len(posts) == 2
    assert json.loads(posts[1].content)["reply"] == {"in_reply_to_tweet_id": first["post_id"]}
    assert second["url"] == f"https://x.com/constworks/status/{second['post_id']}"

    again, code = await publish_report(paths, plan, yes=True, transport=fake_x.transport())
    assert code == 0 and again["results"][0]["replayed"] is True
    assert len(fake_x.calls("POST", "/tweets")) == 2

    history, _ = history_report(paths)
    [row] = history["writes"]
    assert row["tool"] == "publish" and row["caller"] == "pulsar-cli"
    assert [i["state"] for i in row["items"]] == ["published", "published"]


async def test_publish_refuses_a_key_for_several_accounts(paths, authed, fake_x, tmp_path):
    from .conftest import register

    register(paths, None, "x:other")
    plan = _plan(tmp_path, 'accounts: [x:constworks, x:other]\ntext: "hi"')
    out, code = await publish_report(
        paths, plan, yes=True, idempotency_key="k", transport=fake_x.transport()
    )
    assert code == 1 and out["code"] == "invalid_argument"
    assert fake_x.requests == []


async def test_status_counts_what_the_ledger_committed(paths, authed, fake_x, tmp_path):
    await publish_report(paths, _plan(tmp_path), yes=True, transport=fake_x.transport())
    requests = len(fake_x.requests)
    out, code = budget_report(paths)
    assert code == 0 and len(fake_x.requests) == requests, "status is offline"
    [entry] = out["accounts"]
    assert entry["alias"] == ALIAS
    assert entry["posts"] == {"used": 2, "cap": 5, "remaining": 3}
    assert entry["day"]["spent_usd"] == pytest.approx(0.215)
    assert entry["day"]["budget_usd"] == 1.0 and entry["month"]["budget_usd"] == 10.0
    assert entry["quiet"]["active"] is False and entry["unresolved"] == []


async def test_reconcile_with_nothing_unresolved_makes_no_call(paths, authed, fake_x):
    out, code = await reconcile_report(paths, transport=fake_x.transport())
    assert code == 0 and out == {"account": ALIAS, "results": []}
    assert fake_x.requests == []


async def test_import_posted_is_idempotent_and_blocks_reposts(paths, authed, fake_x, tmp_path):
    out, code = await import_report(paths, FIXTURE, transport=fake_x.transport())
    assert code == 0 and out["account"] == ALIAS
    assert out["imported_published"] + out["imported_skipped"] > 0
    assert fake_x.calls("POST", "/tweets") == []
    again, code = await import_report(paths, FIXTURE, transport=fake_x.transport())
    assert code == 0 and again["imported_published"] == again["imported_skipped"] == 0
    assert again["already_present"] == out["imported_published"] + out["imported_skipped"]


async def test_import_refuses_a_missing_file(paths, authed, fake_x, tmp_path):
    out, code = await import_report(paths, tmp_path / "nope.jsonl", transport=fake_x.transport())
    assert code == 1 and out["code"] == "invalid_argument"


def test_cli_validate_prints_json(paths, authed, tmp_path, capsys):
    assert main(["validate", str(_plan(tmp_path))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["accounts"][0]["account"] == ALIAS


def test_cli_status_and_history(paths, authed, capsys):
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["accounts"][0]["alias"] == ALIAS
    assert main(["history", "--limit", "5"]) == 0
    assert json.loads(capsys.readouterr().out) == {"writes": []}


# -- policy on the legacy tool ------------------------------------------------------


async def test_create_post_stops_at_the_daily_cap(paths, authed, fake_x):
    _config(paths, "[policy]\nmax_posts_per_day = 1\n")
    rt = Runtime(paths, transport=fake_x.transport())
    try:
        async with open_session(rt) as s:
            first = await call(s, "create_post", {"text": "one"})
            second = await call(s, "create_post", {"text": "two"})
    finally:
        await rt.aclose()
    assert first["ok"] is True
    assert second["ok"] is False and second["code"] == "daily_cap" and second["retryable"]
    assert len(fake_x.calls("POST", "/tweets")) == 1


async def test_create_post_refuses_over_budget_before_any_post(paths, authed, fake_x):
    _config(paths, "[policy]\ndaily_budget_usd = 0.1\n")
    rt = Runtime(paths, transport=fake_x.transport())
    try:
        async with open_session(rt) as s:
            out = await call(s, "create_post", {"text": "see https://example.com"})
    finally:
        await rt.aclose()
    assert out["code"] == "budget_exceeded"
    assert fake_x.calls("POST", "/tweets") == []
    assert Ledger(paths).history() == [], "a refused write leaves no row"


async def test_validate_plan_tool_is_offline(paths, authed, fake_x):
    runtime = Runtime(paths, transport=fake_x.transport())
    try:
        async with open_session(runtime) as s:
            plan = {"posts": [{"text": "a"}, {"text": "b"}]}
            out = await call(s, "validate_plan", {"plan": plan})
    finally:
        await runtime.aclose()
    assert out["ok"] is True and fake_x.requests == []
    [report] = out["accounts"]
    assert report["account"] == ALIAS and len(report["posts"]) == 2
