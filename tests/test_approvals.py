"""Approvals: a human's yes to one digest for one account, single-use, checked
in the claim, and recordable only from a terminal."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pulsar.app.approvals import parse_ttl
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.facade import LocalApp
from pulsar.app.runtime import default_paths
from pulsar.cli.main import run
from pulsar.internal.errors import PulsarError

from .conftest import ALIAS
from .test_ledger import ACCT, DAY, MONTH, OTHER, Clock, intents, send_all
from .test_publisher import FakeChannel, bound_to, make_publisher, thread

DIGEST = "sha256:" + "a" * 64
LATER = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def approve(ledger, *, digest=DIGEST, account=ACCT, expires_at=LATER):
    return ledger.approve(
        account_alias=account.alias,
        digest=digest,
        approved_by="human:daniel",
        source="plan.yaml",
        posts=1,
        est_cost_usd=0.015,
        expires_at=expires_at,
    )


def claim(ledger, key="reply-1", *, digest=DIGEST):
    return ledger.claim_plan(
        key=key,
        tool="publish",
        digest=digest,
        provider=ACCT.provider,
        account=ACCT,
        caller="agent",
        items=intents(1),
        admit=None,
        day_start=DAY,
        month_start=MONTH,
        approved=True,
    )


# -- the ledger ------------------------------------------------------------------------


def test_a_claim_without_an_approval_is_refused_and_writes_nothing(paths):
    ledger = SqliteLedger(paths, clock=Clock())
    with pytest.raises(PulsarError) as exc:
        claim(ledger)
    assert exc.value.code == "approval_required" and not exc.value.retryable
    assert exc.value.detail == {"account": ACCT.alias, "digest": DIGEST, "last_approval": None}
    assert "pulsar approve" in exc.value.message
    assert ledger.get_plan("reply-1") is None


def test_an_approval_is_used_once_by_one_key(paths):
    ledger = SqliteLedger(paths, clock=Clock())
    recorded = approve(ledger)
    assert claim(ledger).state == "pending"
    [used] = ledger.approvals_for(ACCT.alias, DIGEST)
    assert used.id == recorded.id and used.used_key == "reply-1"
    assert used.state(ledger.now()) == "used"
    with pytest.raises(PulsarError) as exc:
        claim(ledger, key="reply-1-again")
    assert exc.value.code == "approval_required"
    assert exc.value.detail["last_approval"] == "used"


def test_the_key_that_used_it_may_retry_until_it_expires(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    approve(ledger)
    claim(ledger)
    ledger.begin_item("reply-1", 0)
    ledger.item_failed("reply-1", 0, PulsarError("provider_error", "503", retryable=True))
    ledger.finish("reply-1")
    assert claim(ledger).state == "pending", "a retry re-arms under the same approval"
    clock.now = LATER + timedelta(minutes=1)
    ledger.begin_item("reply-1", 0)
    ledger.item_failed("reply-1", 0, PulsarError("provider_error", "503", retryable=True))
    ledger.finish("reply-1")
    with pytest.raises(PulsarError) as exc:
        claim(ledger)
    assert exc.value.detail["last_approval"] == "used"


def test_a_published_plan_replays_without_an_approval(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    approval = approve(ledger)
    claim(ledger)
    send_all(ledger, "reply-1", 1)
    ledger.revoke_approval(approval.id)
    clock.now = LATER + timedelta(days=1)
    assert claim(ledger).state == "published"


def test_an_expired_or_revoked_approval_admits_nothing(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    first = approve(ledger)
    revoked = ledger.revoke_approval(first.id)
    assert revoked.state(ledger.now()) == "revoked"
    assert ledger.revoke_approval(first.id).revoked_at == revoked.revoked_at, "first time kept"
    with pytest.raises(PulsarError) as exc:
        claim(ledger)
    assert exc.value.detail["last_approval"] == "revoked"
    approve(ledger)
    clock.now = LATER
    with pytest.raises(PulsarError) as exc:
        claim(ledger)
    assert exc.value.detail["last_approval"] == "expired"


def test_an_approval_binds_the_digest_and_the_account(paths):
    ledger = SqliteLedger(paths, clock=Clock())
    approve(ledger, digest="sha256:" + "b" * 64)
    with pytest.raises(PulsarError):
        claim(ledger)
    approve(ledger, account=OTHER)
    with pytest.raises(PulsarError):
        claim(ledger)


def test_an_approval_must_expire_in_the_future(paths):
    ledger = SqliteLedger(paths, clock=Clock())
    with pytest.raises(PulsarError) as exc:
        approve(ledger, expires_at=datetime(2026, 9, 26, 11, 0, tzinfo=UTC))
    assert exc.value.code == "invalid_argument"
    assert ledger.approvals(account_alias=None, limit=5) == []


# -- the publisher ---------------------------------------------------------------------


@pytest.fixture
def clock():
    return [datetime(2026, 9, 26, 12, 0, tzinfo=UTC)]


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root


def approve_prepared(pub, prepared, *, expires_at=LATER):
    return pub.ledger.approve(
        account_alias=prepared.bound.alias,
        digest=prepared.digest,
        approved_by="human:daniel",
        source=None,
        posts=len(prepared.posts),
        est_cost_usd=prepared.estimated_cost_usd,
        expires_at=expires_at,
    )


@pytest.mark.anyio
async def test_publish_requiring_approval_sends_only_what_was_approved(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    channel = FakeChannel(clock=clock)
    prepared = pub.prepare(thread("thanks!", reply_to="801"), bound_to(channel))
    with pytest.raises(PulsarError) as exc:
        pub.preflight(prepared, require_approval=True)
    assert exc.value.code == "approval_required"
    with pytest.raises(PulsarError):
        await pub.publish(prepared, caller="agent", require_approval=True)
    assert channel.creates == []

    approval = approve_prepared(pub, prepared)
    pub.preflight(prepared, require_approval=True)
    assert pub.approval(prepared).id == approval.id
    out = await pub.publish(prepared, caller="agent", require_approval=True)
    assert out.record.state == "published" and len(channel.creates) == 1

    again = await pub.publish(prepared, caller="agent", require_approval=True)
    assert again.replayed, "a replay needs no fresh approval"
    pub.preflight(prepared, require_approval=True)
    with pytest.raises(PulsarError) as exc:
        await pub.publish(prepared, idempotency_key="second", caller="agent", require_approval=True)
    assert exc.value.code == "approval_required" and len(channel.creates) == 1


@pytest.mark.anyio
async def test_an_edited_plan_is_not_approved(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    bound = bound_to(FakeChannel(clock=clock))
    approve_prepared(pub, pub.prepare(thread("thanks!", reply_to="801"), bound))
    edited = pub.prepare(thread("thanks!!", reply_to="801"), bound)
    with pytest.raises(PulsarError) as exc:
        await pub.publish(edited, caller="agent", require_approval=True)
    assert exc.value.code == "approval_required"


@pytest.mark.anyio
async def test_a_new_not_before_keeps_the_approval(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    bound = bound_to(FakeChannel(clock=clock))
    approve_prepared(pub, pub.prepare(thread("launch", not_before="2026-09-26T13:00:00Z"), bound))
    moved = pub.prepare(thread("launch", not_before="2026-09-26T11:00:00Z"), bound)
    out = await pub.publish(moved, caller="agent", require_approval=True)
    assert out.record.state == "published"


# -- ttl -------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "hours"), [("90m", 1.5), ("72h", 72), ("7d", 168)])
def test_ttl_parses(text, hours):
    assert parse_ttl(text) == timedelta(hours=hours)


@pytest.mark.parametrize("text", ["", "0m", "31d", "3w", "-1h", "1.5h"])
def test_ttl_refuses(text):
    with pytest.raises(PulsarError) as exc:
        parse_ttl(text)
    assert exc.value.code == "invalid_argument"


# -- the app and the CLI ---------------------------------------------------------------

REPLY = f"""\
account: {ALIAS}
reply_to: "801"
posts:
  - text: "Thanks — it ships Friday."
"""


class Terminal(io.StringIO):
    """A stdin that says it is a terminal, holding what the human types."""

    def isatty(self) -> bool:
        return True


def app() -> LocalApp:
    return LocalApp(default_paths(os.environ, Path.home()), environ=os.environ, cwd=Path.cwd())


@pytest.fixture
def plan(tmp_path) -> Path:
    path = tmp_path / "reply-801.yaml"
    path.write_text(REPLY)
    return path


def digest_of(plan: Path) -> str:
    out, _ = app().approve_preview(plan, account=None, ttl=None)
    [account] = out["accounts"]
    return account["digest"]


def pulsar(capsys, monkeypatch, argv, typed: str | None = None):
    monkeypatch.setattr("sys.stdin", Terminal(typed) if typed is not None else io.StringIO(""))
    code = run(["--json", *argv], app())
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_approve_off_a_terminal_is_refused_and_records_nothing(
    paths, authed, plan, capsys, monkeypatch
):
    code, out, err = pulsar(capsys, monkeypatch, ["approve", str(plan)])
    assert code == 1 and out == ""
    assert "Thanks — it ships Friday." in err, "the human is shown the whole post"
    assert json.loads(err.splitlines()[-1])["code"] == "interactive_only"
    assert app().approvals(account=None, limit=20)[0]["approvals"] == []


def test_approve_with_the_digest_typed_back_records_it(paths, authed, plan, capsys, monkeypatch):
    digest = digest_of(plan)
    typed = digest.removeprefix("sha256:")[:8]
    code, out, err = pulsar(capsys, monkeypatch, ["approve", str(plan)], typed=typed + "\n")
    assert code == 0, err
    [approval] = json.loads(out)["approvals"]
    assert approval["digest"] == digest and approval["state"] == "active"
    assert approval["account"] == ALIAS and approval["approved_by"].startswith("human:")
    created = datetime.fromisoformat(approval["created_at"])
    expires = datetime.fromisoformat(approval["expires_at"])
    assert timedelta(hours=71) < expires - created <= timedelta(hours=72), "a reply lasts 72h"

    code, out, _ = pulsar(capsys, monkeypatch, ["approvals"])
    assert [a["id"] for a in json.loads(out)["approvals"]] == [approval["id"]]
    code, out, _ = pulsar(capsys, monkeypatch, ["revoke", str(approval["id"])])
    assert code == 0 and json.loads(out)["revoked"]["state"] == "revoked"


def test_approve_with_the_wrong_digest_typed_records_nothing(
    paths, authed, plan, capsys, monkeypatch
):
    code, out, err = pulsar(capsys, monkeypatch, ["approve", str(plan)], typed="deadbeef\n")
    assert code == 1 and out == "" and "nothing was recorded" in err
    assert app().approvals(account=None, limit=20)[0]["approvals"] == []


def test_approve_refuses_a_plan_edited_after_it_was_shown(paths, authed, plan):
    digest = digest_of(plan)
    plan.write_text(REPLY.replace("Friday", "Monday"))
    with pytest.raises(PulsarError) as exc:
        app().approve(plan, expect={ALIAS: digest}, account=None, ttl=None)
    assert exc.value.code == "invalid_argument"
    assert "changed since it was shown" in exc.value.message
    assert app().approvals(account=None, limit=20)[0]["approvals"] == []


def test_revoking_an_unknown_approval_is_an_error(paths, authed, capsys, monkeypatch):
    code, out, err = pulsar(capsys, monkeypatch, ["revoke", "99"])
    assert code == 1 and out == "" and json.loads(err)["code"] == "invalid_argument"
