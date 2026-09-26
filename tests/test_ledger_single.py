"""The single-request API (``claim`` / ``publish`` / ``fail``) against ``Ledger`` directly."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from pulsar.core.errors import (
    IDEMPOTENCY_CONFLICT,
    INTERNAL,
    INVALID_ARGUMENT,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.core.ledger import PUBLISHED, SUBMITTING, Ledger
from pulsar.core.paths import Paths

from .test_ledger import ME, Clock, states

STALE = timedelta(minutes=10)


@pytest.mark.parametrize("trial", range(10))
def test_concurrent_claims_from_many_processes_admit_exactly_one(tmp_path, trial):
    """Separate Ledger objects stand in for processes; each opens its own connection.

    Each trial starts from a new file, so the racers also race to create the
    schema and switch it to WAL (which once failed with ``database is locked``).
    """
    paths = Paths(home=tmp_path / f"home-{trial}")
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        ledger = Ledger(paths)
        barrier.wait()
        try:
            rec = ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
            result = rec.state
        except OutcomeUnknown:
            result = "blocked"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["blocked"] * (n - 1) + [SUBMITTING]


def test_key_from_another_account_is_a_conflict(paths):
    ledger = Ledger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
    ledger.publish("k", post_id="1")
    other = {"user_id": "999", "username": "someone"}
    with pytest.raises(PulsarError) as exc:
        ledger.claim(key="k", tool="create_post", digest="d", account=other, caller="t")
    assert exc.value.code == IDEMPOTENCY_CONFLICT


def test_legacy_create_post_rows_mirror_one_item(paths):
    ledger = Ledger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t", text_sha256="h")
    plan = ledger.get_plan("k")
    assert plan.account_alias == "x:constworks" and states(plan) == [SUBMITTING]
    ledger.fail("k", PulsarError("api_error", "down"))
    assert ledger.get_plan("k").items[0].error_code == "api_error"
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t", text_sha256="h")
    ledger.publish("k", post_id="77", url="https://x.com/constworks/status/77")
    (item,) = ledger.get_plan("k").items
    assert (item.state, item.post_id, item.error_code) == (PUBLISHED, "77", None)
    ledger.claim(key="up", tool="upload_media", digest="u", account=ME, caller="t")
    assert ledger.get_plan("up").items == ()


def test_settling_an_unclaimed_key_is_an_internal_error(paths):
    ledger = Ledger(paths)
    for settle in (
        lambda: ledger.publish("never-claimed", post_id="1"),
        lambda: ledger.fail("never-claimed", PulsarError("api_error", "down")),
    ):
        with pytest.raises(PulsarError) as exc:
            settle()
        assert exc.value.code == INTERNAL
        assert exc.value.detail == {"idempotency_key": "never-claimed"}


# -- a crashed delete or upload does not block its key forever ------


def claim_delete(ledger, **kwargs):
    return ledger.claim(
        key="delete:42", tool="delete_post", digest="d-del", account=ME, caller="t", **kwargs
    )


@pytest.mark.parametrize(
    ("tool", "key"), [("delete_post", "delete:42"), ("upload_media", "upload:abc")]
)
def test_a_stale_submitting_delete_or_upload_is_re_armed(paths, tool, key):
    clock = Clock()
    ledger = Ledger(paths, clock=clock)
    first = ledger.claim(key=key, tool=tool, digest="d", account=ME, caller="t")
    assert first.state == SUBMITTING  # ...and the sender dies before settling it

    with pytest.raises(OutcomeUnknown):  # without a threshold: blocked, as before
        ledger.claim(key=key, tool=tool, digest="d", account=ME, caller="t")
    clock.now += STALE - timedelta(seconds=1)
    with pytest.raises(OutcomeUnknown):  # within the threshold: maybe still running
        ledger.claim(key=key, tool=tool, digest="d", account=ME, caller="t", stale_after=STALE)

    clock.now += timedelta(seconds=2)
    again = ledger.claim(key=key, tool=tool, digest="d", account=ME, caller="u", stale_after=STALE)
    assert again.state == SUBMITTING and again.attempts == 2 and again.caller == "u"
    plan = ledger.get_plan(key)
    assert plan.note is not None and "re-armed" in plan.note and first.updated_at in plan.note
    assert ledger.publish(key, post_id="42").state == PUBLISHED


def test_a_stale_submitting_post_is_never_re_armed(paths):
    clock = Clock()
    ledger = Ledger(paths, clock=clock)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
    clock.now += timedelta(days=1)
    with pytest.raises(OutcomeUnknown) as exc:
        ledger.claim(
            key="k", tool="create_post", digest="d", account=ME, caller="t", stale_after=STALE
        )
    assert exc.value.detail["state"] == SUBMITTING
    assert ledger.get("k").attempts == 1


def test_re_arm_still_refuses_a_different_request(paths):
    clock = Clock()
    ledger = Ledger(paths, clock=clock)
    claim_delete(ledger)
    clock.now += timedelta(hours=1)
    with pytest.raises(PulsarError) as exc:
        ledger.claim(
            key="delete:42", tool="delete_post", digest="other", account=ME, caller="t",
            stale_after=STALE,
        )  # fmt: skip
    assert exc.value.code == IDEMPOTENCY_CONFLICT


def test_stale_after_must_be_positive(paths):
    with pytest.raises(PulsarError) as exc:
        claim_delete(Ledger(paths), stale_after=timedelta(0))
    assert exc.value.code == INVALID_ARGUMENT
