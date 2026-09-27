"""Policy: quiet hours, daily post cap, budgets. Fixed clocks only, never the wall clock."""

import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pulsar.app.core.ledger import Usage
from pulsar.app.core.publishing import Policy, day_window, month_window, quiet_until
from pulsar.app.settings import PolicyConfig
from pulsar.internal.errors import BUDGET_EXCEEDED, DAILY_CAP, QUIET_HOURS, PulsarError

LA = ZoneInfo("America/Los_Angeles")
NOON = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
NONE_USED = Usage(spent_day_usd=0.0, spent_month_usd=0.0, posts_day=0)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def refuse(policy: Policy, **kwargs) -> PulsarError:
    kwargs.setdefault("usage", NONE_USED)
    kwargs.setdefault("planned_cost_usd", 0.015)
    kwargs.setdefault("planned_posts", 1)
    kwargs.setdefault("now", NOON)
    with pytest.raises(PulsarError) as exc:
        policy.check(**kwargs)
    return exc.value


def allow(policy: Policy, **kwargs) -> None:
    kwargs.setdefault("usage", NONE_USED)
    kwargs.setdefault("planned_cost_usd", 0.015)
    kwargs.setdefault("planned_posts", 1)
    kwargs.setdefault("now", NOON)
    policy.check(**kwargs)


# --- windows -----------------------------------------------------------------


def test_utc_day_and_month_windows():
    assert day_window(NOON, ZoneInfo("UTC")) == (utc(2026, 9, 26), utc(2026, 9, 27))
    assert month_window(NOON, ZoneInfo("UTC")) == (utc(2026, 9, 1), utc(2026, 10, 1))
    assert month_window(utc(2026, 12, 31, 23, 59), ZoneInfo("UTC")) == (
        utc(2026, 12, 1),
        utc(2027, 1, 1),
    )


def test_windows_are_utc_and_half_open():
    start, end = day_window(utc(2026, 9, 27), ZoneInfo("UTC"))
    assert start == utc(2026, 9, 27) and end == utc(2026, 9, 28)
    assert start.tzinfo == UTC and end.tzinfo == UTC


def test_la_spring_forward_day_is_23_hours():
    start, end = day_window(datetime(2026, 3, 8, 12, tzinfo=LA), LA)
    assert start == utc(2026, 3, 8, 8)  # midnight PST
    assert end == utc(2026, 3, 9, 7)  # midnight PDT
    assert end - start == timedelta(hours=23)


def test_la_fall_back_day_is_25_hours():
    start, end = day_window(datetime(2026, 11, 1, 12, tzinfo=LA), LA)
    assert start == utc(2026, 11, 1, 7)  # midnight PDT
    assert end == utc(2026, 11, 2, 8)  # midnight PST
    assert end - start == timedelta(hours=25)
    # Both passes through the repeated 01:xx hour belong to the same day.
    assert day_window(utc(2026, 11, 1, 8, 30), LA) == (start, end)
    assert day_window(utc(2026, 11, 1, 9, 30), LA) == (start, end)


def test_skipped_midnight_starts_the_day_at_the_jump():
    # Chile springs forward at 24:00: 2026-09-06 00:00 local never happens.
    santiago = ZoneInfo("America/Santiago")
    start, end = day_window(utc(2026, 9, 6, 12), santiago)
    assert start == utc(2026, 9, 6, 4)
    assert start.astimezone(santiago).hour == 1
    assert end - start == timedelta(hours=23)


def test_local_month_starts_at_a_different_utc_instant():
    # 2026-03-01 05:00 UTC is still February 28 in Los Angeles.
    assert month_window(utc(2026, 3, 1, 5), LA) == (utc(2026, 2, 1, 8), utc(2026, 3, 1, 8))
    # Three hours later it is March there; March ends in PDT (DST began on the 8th).
    assert month_window(utc(2026, 3, 1, 8), LA) == (utc(2026, 3, 1, 8), utc(2026, 4, 1, 7))
    assert month_window(utc(2026, 12, 31, 20), LA) == (utc(2026, 12, 1, 8), utc(2027, 1, 1, 8))


def test_naive_datetimes_are_a_programming_error():
    naive = datetime(2026, 9, 26, 12)
    policy = Policy(PolicyConfig())
    with pytest.raises(ValueError):
        day_window(naive, LA)
    with pytest.raises(ValueError):
        month_window(naive, LA)
    with pytest.raises(ValueError):
        quiet_until(naive, PolicyConfig(quiet_hours=(time(23), time(7))))
    with pytest.raises(ValueError):
        policy.check(usage=NONE_USED, planned_cost_usd=0, planned_posts=1, now=naive)
    with pytest.raises(ValueError):
        policy.status(usage=NONE_USED, now=naive)


def test_negative_plans_are_a_programming_error():
    policy = Policy(PolicyConfig())
    with pytest.raises(ValueError):
        allow(policy, planned_posts=-1)
    with pytest.raises(ValueError):
        allow(policy, planned_cost_usd=-0.01)
    with pytest.raises(ValueError):
        allow(policy, planned_cost_usd=float("nan"))


# --- quiet hours ---------------------------------------------------------------

WRAP = PolicyConfig(quiet_hours=(time(23), time(7)), timezone="America/Los_Angeles")


def test_no_quiet_hours_by_default():
    assert quiet_until(utc(2026, 9, 26, 3), PolicyConfig()) is None


@pytest.mark.parametrize(
    ("local", "until"),
    [
        (datetime(2026, 9, 26, 22, 59, 59), None),
        (datetime(2026, 9, 26, 23, 0), datetime(2026, 9, 27, 7)),  # start edge is inside
        (datetime(2026, 9, 26, 23, 59), datetime(2026, 9, 27, 7)),
        (datetime(2026, 9, 27, 0, 0), datetime(2026, 9, 27, 7)),
        (datetime(2026, 9, 27, 6, 59, 59), datetime(2026, 9, 27, 7)),
        (datetime(2026, 9, 27, 7, 0), None),  # end edge is outside
        (datetime(2026, 9, 27, 12, 0), None),
    ],
)
def test_wrapping_quiet_window_edges(local, until):
    now = local.replace(tzinfo=LA)
    expected = until.replace(tzinfo=LA).astimezone(UTC) if until else None
    assert quiet_until(now, WRAP) == expected


def test_non_wrapping_quiet_window():
    cfg = PolicyConfig(quiet_hours=(time(12), time(13, 30)))
    assert quiet_until(utc(2026, 9, 26, 11, 59), cfg) is None
    assert quiet_until(utc(2026, 9, 26, 12), cfg) == utc(2026, 9, 26, 13, 30)
    assert quiet_until(utc(2026, 9, 26, 13, 30), cfg) is None


def test_quiet_until_is_utc_whatever_zone_now_is_in():
    now = datetime(2026, 9, 27, 1, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # 2026-09-26 09:00 PDT
    assert quiet_until(now, WRAP) is None
    now = datetime(2026, 9, 27, 17, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # 01:00 PDT
    until = quiet_until(now, WRAP)
    assert until == utc(2026, 9, 27, 14) and until is not None and until.tzinfo == UTC


def test_quiet_end_in_a_spring_forward_gap_ends_at_the_jump():
    # 02:30 does not exist on 2026-03-08 in LA: 02:00 PST jumps to 03:00 PDT (10:00 UTC).
    cfg = PolicyConfig(quiet_hours=(time(23), time(2, 30)), timezone="America/Los_Angeles")
    assert quiet_until(utc(2026, 3, 8, 9, 59, 59), cfg) == utc(2026, 3, 8, 10)
    assert quiet_until(utc(2026, 3, 8, 10), cfg) is None


def test_quiet_end_repeated_by_fall_back_uses_the_next_occurrence():
    # 01:30 happens twice on 2026-11-01 in LA: 08:30 UTC (PDT) and 09:30 UTC (PST).
    cfg = PolicyConfig(quiet_hours=(time(0), time(1, 30)), timezone="America/Los_Angeles")
    assert quiet_until(utc(2026, 11, 1, 8, 10), cfg) == utc(2026, 11, 1, 8, 30)
    assert quiet_until(utc(2026, 11, 1, 8, 40), cfg) is None  # 01:40 PDT
    assert quiet_until(utc(2026, 11, 1, 9, 10), cfg) == utc(2026, 11, 1, 9, 30)  # 01:10 PST


def test_quiet_hours_refusal():
    now = datetime(2026, 9, 26, 23, 30, tzinfo=LA)
    err = refuse(Policy(WRAP), now=now)
    assert err.code == QUIET_HOURS and err.retryable
    assert err.detail == {
        "retry_after": "2026-09-27T14:00:00+00:00",
        "window": "23:00-07:00",
        "timezone": "America/Los_Angeles",
    }


# --- daily cap -------------------------------------------------------------------


def test_cap_boundary_equality_passes():
    policy = Policy(PolicyConfig())  # cap 5
    allow(policy, usage=Usage(0, 0, 4), planned_posts=1)
    allow(policy, usage=Usage(0, 0, 0), planned_posts=5, planned_cost_usd=0.075)


def test_cap_exceeded_is_retryable_at_next_local_midnight():
    policy = Policy(PolicyConfig(timezone="America/Los_Angeles"))
    now = datetime(2026, 9, 26, 15, tzinfo=LA)
    err = refuse(policy, usage=Usage(0.06, 0.06, 4), planned_posts=2, now=now)
    assert err.code == DAILY_CAP and err.retryable
    assert err.detail == {
        "limit": 5,
        "used": 4,
        "planned": 2,
        "retry_after": "2026-09-27T07:00:00+00:00",
    }


def test_thread_longer_than_the_cap_can_never_pass():
    err = refuse(Policy(PolicyConfig()), planned_posts=6, planned_cost_usd=0.09)
    assert err.code == DAILY_CAP and not err.retryable
    assert err.detail["retry_after"] is None
    assert "never" in err.message


def test_zero_cap_blocks_every_post_even_free_ones():
    err = refuse(Policy(PolicyConfig(max_posts_per_day=0)), planned_cost_usd=0.0)
    assert err.code == DAILY_CAP and not err.retryable


# --- budgets ---------------------------------------------------------------------


def test_budget_boundary_equality_passes_with_float_sums():
    # In floats 0.1 + 0.2 > 0.3; a plan landing exactly on the budget must pass.
    assert 0.1 + 0.2 > 0.3
    allow(
        Policy(PolicyConfig(daily_budget_usd=0.3)), usage=Usage(0.1, 0.1, 0), planned_cost_usd=0.2
    )
    # A ledger summing 0.015 per post: 66 posts, then one costing the last cent.
    spent = sum([0.015] * 66)
    policy = Policy(PolicyConfig(max_posts_per_day=100))
    allow(policy, usage=Usage(spent, spent, 0), planned_cost_usd=0.01)
    assert refuse(policy, usage=Usage(spent, spent, 0), planned_cost_usd=0.010001).retryable
    allow(policy, usage=Usage(0.9, 0.9, 0), planned_cost_usd=sum([0.015] * 6) + 0.01)


def test_daily_budget_exceeded_is_retryable_at_next_local_midnight():
    policy = Policy(PolicyConfig(max_posts_per_day=100, timezone="America/Los_Angeles"))
    now = datetime(2026, 11, 1, 12, tzinfo=LA)  # a 25-hour day
    err = refuse(policy, usage=Usage(0.99, 0.99, 0), planned_cost_usd=0.015, now=now)
    assert err.code == BUDGET_EXCEEDED and err.retryable
    assert err.detail == {
        "window": "day",
        "budget_usd": 1.0,
        "spent_usd": 0.99,
        "planned_usd": 0.015,
        "retry_after": "2026-11-02T08:00:00+00:00",
    }


def test_monthly_budget_exceeded_is_retryable_at_next_local_month():
    policy = Policy(PolicyConfig(timezone="America/Los_Angeles"))
    now = utc(2026, 3, 1, 5)  # still February in LA
    err = refuse(policy, usage=Usage(0.0, 9.99, 0), planned_cost_usd=0.2, now=now)
    assert err.code == BUDGET_EXCEEDED and err.retryable
    assert err.detail == {
        "window": "month",
        "budget_usd": 10.0,
        "spent_usd": 9.99,
        "planned_usd": 0.2,
        "retry_after": "2026-03-01T08:00:00+00:00",
    }


def test_plan_costlier_than_a_budget_can_never_pass():
    err = refuse(Policy(PolicyConfig(daily_budget_usd=0.1)), planned_cost_usd=0.2)
    assert err.code == BUDGET_EXCEEDED and not err.retryable
    assert err.detail["window"] == "day" and err.detail["retry_after"] is None
    assert "never" in err.message
    err = refuse(
        Policy(PolicyConfig(daily_budget_usd=5, monthly_budget_usd=0.1)), planned_cost_usd=0.2
    )
    assert err.detail["window"] == "month" and not err.retryable


def test_zero_budget_is_a_kill_switch_for_paid_posts_only():
    policy = Policy(PolicyConfig(daily_budget_usd=0))
    err = refuse(policy, planned_cost_usd=0.015)
    assert err.code == BUDGET_EXCEEDED and not err.retryable
    assert "switched off" in err.message
    allow(policy, planned_cost_usd=0.0)
    # Free plans are never blocked by a budget, even an overspent one.
    allow(Policy(PolicyConfig(monthly_budget_usd=0)), usage=Usage(3, 30, 0), planned_cost_usd=0)


# --- order -----------------------------------------------------------------------


def test_rules_report_in_order_quiet_cap_day_month():
    cfg = PolicyConfig(
        daily_budget_usd=1,
        monthly_budget_usd=2,
        max_posts_per_day=5,
        quiet_hours=(time(23), time(7)),
    )
    policy = Policy(cfg)
    everything = Usage(spent_day_usd=1.0, spent_month_usd=2.0, posts_day=5)
    night, day = utc(2026, 9, 26, 23, 30), utc(2026, 9, 26, 12)
    assert refuse(policy, usage=everything, now=night).code == QUIET_HOURS
    assert refuse(policy, usage=everything, now=day).code == DAILY_CAP
    err = refuse(policy, usage=Usage(1.0, 2.0, 0), now=day)
    assert (err.code, err.detail["window"]) == (BUDGET_EXCEEDED, "day")
    err = refuse(policy, usage=Usage(0.5, 2.0, 0), now=day)
    assert (err.code, err.detail["window"]) == (BUDGET_EXCEEDED, "month")
    allow(policy, usage=Usage(0.5, 1.5, 4), now=day)


# --- error shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "usage", "now", "code"),
    [
        (WRAP, NONE_USED, utc(2026, 9, 26, 8), QUIET_HOURS),
        (PolicyConfig(), Usage(0, 0, 5), NOON, DAILY_CAP),
        (PolicyConfig(), Usage(1.0, 1.0, 0), NOON, BUDGET_EXCEEDED),
        (PolicyConfig(), Usage(0, 10.0, 0), NOON, BUDGET_EXCEEDED),
    ],
)
def test_error_shapes_carry_retry_after(cfg, usage, now, code):
    err = refuse(Policy(cfg), usage=usage, now=now)
    result, envelope = err.to_result(), err.to_envelope()
    assert result["code"] == envelope["error"]["code"] == code
    assert result["retryable"] is envelope["error"]["retryable"] is True
    retry_after = datetime.fromisoformat(result["detail"]["retry_after"])
    assert retry_after.utcoffset() == timedelta(0) and retry_after > now
    assert envelope["error"]["detail"]["retry_after"] == result["detail"]["retry_after"]
    json.dumps(envelope)


# --- status ------------------------------------------------------------------------


def test_status_summary():
    cfg = PolicyConfig(quiet_hours=(time(23), time(7)), timezone="America/Los_Angeles")
    now = datetime(2026, 11, 1, 23, 30, tzinfo=LA)
    status = Policy(cfg).status(usage=Usage(1.2, 4.5, 7), now=now)
    assert status == {
        "timezone": "America/Los_Angeles",
        "day": {
            "budget_usd": 1.0,
            "spent_usd": 1.2,
            "remaining_usd": 0.0,
            "start": "2026-11-01T07:00:00+00:00",
            "resets_at": "2026-11-02T08:00:00+00:00",
        },
        "month": {
            "budget_usd": 10.0,
            "spent_usd": 4.5,
            "remaining_usd": 5.5,
            "start": "2026-11-01T07:00:00+00:00",
            "resets_at": "2026-12-01T08:00:00+00:00",
        },
        "posts": {"used": 7, "cap": 5, "remaining": 0},
        "quiet": {"window": "23:00-07:00", "active": True, "until": "2026-11-02T15:00:00+00:00"},
    }
    json.dumps(status)


def test_status_outside_quiet_hours_and_without_them():
    status = Policy(PolicyConfig()).status(usage=Usage(0.03, 0.03, 2), now=NOON)
    assert status["quiet"] == {"window": None, "active": False, "until": None}
    assert status["day"]["remaining_usd"] == 0.97
    assert status["posts"] == {"used": 2, "cap": 5, "remaining": 3}
