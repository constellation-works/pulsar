"""``State``: the stored vocabulary, its predicates, and how a row's state is derived."""

from __future__ import annotations

import json

import pytest

from pulsar.core.errors import INTERNAL, PulsarError
from pulsar.core.ledger import (
    COMMITTED_ITEM_STATES,
    FAILED,
    OPEN_ROW_STATES,
    PARTIAL,
    PENDING,
    PUBLISHED,
    SKIPPED,
    SUBMITTING,
    UNKNOWN,
    ItemRecord,
    SqliteLedger,
    State,
    derive_state,
    is_ambiguous,
    is_settled,
)

from .test_ledger import claim_plan, send_all, sql


def test_states_store_and_export_as_the_same_strings(paths):
    assert [s.value for s in State] == [
        "pending", "submitting", "published", "partial", "failed", "unknown", "skipped"
    ]  # fmt: skip
    aliases = [PENDING, SUBMITTING, PUBLISHED, PARTIAL, FAILED, UNKNOWN, SKIPPED]
    assert aliases == list(State) and all(isinstance(a, State) for a in aliases)
    assert json.dumps({"state": PUBLISHED}) == '{"state": "published"}'
    assert PUBLISHED == "published" and f"{PUBLISHED}" == "published"
    assert COMMITTED_ITEM_STATES == ("submitting", "published", "unknown")
    assert OPEN_ROW_STATES == ("pending", "submitting")

    ledger = SqliteLedger(paths)
    claim_plan(ledger)
    record = send_all(ledger, "plan-1", 1)
    assert record.state is State.PUBLISHED and record.items[0].state is State.PUBLISHED
    assert sql(paths, "SELECT state, typeof(state) FROM writes") == [("published", "text")]
    assert json.loads(json.dumps(record.to_dict()))["state"] == "published"


@pytest.mark.parametrize(
    ("state", "settled", "ambiguous"),
    [
        (State.PENDING, False, False),
        (State.SUBMITTING, False, True),
        (State.PUBLISHED, True, False),
        (State.PARTIAL, True, False),
        (State.FAILED, True, False),
        (State.UNKNOWN, False, True),
        (State.SKIPPED, True, False),
    ],
)
def test_predicates_answer_for_every_state(state, settled, ambiguous):
    assert is_settled(state) is settled
    assert is_ambiguous(state) is ambiguous


def test_predicates_cover_the_whole_enum():
    """A state missing from this table is one nobody decided about."""
    assert {s for s in State if is_settled(s)} == {PUBLISHED, PARTIAL, FAILED, SKIPPED}


def items(*states: State) -> list[ItemRecord]:
    return [ItemRecord(idx=i, state=s) for i, s in enumerate(states)]


@pytest.mark.parametrize(
    ("item_states", "row"),
    [
        ((PUBLISHED, PUBLISHED), PUBLISHED),
        ((PUBLISHED, UNKNOWN), UNKNOWN),
        ((PUBLISHED, SUBMITTING), UNKNOWN),
        ((FAILED, UNKNOWN, PENDING), UNKNOWN),
        ((PUBLISHED, FAILED, PENDING), PARTIAL),
        ((PUBLISHED, PENDING), PARTIAL),
        ((FAILED, PENDING), FAILED),
        ((), FAILED),
    ],
)
def test_derive_state(item_states, row):
    assert derive_state(items(*item_states)) is row


@pytest.mark.parametrize("row_only", [PARTIAL, SKIPPED])
def test_derive_state_refuses_a_row_only_state_on_an_item(row_only):
    with pytest.raises(PulsarError) as exc:
        derive_state(items(PUBLISHED, row_only))
    assert exc.value.code == INTERNAL
