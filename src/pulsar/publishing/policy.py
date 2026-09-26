"""Publishing policy: quiet hours, the per-account daily post cap, and budgets.

Enforced before any network call, against the committed ``Usage`` the ledger
reports. ``Policy.check`` raises the first rule that fails, in this order:

1. ``quiet_hours`` — now is inside the configured quiet window.
2. ``daily_cap`` — the account's posts today plus this plan exceed the cap.
3. ``budget_exceeded`` — today's spend (all accounts) plus this plan exceeds
   the daily budget; then the same for the calendar month.

Limits are inclusive: a plan that lands exactly on a cap or budget passes.
Money is compared as ``Decimal`` rounded to 6 places, so ``0.015 * N`` sums
compare as written. A budget of 0 blocks every paid plan and a cap of 0
every post (kill switches). A plan that costs nothing is never blocked by a
budget, even one already overspent; the post cap still applies to it.

Every refusal carries ``detail.retry_after`` (ISO 8601 UTC): when the window
moves (quiet hours end, the next local midnight, the next local month). A
plan that is too big on its own — more posts than the cap, or costing more
than the budget — can never pass however long the caller waits, so it is
refused with ``retryable: false`` and ``retry_after: null``.

Clock: day and month boundaries and quiet hours are local wall-clock times
in ``policy.timezone``, converted to UTC instants:

- A local day is [local midnight, next local midnight): 23 or 25 hours
  across a DST change. Where midnight itself is skipped (zones that switch
  at 00:00), the day starts at the first instant that exists.
- Quiet hours are ``[start, end)`` on the wall clock and may wrap midnight.
  When the end falls in a spring-forward gap (``quiet_hours = "23:00-02:30"``
  on a night 02:00 jumps to 03:00), the window ends at the jump, the first
  instant whose wall clock reads at or after the end; that keeps
  ``retry_after`` and the in-window test consistent. On a fall-back night a
  repeated wall-clock hour is inside the window both times it occurs.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pulsar.errors import BUDGET_EXCEEDED, DAILY_CAP, QUIET_HOURS, PulsarError
from pulsar.home import PolicyConfig
from pulsar.ledger import Usage

_MICRO = Decimal("0.000001")


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("policy needs a timezone-aware datetime; got a naive one")


def _wall(instant: datetime, tz: ZoneInfo) -> datetime:
    """The local wall-clock reading of ``instant``, as a naive datetime."""
    return instant.astimezone(tz).replace(tzinfo=None)


def _instants(day: date, at: time, tz: ZoneInfo) -> list[datetime]:
    """The UTC instants whose local wall clock reads ``day at``, ascending.

    Usually one; two when a fall-back repeats that time. When a spring-forward
    gap skips it, the single instant of the jump: the first instant whose wall
    clock reads at or after it.
    """
    wall = datetime.combine(day, at)
    candidates = sorted({wall.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1)})
    real = [c for c in candidates if _wall(c, tz) == wall]
    if real:
        return real
    # In a gap, fold=1 (the later offset) lands before the jump and fold=0
    # after it. Transitions fall on whole seconds; bisect to the jump.
    lo, hi = candidates[0], candidates[-1]
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if _wall(mid, tz) >= wall:
            hi = mid
        else:
            lo = mid
    return [hi.replace(microsecond=0)]


def _start_of(day: date, tz: ZoneInfo) -> datetime:
    return _instants(day, time(0), tz)[0]


def day_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of the local day containing ``now``, as aware UTC datetimes."""
    _require_aware(now)
    today = now.astimezone(tz).date()
    return _start_of(today, tz), _start_of(today + timedelta(days=1), tz)


def month_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of the local calendar month containing ``now``, in UTC."""
    _require_aware(now)
    first = now.astimezone(tz).date().replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return _start_of(first, tz), _start_of(following, tz)


def _quiet_label(config: PolicyConfig) -> str | None:
    if config.quiet_hours is None:
        return None
    start, end = config.quiet_hours
    return f"{start:%H:%M}-{end:%H:%M}"


def quiet_until(now: datetime, config: PolicyConfig) -> datetime | None:
    """When the quiet window ends (aware UTC) if ``now`` is inside it, else None."""
    _require_aware(now)
    if config.quiet_hours is None:
        return None
    start, end = config.quiet_hours
    tz = config.tz
    local = _wall(now, tz)
    clock = local.time()
    if start < end:
        if not start <= clock < end:
            return None
        end_day = local.date()
    elif clock >= start:
        end_day = local.date() + timedelta(days=1)
    elif clock < end:
        end_day = local.date()
    else:
        return None
    now_utc = now.astimezone(UTC)
    ends = _instants(end_day, end, tz)
    # On a fall-back night the end may occur twice; the next one is ours.
    return next((e for e in ends if e > now_utc), ends[-1])


def _usd(value: float) -> Decimal:
    return Decimal(repr(float(value))).quantize(_MICRO, rounding=ROUND_HALF_EVEN)


def _json_usd(value: Decimal) -> float:
    return float(value)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="seconds")


class Policy:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config
        self.tz = config.tz

    def check(
        self, *, usage: Usage, planned_cost_usd: float, planned_posts: int, now: datetime
    ) -> None:
        """Raise ``PulsarError`` for the first rule the plan breaks; return if it may go."""
        _require_aware(now)
        if planned_posts < 0:
            raise ValueError(f"planned_posts must be >= 0, got {planned_posts}")
        if not math.isfinite(planned_cost_usd) or planned_cost_usd < 0:
            raise ValueError(
                f"planned_cost_usd must be a finite number >= 0, got {planned_cost_usd}"
            )
        self._check_quiet(now)
        self._check_cap(usage, planned_posts, now)
        planned = _usd(planned_cost_usd)
        if planned == 0:
            return
        day_end = day_window(now, self.tz)[1]
        month_end = month_window(now, self.tz)[1]
        self._check_budget(
            "day", _usd(self.config.daily_budget_usd), _usd(usage.spent_day_usd), planned, day_end
        )
        self._check_budget(
            "month",
            _usd(self.config.monthly_budget_usd),
            _usd(usage.spent_month_usd),
            planned,
            month_end,
        )

    def _check_quiet(self, now: datetime) -> None:
        until = quiet_until(now, self.config)
        if until is None:
            return
        window = _quiet_label(self.config)
        raise PulsarError(
            QUIET_HOURS,
            f"inside quiet hours {window} ({self.config.timezone}); "
            f"publishing resumes at {_iso(until)}",
            detail={"retry_after": _iso(until), "window": window, "timezone": self.config.timezone},
        )

    def _check_cap(self, usage: Usage, planned: int, now: datetime) -> None:
        limit = self.config.max_posts_per_day
        if usage.posts_day + planned <= limit:
            return
        detail: dict[str, Any] = {"limit": limit, "used": usage.posts_day, "planned": planned}
        if planned > limit:
            raise PulsarError(
                DAILY_CAP,
                f"the plan has {planned} posts but the daily cap is {limit} per account; "
                "it can never pass as one plan — split it across days or raise "
                "policy.max_posts_per_day",
                detail={**detail, "retry_after": None},
                retryable=False,
            )
        retry_after = _iso(day_window(now, self.tz)[1])
        raise PulsarError(
            DAILY_CAP,
            f"daily cap reached: {usage.posts_day} of {limit} posts used today on this "
            f"account, the plan adds {planned}; the day resets at {retry_after}",
            detail={**detail, "retry_after": retry_after},
        )

    def _check_budget(
        self, window: str, budget: Decimal, spent: Decimal, planned: Decimal, end: datetime
    ) -> None:
        if spent + planned <= budget:
            return
        key = "daily_budget_usd" if window == "day" else "monthly_budget_usd"
        detail: dict[str, Any] = {
            "window": window,
            "budget_usd": _json_usd(budget),
            "spent_usd": _json_usd(spent),
            "planned_usd": _json_usd(planned),
        }
        if planned > budget:
            reason = "publishing is switched off" if budget == 0 else "it can never pass"
            raise PulsarError(
                BUDGET_EXCEEDED,
                f"the plan costs ${planned.normalize():f} but the {window} budget is "
                f"${budget.normalize():f}; {reason} — raise policy.{key}",
                detail={**detail, "retry_after": None},
                retryable=False,
            )
        retry_after = _iso(end)
        raise PulsarError(
            BUDGET_EXCEEDED,
            f"{window} budget exceeded: ${spent.normalize():f} of ${budget.normalize():f} "
            f"spent, the plan adds ${planned.normalize():f}; the {window} resets at "
            f"{retry_after}",
            detail={**detail, "retry_after": retry_after},
        )

    def status(self, *, usage: Usage, now: datetime) -> dict[str, Any]:
        """JSON-safe summary of budgets, usage and quiet hours for ``pulsar status``."""
        _require_aware(now)
        day_start, day_end = day_window(now, self.tz)
        month_start, month_end = month_window(now, self.tz)
        until = quiet_until(now, self.config)

        def money(budget: float, spent: float, start: datetime, end: datetime) -> dict[str, Any]:
            b, s = _usd(budget), _usd(spent)
            return {
                "budget_usd": _json_usd(b),
                "spent_usd": _json_usd(s),
                "remaining_usd": _json_usd(max(b - s, Decimal(0))),
                "start": _iso(start),
                "resets_at": _iso(end),
            }

        cap = self.config.max_posts_per_day
        return {
            "timezone": self.config.timezone,
            "day": money(self.config.daily_budget_usd, usage.spent_day_usd, day_start, day_end),
            "month": money(
                self.config.monthly_budget_usd, usage.spent_month_usd, month_start, month_end
            ),
            "posts": {
                "used": usage.posts_day,
                "cap": cap,
                "remaining": max(cap - usage.posts_day, 0),
            },
            "quiet": {
                "window": _quiet_label(self.config),
                "active": until is not None,
                "until": _iso(until) if until is not None else None,
            },
        }
