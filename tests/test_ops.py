"""Operator verbs (``app/ops.py``) and the policy on the legacy ``create_post``."""

import json
from pathlib import Path

import pytest

from pulsar.app.core.account import AccountRegistry
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.ops import HISTORY_LIMIT_MAX
from pulsar.internal.errors import OutcomeUnknown, PulsarError

from .conftest import ALIAS, SECRETS, make_app, make_runtime, register
from .test_ledger import call, claim_plan, open_session

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
    paths.settings_file.chmod(0o600)  # config.toml must not be group/world-writable


def _clean(out) -> None:
    blob = json.dumps(out)
    assert not any(s in blob for s in SECRETS)


async def test_validate_is_offline_and_reports_digest_and_cost(paths, authed, fake_x, tmp_path):
    out, code = await make_app(paths).validate(_plan(tmp_path))
    assert code == 0 and fake_x.requests == []
    [report] = out["accounts"]
    assert report["account"] == ALIAS and report["digest"].startswith("sha256:")
    assert [p["has_url"] for p in report["posts"]] == [False, True]
    assert report["estimated_cost_usd"] == pytest.approx(0.015 + 0.2)


async def test_validate_needs_no_credentials(paths, tmp_path):
    out, code = await make_app(paths).validate(_plan(tmp_path))
    assert code == 0 and out["accounts"][0]["account"] == ALIAS


async def test_validate_binds_a_plan_without_accounts_to_the_default(paths, authed, tmp_path):
    bare = await make_app(paths).validate(_plan(tmp_path, 'text: "hello"'))
    named = await make_app(paths).validate(_plan(tmp_path, 'account: x:constworks\ntext: "hello"'))
    assert bare[0]["accounts"][0]["digest"] == named[0]["accounts"][0]["digest"]


async def test_validate_refuses_an_account_outside_the_plan(paths, authed, tmp_path):
    plan = _plan(tmp_path, 'account: x:constworks\ntext: "hi"')
    register(paths, None, "x:other")
    with pytest.raises(PulsarError) as exc:
        await make_app(paths).validate(plan, account="x:other")
    assert exc.value.code == "invalid_argument"


async def test_validate_reports_plan_errors(paths, authed, tmp_path):
    with pytest.raises(PulsarError) as exc:
        await make_app(paths).validate(_plan(tmp_path, "posts: []"))
    assert exc.value.code == "invalid_plan"


async def test_a_missing_plan_file_names_the_path(paths, tmp_path):
    with pytest.raises(PulsarError) as exc:
        await make_app(paths).validate(tmp_path / "nope.yaml")
    assert exc.value.code == "invalid_argument" and exc.value.detail == {
        "plan": str(tmp_path / "nope.yaml")
    }


async def test_publish_without_confirm_only_validates(paths, authed, fake_x, tmp_path):
    out, code = await make_app(paths, transport=fake_x.transport()).publish(_plan(tmp_path))
    assert code == 0 and out["published"] is False and "--confirm" in out["note"]
    assert fake_x.requests == []
    assert SqliteLedger(paths).history() == []


async def test_publish_posts_a_thread_once_then_replays(paths, authed, fake_x, tmp_path):
    plan = _plan(tmp_path)
    out, code = await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    _clean(out)
    assert code == 0 and out["published"] is True
    [receipt] = out["results"]
    assert receipt["ok"] and receipt["state"] == "published" and not receipt["replayed"]
    assert receipt["error"] is None
    first, second = receipt["items"]
    posts = fake_x.calls("POST", "/tweets")
    assert len(posts) == 2
    assert json.loads(posts[1].content)["reply"] == {"in_reply_to_tweet_id": first["post_id"]}
    assert second["url"] == f"https://x.com/constworks/status/{second['post_id']}"

    again, code = await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    assert code == 0 and again["results"][0]["replayed"] is True
    assert len(fake_x.calls("POST", "/tweets")) == 2

    history, _ = make_app(paths).history()
    [row] = history["writes"]
    assert row["tool"] == "publish" and row["caller"] == "pulsar-cli"
    assert [i["state"] for i in row["items"]] == ["published", "published"]


async def test_publish_refuses_a_key_for_several_accounts(paths, authed, fake_x, tmp_path):
    register(paths, None, "x:other")
    plan = _plan(tmp_path, 'accounts: [x:constworks, x:other]\ntext: "hi"')
    with pytest.raises(PulsarError) as exc:
        await make_app(paths, transport=fake_x.transport()).publish(
            plan, confirm=True, idempotency_key="k"
        )
    assert exc.value.code == "invalid_argument"
    assert fake_x.requests == []


async def test_publish_prepares_every_account_before_sending_any(paths, authed, fake_x, tmp_path):
    register(paths, None, "x:other")  # bound in the registry, no credentials
    plan = _plan(tmp_path, 'accounts: [x:constworks, x:other]\ntext: "hi"')
    with pytest.raises(PulsarError) as exc:
        await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    assert exc.value.code == "auth_expired" and "x:other" in exc.value.message
    assert fake_x.calls("POST", "/tweets") == [], "the first account did not post either"
    assert SqliteLedger(paths).history() == []


async def test_status_counts_what_the_ledger_committed(paths, authed, fake_x, tmp_path):
    await make_app(paths, transport=fake_x.transport()).publish(_plan(tmp_path), confirm=True)
    requests = len(fake_x.requests)
    out, code = make_app(paths).status()
    assert code == 0 and len(fake_x.requests) == requests, "status is offline"
    [entry] = out["accounts"]
    assert entry["alias"] == ALIAS
    assert entry["posts"] == {"used": 2, "cap": 5, "remaining": 3}
    assert entry["day"]["spent_usd"] == pytest.approx(0.215)
    assert entry["day"]["budget_usd"] == 1.0 and entry["month"]["budget_usd"] == 10.0
    assert entry["quiet"]["active"] is False and entry["unresolved"] == []


async def test_reconcile_with_nothing_unresolved_makes_no_call(paths, authed, fake_x):
    out, code = await make_app(paths, transport=fake_x.transport()).reconcile()
    assert code == 0 and out == {"account": ALIAS, "home": str(paths.home), "results": []}
    assert fake_x.requests == []


async def test_import_without_confirm_reports_and_writes_nothing(paths, authed, fake_x):
    out, code = await make_app(paths, transport=fake_x.transport()).import_posted(FIXTURE)
    assert code == 0 and out["applied"] is False and "--confirm" in out["note"]
    assert out["imported_published"] + out["imported_skipped"] > 0
    assert fake_x.requests == [] and SqliteLedger(paths).history() == []


async def test_import_posted_is_idempotent_and_blocks_reposts(paths, authed, fake_x, tmp_path):
    out, code = await make_app(paths, transport=fake_x.transport()).import_posted(
        FIXTURE, confirm=True
    )
    assert code == 0 and out["account"] == ALIAS and out["applied"] is True
    assert out["imported_published"] + out["imported_skipped"] > 0
    assert fake_x.calls("POST", "/tweets") == []
    again, code = await make_app(paths, transport=fake_x.transport()).import_posted(
        FIXTURE, confirm=True
    )
    assert code == 0 and again["imported_published"] == again["imported_skipped"] == 0
    assert again["already_present"] == out["imported_published"] + out["imported_skipped"]


async def test_import_refuses_a_missing_file(paths, authed, fake_x, tmp_path):
    with pytest.raises(PulsarError) as exc:
        await make_app(paths, transport=fake_x.transport()).import_posted(
            tmp_path / "nope.jsonl", confirm=True
        )
    assert exc.value.code == "invalid_argument"


async def test_history_reports_total_and_truncated(paths, authed, fake_x, tmp_path):
    for n in range(3):
        plan = _plan(tmp_path, f'account: x:constworks\ntext: "post {n}"')
        await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    out, code = make_app(paths).history(limit=2)
    assert code == 0 and len(out["writes"]) == 2
    assert out["total"] == 3 and out["truncated"] is True
    out, _ = make_app(paths).history(limit=3)
    assert out["total"] == 3 and out["truncated"] is False


@pytest.mark.parametrize("limit", [0, -1, HISTORY_LIMIT_MAX + 1, True])
def test_history_refuses_an_out_of_range_limit(paths, limit):
    with pytest.raises(PulsarError) as exc:
        make_app(paths).history(limit=limit)
    assert exc.value.code == "invalid_argument", "a limit is refused, never clamped"


def _tree_modes(root: Path, dirs: int, files: int) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(dirs if path.is_dir() else files)


async def test_reports_run_against_a_read_only_home(paths, bundle, fake_x, tmp_path):
    register(paths, bundle, handle="constworks", provider_user_id=fake_x.user_id)
    await make_app(paths, transport=fake_x.transport()).publish(_plan(tmp_path), confirm=True)
    before = {p: p.stat().st_mtime_ns for p in paths.home.rglob("*")}
    _tree_modes(paths.home, 0o500, 0o400)
    try:
        status, code = make_app(paths).status()
        assert code == 0 and status["accounts"][0]["posts"]["used"] == 2
        history, code = make_app(paths).history()
        assert code == 0 and history["total"] == 1
        valid, code = await make_app(paths).validate(_plan(tmp_path))
        assert code == 0 and valid["published"] is False
    finally:
        _tree_modes(paths.home, 0o700, 0o600)
    assert {p: p.stat().st_mtime_ns for p in paths.home.rglob("*")} == before


# -- policy on the legacy tool ------------------------------------------------------


async def test_create_post_stops_at_the_daily_cap(paths, authed, fake_x):
    _config(paths, "[policy]\nmax_posts_per_day = 1\n")
    rt = make_runtime(paths, transport=fake_x.transport())
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
    rt = make_runtime(paths, transport=fake_x.transport())
    try:
        async with open_session(rt) as s:
            out = await call(s, "create_post", {"text": "see https://example.com"})
    finally:
        await rt.aclose()
    assert out["code"] == "budget_exceeded"
    assert fake_x.calls("POST", "/tweets") == []
    assert SqliteLedger(paths).history() == [], "a refused write leaves no row"


async def test_validate_plan_tool_is_offline(paths, authed, fake_x):
    runtime = make_runtime(paths, transport=fake_x.transport())
    try:
        async with open_session(runtime) as s:
            plan = {"posts": [{"text": "a"}, {"text": "b"}]}
            out = await call(s, "validate_plan", {"plan": plan})
    finally:
        await runtime.aclose()
    assert out["ok"] is True and fake_x.requests == []
    [report] = out["accounts"]
    assert report["account"] == ALIAS and len(report["posts"]) == 2


async def test_reconcile_marks_the_account_when_its_credentials_expired(paths, bundle, fake_x):
    register(paths, bundle, handle="constworks", provider_user_id="1234567890")
    ledger = SqliteLedger(paths)
    claim_plan(ledger, "lost", n=1)
    ledger.begin_item("lost", 0)
    ledger.item_unknown("lost", 0, OutcomeUnknown("ReadTimeout"))
    ledger.finish("lost")
    fake_x.fail_auth_once = True
    fake_x.refresh_status = 401
    with pytest.raises(PulsarError) as exc:
        await make_app(paths, transport=fake_x.transport()).reconcile()
    assert exc.value.code == "auth_expired"
    assert AccountRegistry(paths).get(ALIAS).status == "reauth_required"


async def test_publish_uses_the_plans_key_and_never_posts_it_twice(paths, authed, fake_x, tmp_path):
    keyed = 'account: x:constworks\nkey: "release:orbit:v0.26.0"\ntext: "Orbit v0.26 is out"\n'
    plan = _plan(tmp_path, keyed)
    out, code = await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    assert code == 0 and out["results"][0]["idempotency_key"] == "release:orbit:v0.26.0"
    # Another draft of the same announcement is the same key, not a second post.
    plan.write_text(keyed.replace("is out", "has shipped"))
    again, code = await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    assert code == 1 and again["results"][0]["error"]["code"] == "idempotency_conflict"
    assert len(fake_x.calls("POST", "/tweets")) == 1


async def test_publish_replays_an_imported_key_whatever_the_new_text(
    paths, authed, fake_x, tmp_path
):
    await make_app(paths, transport=fake_x.transport()).import_posted(FIXTURE, confirm=True)
    plan = _plan(tmp_path, 'account: x:constworks\nkey: "pr:example:5"\ntext: "A new draft"\n')
    out, code = await make_app(paths, transport=fake_x.transport()).publish(plan, confirm=True)
    [receipt] = out["results"]
    assert code == 0 and receipt["replayed"] is True and receipt["state"] == "published"
    assert fake_x.calls("POST", "/tweets") == []


async def test_publish_refuses_a_key_that_differs_from_the_plans(paths, authed, fake_x, tmp_path):
    plan = _plan(tmp_path, 'account: x:constworks\nkey: "repo:pulsar"\ntext: "hi"\n')
    for confirm in (False, True):
        with pytest.raises(PulsarError) as exc:
            await make_app(paths, transport=fake_x.transport()).publish(
                plan, confirm=confirm, idempotency_key="repo:other"
            )
        assert exc.value.code == "invalid_argument"
    out, code = await make_app(paths, transport=fake_x.transport()).publish(
        plan, confirm=True, idempotency_key="repo:pulsar"
    )
    assert code == 0 and out["results"][0]["idempotency_key"] == "repo:pulsar"
    assert len(fake_x.calls("POST", "/tweets")) == 1
