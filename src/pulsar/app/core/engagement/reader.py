"""``Reader``: one paid read, budgeted and recorded.

The order mirrors the publisher's, for a call that changes nothing at the
provider:

1. **Admit**: the most the read can cost (``max_posts`` at the provider's
   read price) is checked against the day's and month's budgets
   (``Policy.check_read``). Quiet hours and the post cap do not apply.
2. **Read** through the account's channel.
3. **Record** what the provider returned and billed (``Ledger.record_read``):
   the count and cost, never the posts.

A read that fails records nothing: a provider error is not billed. Two
concurrent reads can both pass the budget check, so the budget can overshoot
by one read; reads are small next to posts, and the next check sees both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pulsar.app.core.channels.contract import Mention, OwnPost, Page, Prices
from pulsar.app.core.ledger import Ledger, ReadKind
from pulsar.app.core.publishing import Bound, Policy, PolicyConfig, day_window, month_window
from pulsar.internal.errors import SECRET_DETECTED, UNSUPPORTED, PulsarError
from pulsar.internal.guard import scan_for_secrets


class ReadSettings(Protocol):
    """What reading needs from the operator's settings; ``app.settings.Settings``
    is the one the app hands in."""

    @property
    def policy(self) -> PolicyConfig: ...

    def prices_for(self, provider: str) -> Prices: ...


@dataclass(frozen=True)
class Read[T]:
    """What one read returned, and what it cost."""

    account: str
    since: datetime
    posts: tuple[T, ...]
    complete: bool
    cost_usd: float
    # Mentions the account has already answered (by the post id they carry).
    replied: frozenset[str] = frozenset()


class Reader:
    def __init__(
        self,
        *,
        ledger: Ledger,
        settings: ReadSettings,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.policy = Policy(settings.policy)
        self._now = now

    async def mentions(
        self, bound: Bound, *, since: datetime, max_posts: int, caller: str
    ) -> Read[Mention]:
        """Posts by others mentioning the account since ``since``, newest first,
        each marked answered or not."""
        if not bound.channel.capabilities.mentions:
            raise PulsarError(UNSUPPORTED, f"{bound.provider} has no mentions to read")
        page = await self._read(
            bound,
            "mentions",
            since,
            max_posts,
            caller,
            lambda: bound.channel.mentions(since, max_posts=max_posts),
        )
        # The account's own posts in a conversation mention it too; they are not engagement.
        posts = tuple(m for m in page.posts if m.author != bound.handle.lower())
        replied = self.ledger.replied_to(bound.alias, [m.post_id for m in posts])
        return Read(
            account=bound.alias,
            since=since,
            posts=posts,
            complete=page.complete,
            cost_usd=self._cost(bound, page),
            replied=frozenset(replied),
        )

    async def own_posts(
        self, bound: Bound, *, since: datetime, max_posts: int, caller: str
    ) -> Read[OwnPost]:
        """The account's posts since ``since`` with their metrics, newest first."""
        if not bound.channel.capabilities.metrics:
            raise PulsarError(UNSUPPORTED, f"{bound.provider} reports no post metrics")
        page = await self._read(
            bound,
            "own_posts",
            since,
            max_posts,
            caller,
            lambda: bound.channel.own_posts(since, max_posts=max_posts),
        )
        return Read(
            account=bound.alias,
            since=since,
            posts=page.posts,
            complete=page.complete,
            cost_usd=self._cost(bound, page),
        )

    def _cost[T](self, bound: Bound, page: Page[T]) -> float:
        return self.settings.prices_for(bound.provider).for_read(page.fetched)

    async def _read[T](
        self,
        bound: Bound,
        kind: ReadKind,
        since: datetime,
        max_posts: int,
        caller: str,
        call: Callable[[], Awaitable[Page[T]]],
    ) -> Page[T]:
        if max_posts < 1:
            raise ValueError(f"max_posts must be >= 1, got {max_posts}")
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if scan_for_secrets(caller):
            raise PulsarError(SECRET_DETECTED, "caller looks like it contains a credential")
        now = self._now()
        tz = self.settings.policy.tz
        day_start, _ = day_window(now, tz)
        month_start, _ = month_window(now, tz)
        usage = self.ledger.usage(bound.alias, day_start=day_start, month_start=month_start)
        prices = self.settings.prices_for(bound.provider)
        self.policy.check_read(usage=usage, planned_cost_usd=prices.for_read(max_posts), now=now)
        page = await call()
        self.ledger.record_read(
            kind=kind,
            provider=bound.provider,
            account_alias=bound.alias,
            caller=caller,
            since=since,
            posts=page.fetched,
            est_cost_usd=prices.for_read(page.fetched),
            complete=page.complete,
        )
        return page
