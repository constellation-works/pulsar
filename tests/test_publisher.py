"""The publisher over a fake channel: no HTTP, every step observable."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta

import pytest

from pulsar.channels import (
    Capabilities,
    Identity,
    LoadedMedia,
    MediaCapabilities,
    PostCheck,
    Published,
    RecentPosts,
    RemotePost,
)
from pulsar.errors import AuthExpired, OutcomeUnknown, PulsarError
from pulsar.home import PolicyConfig, Prices, Settings
from pulsar.ledger import ItemIntent, SqliteLedger
from pulsar.plan import Plan, PostSpec
from pulsar.publishing import RECONCILE_GRACE, STALE_SUBMITTING, Bound, Publisher

from .media_samples import PNG
from .test_ledger import build_v1_ledger

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

CAPS = Capabilities(
    provider="fake",
    max_length=100,
    length_unit="characters",
    threads=True,
    reply=True,
    quote=True,
    delete=True,
    metrics=False,
    media=MediaCapabilities(
        mime_types=frozenset({"image/png"}),
        max_bytes=(("image/png", 1024),),
        max_per_post=2,
        alt_text=True,
        alt_max_length=100,
    ),
)


def fp(text: str) -> str:
    return hashlib.sha256(text.lower().encode()).hexdigest()


@dataclass
class FakeChannel:
    caps: Capabilities = CAPS
    # idx of the create call (0-based, counted across the channel's life) -> behaviour
    fail_on: dict[int, str] = field(default_factory=dict)
    creates: list[dict] = field(default_factory=list)
    uploads: list[LoadedMedia] = field(default_factory=list)
    timeline: list[RemotePost] = field(default_factory=list)
    complete: bool = True
    next_id: int = 500
    clock: list[datetime] = field(default_factory=lambda: [NOW])

    @property
    def capabilities(self) -> Capabilities:
        return self.caps

    def check_post(self, post: PostSpec, prices: Prices) -> PostCheck:
        if len(post.text) > self.caps.max_length:
            raise PulsarError("invalid_text", "too long")
        has_url = "http" in post.text
        return PostCheck(
            text=post.text,
            length=len(post.text),
            has_url=has_url,
            estimated_cost_usd=prices.for_post(has_url=has_url),
        )

    def check_media(self, media: tuple[LoadedMedia, ...]) -> None:
        if len(media) > self.caps.media.max_per_post:
            raise PulsarError("invalid_media", "too many")

    def check_target(self, *, reply_to: str | None, quote: str | None) -> None:
        for value in (reply_to, quote):
            if value is not None and not value.isdigit():
                raise PulsarError("invalid_argument", "bad id")

    def fingerprint(self, text: str) -> str:
        return fp(text)

    async def whoami(self) -> Identity:
        return Identity(provider_user_id="42", handle="acct")

    async def upload(self, media: LoadedMedia) -> str:
        self.uploads.append(media)
        return f"m{len(self.uploads)}"

    async def create(self, text, *, reply_to=None, quote=None, media_ids=()) -> Published:
        n = len(self.creates)
        self.creates.append(
            {"text": text, "reply_to": reply_to, "quote": quote, "media_ids": media_ids}
        )
        mode = self.fail_on.get(n)
        if mode == "fail":
            raise PulsarError("forbidden", "X said no")
        self.next_id += 1
        post_id = str(self.next_id)
        if mode in ("unknown-posted", "unknown-lost"):
            if mode == "unknown-posted":
                self.timeline.append(
                    RemotePost(post_id, f"u/{post_id}", self.clock[0], fp(text.upper()))
                )
            raise OutcomeUnknown("ReadTimeout after the request may have reached X")
        self.timeline.append(RemotePost(post_id, f"u/{post_id}", self.clock[0], fp(text)))
        return Published(post_id=post_id, url=f"u/{post_id}", text=text)

    async def delete(self, post_id: str) -> bool:
        return True

    async def recent_posts(self, since: datetime) -> RecentPosts:
        posts = tuple(p for p in reversed(self.timeline) if p.created_at >= since)
        return RecentPosts(posts=posts, complete=self.complete)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def channel(clock):
    return FakeChannel(clock=clock)


@pytest.fixture
def bound(channel):
    return Bound(alias="fake:acct", provider="fake", user_id="42", handle="acct", channel=channel)


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.png").write_bytes(PNG)
    return root


def make_publisher(paths, clock, media_root, **policy) -> Publisher:
    settings = Settings(
        provider_prices=tuple(
            (name, Prices(plain_post_usd=0.01, url_post_usd=0.2)) for name in ("fake", "x")
        ),
        media_roots=(media_root,),
        policy=PolicyConfig(**policy),
    )
    return Publisher(
        ledger=SqliteLedger(paths, clock=lambda: clock[0]),
        settings=settings,
        deny=(paths.home,),
        now=lambda: clock[0],
    )


@pytest.fixture
def publisher(paths, clock, media_root):
    return make_publisher(paths, clock, media_root)


def thread(*texts: str, **extra) -> Plan:
    return Plan.from_mapping({"posts": [{"text": t} for t in texts], **extra})


async def test_single_post_publishes_and_replays(publisher, bound, channel):
    prepared = publisher.prepare(thread("hello"), bound)
    out = await publisher.publish(prepared, caller="test")
    assert out.error is None and out.record.state == "published"
    assert out.receipt()["items"][0]["post_id"] == "501"
    again = await publisher.publish(publisher.prepare(thread("hello"), bound), caller="test")
    assert again.replayed and again.receipt()["items"][0]["post_id"] == "501"
    assert len(channel.creates) == 1


async def test_thread_replies_to_previous_item(publisher, bound, channel):
    plan = thread("one", "two", "three", reply_to="7")
    out = await publisher.publish(publisher.prepare(plan, bound), caller="test")
    assert out.record.state == "published"
    assert [c["reply_to"] for c in channel.creates] == ["7", "501", "502"]


async def test_thread_failing_at_item_two_resumes_from_item_two(publisher, bound, channel):
    channel.fail_on = {1: "fail"}
    prepared = publisher.prepare(thread("one", "two", "three"), bound)
    out = await publisher.publish(prepared, caller="test", idempotency_key="t1")
    assert out.record.state == "partial"
    assert out.error is not None and out.error.code == "forbidden"
    assert out.error.detail["published"] == [0] and out.error.detail["state"] == "partial"
    resumed = await publisher.publish(prepared, caller="test", idempotency_key="t1")
    assert resumed.error is None and resumed.record.state == "published"
    assert sorted(resumed.live) == [1, 2]
    # item 0 was not posted again; item 1 replies to item 0's post
    assert [c["text"] for c in channel.creates] == ["one", "two", "two", "three"]
    assert channel.creates[2]["reply_to"] == "501"


async def test_quote_applies_to_the_first_post_only(publisher, bound, channel):
    await publisher.publish(publisher.prepare(thread("a", "b", quote="9"), bound), caller="t")
    assert [c["quote"] for c in channel.creates] == ["9", None]


async def test_media_uploaded_per_item_with_alt(publisher, bound, channel, media_root):
    plan = Plan.from_mapping(
        {"posts": [{"text": "pic", "media": [{"path": str(media_root / "a.png"), "alt": "A"}]}]}
    )
    out = await publisher.publish(publisher.prepare(plan, bound), caller="t")
    assert out.record.state == "published"
    assert channel.uploads[0].alt == "A" and channel.creates[0]["media_ids"] == ("m1",)


async def test_unknown_blocks_retry_until_reconcile_finds_the_post(
    publisher, bound, channel, clock
):
    channel.fail_on = {0: "unknown-posted"}
    prepared = publisher.prepare(thread("Launch day"), bound)
    out = await publisher.publish(prepared, caller="t", idempotency_key="u1")
    assert out.record.state == "unknown" and out.error.code == "outcome_unknown"
    with pytest.raises(PulsarError) as exc:
        await publisher.publish(prepared, caller="t", idempotency_key="u1")
    assert exc.value.code == "outcome_unknown"
    assert len(channel.creates) == 1

    # the timeline shows X's rewritten text; the fingerprint here is case-insensitive
    report = await publisher.reconcile(bound)
    assert report[0]["state"] == "published"
    assert report[0]["items"][0] == {"idx": 0, "resolved": "published", "post_id": "501"}
    replay = await publisher.publish(prepared, caller="t", idempotency_key="u1")
    assert replay.replayed and replay.receipt()["items"][0]["post_id"] == "501"
    assert len(channel.creates) == 1


async def test_reconcile_marks_absent_posts_failed_only_after_grace(
    publisher, bound, channel, clock
):
    channel.fail_on = {0: "unknown-lost"}
    prepared = publisher.prepare(thread("never arrived"), bound)
    await publisher.publish(prepared, caller="t", idempotency_key="u2")
    early = await publisher.reconcile(bound)
    assert early[0]["state"] == "unknown"
    assert early[0]["items"][0]["reason"] == "within grace period"
    clock[0] = NOW + RECONCILE_GRACE + timedelta(seconds=1)
    channel.complete = False
    incomplete = await publisher.reconcile(bound)
    assert incomplete[0]["state"] == "unknown"
    channel.complete = True
    late = await publisher.reconcile(bound)
    assert late[0]["state"] == "failed" and late[0]["items"][0]["resolved"] == "absent"
    # failed means nothing reached X, so the same key may now publish
    again = await publisher.publish(prepared, caller="t", idempotency_key="u2")
    assert again.record.state == "published"


async def test_reconcile_never_matches_a_post_already_in_the_ledger(
    publisher, bound, channel, clock
):
    await publisher.publish(
        publisher.prepare(thread("same text"), bound), caller="t", idempotency_key="k1"
    )
    channel.fail_on = {1: "unknown-lost"}
    await publisher.publish(
        publisher.prepare(thread("same text"), bound), caller="t", idempotency_key="k2"
    )
    report = await publisher.reconcile(bound)
    assert report[0]["idempotency_key"] == "k2"
    assert report[0]["items"][0]["resolved"] is None


@pytest.mark.parametrize(
    ("policy", "code", "retryable"),
    [
        # a plan larger than the limit itself can never pass: not retryable
        ({"daily_budget_usd": 0.015}, "budget_exceeded", False),
        ({"monthly_budget_usd": 0.0}, "budget_exceeded", False),
        ({"max_posts_per_day": 1}, "daily_cap", False),
        ({"quiet_hours": (time(11, 0), time(13, 0))}, "quiet_hours", True),
    ],
)
async def test_policy_refuses_before_any_network_request(
    paths, clock, media_root, bound, channel, policy, code, retryable
):
    pub = make_publisher(paths, clock, media_root, **policy)
    with pytest.raises(PulsarError) as exc:
        await pub.publish(pub.prepare(thread("a", "b"), bound), caller="t", idempotency_key="p")
    assert exc.value.code == code and exc.value.retryable is retryable
    assert channel.creates == [] and channel.uploads == []
    assert pub.ledger.get_plan("p") is None


async def test_budget_counts_what_was_already_spent(paths, clock, media_root, bound, channel):
    pub = make_publisher(paths, clock, media_root, daily_budget_usd=0.02)
    await pub.publish(pub.prepare(thread("first"), bound), caller="t")
    with pytest.raises(PulsarError) as exc:
        await pub.publish(pub.prepare(thread("second", "third"), bound), caller="t")
    assert exc.value.code == "budget_exceeded"
    assert len(channel.creates) == 1


async def test_not_before_in_the_future_is_not_due(publisher, bound, channel):
    plan = thread("later", not_before="2026-09-27T00:00:00Z")
    with pytest.raises(PulsarError) as exc:
        await publisher.publish(publisher.prepare(plan, bound), caller="t")
    assert exc.value.code == "not_due" and exc.value.detail["retry_after"].startswith("2026-09-27")
    assert channel.creates == []


async def test_offline_checks_run_before_anything(publisher, bound, channel, media_root):
    secret_alt = Plan.from_mapping(
        {
            "text": "pic",
            "media": [
                {"path": str(media_root / "a.png"), "alt": "key sk-ant-abcdefghijklmnopqrstu"}
            ],
        }
    )
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(secret_alt, bound)
    assert exc.value.code == "secret_detected"
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(thread("x" * 101), bound)
    assert exc.value.code == "invalid_text" and exc.value.detail["post"] == 0
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(thread("a", reply_to="../1"), bound)
    assert exc.value.code == "invalid_argument"
    no_threads = replace(bound, channel=FakeChannel(caps=replace(CAPS, threads=False)))
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(thread("a", "b"), no_threads)
    assert exc.value.code == "unsupported"
    assert channel.creates == [] and not publisher.ledger.history()


async def test_media_outside_roots_is_refused_offline(publisher, bound, tmp_path):
    (tmp_path / "elsewhere.png").write_bytes(PNG)
    plan = Plan.from_mapping(
        {"text": "pic", "media": [{"path": str(tmp_path / "elsewhere.png"), "alt": "A"}]}
    )
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(plan, bound)
    assert exc.value.code == "invalid_media"


async def test_digest_binds_media_bytes(publisher, bound, media_root):
    plan = Plan.from_mapping(
        {"text": "pic", "media": [{"path": str(media_root / "a.png"), "alt": "A"}]}
    )
    before = publisher.prepare(plan, bound).digest
    (media_root / "a.png").write_bytes(PNG + b"\x00changed")
    assert publisher.prepare(plan, bound).digest != before


def test_report_is_what_a_human_approves(publisher, bound):
    report = publisher.prepare(thread("one https://x.co", "two"), bound).report()
    assert report["estimated_cost_usd"] == 0.21
    assert [p["text"] for p in report["posts"]] == ["one https://x.co", "two"]
    assert report["digest"].startswith("sha256:")


# -- review findings: reservations, v1 rows, slow uploads ---------------------------


class GatedChannel(FakeChannel):
    """Holds ``create`` of the texts in ``hold`` (or every upload) until released."""

    def __init__(self, *, clock, hold=(), gate_uploads=False):
        super().__init__(clock=clock)
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()  # set once a call is parked on ``release``
        self.hold = set(hold)
        self.gate_uploads = gate_uploads

    async def _park(self):
        self.waiting.set()
        await self.release.wait()

    async def upload(self, media):
        if self.gate_uploads:
            await self._park()
        return await super().upload(media)

    async def create(self, text, **kw):
        if text in self.hold:
            await self._park()
        return await super().create(text, **kw)


def bound_to(channel, *, alias="fake:acct", provider="fake", user_id="42", handle="acct"):
    return Bound(alias=alias, provider=provider, user_id=user_id, handle=handle, channel=channel)


async def test_a_thread_in_progress_reserves_its_unsent_posts(paths, clock, media_root):
    # $0.02 a day at $0.01 a post: thread A (two posts) takes all of it.
    pub = make_publisher(paths, clock, media_root, daily_budget_usd=0.02)
    channel = GatedChannel(clock=clock, hold={"a1"})
    bound = bound_to(channel)
    a = asyncio.create_task(pub.publish(pub.prepare(thread("a1", "a2"), bound), caller="A"))
    await channel.waiting.wait()  # A claimed: a1 submitting, a2 pending
    with pytest.raises(PulsarError) as exc:
        await pub.publish(pub.prepare(thread("b1"), bound), caller="B")
    assert exc.value.code == "budget_exceeded"
    channel.release.set()
    assert (await a).record.state == "published"
    assert [c["text"] for c in channel.creates] == ["a1", "a2"]


async def test_a_pending_row_reclaimed_is_not_counted_twice(paths, clock, media_root, bound):
    pub = make_publisher(paths, clock, media_root, daily_budget_usd=0.02)
    prepared = pub.prepare(thread("a1", "a2"), bound)
    # Claimed but never sent, as by a sender that died before its first post.
    pub.ledger.claim_plan(
        key="p1", tool="publish", digest=prepared.digest, provider="fake", account=bound.ref,
        caller="dead", items=[ItemIntent("h", fp(t), 0.01) for t in ("a1", "a2")],
        admit=None, day_start=NOW.replace(hour=0), month_start=NOW.replace(day=1, hour=0),
    )  # fmt: skip
    out = await pub.publish(prepared, caller="t", idempotency_key="p1")
    assert out.record.state == "published"


def v1_bound(channel) -> Bound:
    return bound_to(
        channel, alias="x:constworks", provider="x", user_id="1234567890", handle="constworks"
    )


def legacy(pub, bound, text, digest):
    """A ``create_post`` retry of a v1 row: same request digest as the stored row."""
    return replace(pub.prepare(thread(text), bound), digest=digest)


async def test_reconcile_never_finds_a_v1_row_absent(paths, clock, media_root):
    build_v1_ledger(paths)  # k-unk: "lost", unknown since 2026-09-20 10:00
    pub = make_publisher(paths, clock, media_root)
    channel = FakeChannel(clock=clock)
    bound = v1_bound(channel)
    posted = datetime(2026, 9, 20, 10, 0, 1, tzinfo=UTC)
    channel.timeline.append(RemotePost("777", "u/777", posted, fp("lost")))
    [before] = await pub.reconcile(bound)
    assert before["state"] == "unknown"
    assert before["items"][0]["resolved"] is None
    assert "no fingerprint" in before["items"][0]["reason"]

    # Repeating the request still refuses, but attaches the fingerprint...
    with pytest.raises(PulsarError) as exc:
        await pub.publish(
            legacy(pub, bound, "lost", "d-unk"), idempotency_key="k-unk", caller="t",
            tool="create_post",
        )  # fmt: skip
    assert exc.value.code == "outcome_unknown" and channel.creates == []
    # ...so reconcile can now find the post that went out.
    [after] = await pub.reconcile(bound)
    assert after["state"] == "published" and after["items"][0]["post_id"] == "777"


async def test_retrying_a_v1_failed_row_takes_the_new_fingerprint_and_price(
    paths, clock, media_root
):
    build_v1_ledger(paths)  # k-fail: "dup", failed
    pub = make_publisher(paths, clock, media_root)
    channel = FakeChannel(clock=clock, fail_on={0: "unknown-posted"})
    bound = v1_bound(channel)
    out = await pub.publish(
        legacy(pub, bound, "dup", "d-fail"), idempotency_key="k-fail", caller="t",
        tool="create_post",
    )  # fmt: skip
    [item] = out.record.items
    assert item.state == "unknown" and item.fingerprint == fp("dup") and item.est_cost_usd == 0.01
    clock[0] = NOW + RECONCILE_GRACE + timedelta(minutes=1)
    reports = {r["idempotency_key"]: r for r in await pub.reconcile(bound)}
    assert reports["k-fail"]["state"] == "published"
    assert reports["k-unk"]["state"] == "unknown", "the v1 row without a fingerprint stays"
    assert len(channel.creates) == 1


async def test_reconcile_during_a_slow_upload_cannot_double_post(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    slow = GatedChannel(clock=clock, gate_uploads=True)
    plan = Plan.from_mapping(
        {"text": "video", "media": [{"path": str(media_root / "a.png"), "alt": "A"}]}
    )
    first = asyncio.create_task(
        pub.publish(pub.prepare(plan, bound_to(slow)), caller="A", idempotency_key="k")
    )
    await slow.waiting.wait()  # item 0 submitting, its upload still running
    clock[0] = NOW + STALE_SUBMITTING + timedelta(minutes=1)
    [report] = await pub.reconcile(bound_to(slow))
    assert report["items"][0]["resolved"] == "absent"  # true: nothing was posted yet

    retry_channel = FakeChannel(clock=clock)
    retry = await pub.publish(
        pub.prepare(plan, bound_to(retry_channel)), caller="B", idempotency_key="k"
    )
    assert retry.record.state == "published"
    slow.release.set()
    out = await first
    assert out.error is not None and "nothing was posted" in out.error.message
    assert slow.creates == [] and len(retry_channel.creates) == 1
    settled = pub.ledger.get_plan("k")
    assert settled.state == "published" and settled.items[0].post_id == "501"


async def test_core_scans_post_text_whatever_the_channel_does(publisher, bound):
    # FakeChannel.check_post does not scan: the publisher must.
    token = "ghp_" + "a" * 36
    with pytest.raises(PulsarError) as exc:
        publisher.prepare(thread("fine", f"oops {token}"), bound)
    assert exc.value.code == "secret_detected" and exc.value.detail["post"] == 1
    prepared = publisher.prepare(thread("fine"), bound)
    with pytest.raises(PulsarError) as exc:
        await publisher.publish(prepared, caller=token)
    assert exc.value.code == "secret_detected"
    assert publisher.ledger.history() == []


# -- preflight (dry runs) and reconcile isolation -------------------------------------


async def test_preflight_refuses_what_publish_would_and_writes_nothing(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root, daily_budget_usd=0.01)
    channel = FakeChannel(clock=clock)
    bound = bound_to(channel)
    pub.preflight(pub.prepare(thread("one"), bound))  # admitted: nothing raised
    await pub.publish(pub.prepare(thread("one"), bound), caller="t")
    rows = pub.ledger.history()
    with pytest.raises(PulsarError) as exc:
        pub.preflight(pub.prepare(thread("two"), bound))
    assert exc.value.code == "budget_exceeded"
    assert pub.ledger.history() == rows and len(channel.creates) == 1
    # A replay of what was already published costs nothing, so it is admitted.
    pub.preflight(pub.prepare(thread("one"), bound))


async def test_preflight_refuses_a_plan_that_is_not_due(publisher, bound):
    later = (NOW + timedelta(hours=1)).isoformat()
    with pytest.raises(PulsarError) as exc:
        publisher.preflight(publisher.prepare(thread("soon", not_before=later), bound))
    assert exc.value.code == "not_due"


class FlakyTimeline(FakeChannel):
    """A timeline read that is rate limited once, then works."""

    reads: int = 0

    async def recent_posts(self, since: datetime) -> RecentPosts:
        self.reads += 1
        if self.reads == 1:
            raise PulsarError("rate_limited", "slow down", retryable=True)
        return await super().recent_posts(since)


async def test_reconcile_reports_a_failing_row_and_settles_the_rest(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    channel = FlakyTimeline(clock=clock, fail_on={0: "unknown-lost", 1: "unknown-lost"})
    bound = bound_to(channel)
    for key, text in (("k1", "first"), ("k2", "second")):
        out = await pub.publish(pub.prepare(thread(text), bound), caller="t", idempotency_key=key)
        assert out.record.state == "unknown"
    clock[0] = NOW + RECONCILE_GRACE + timedelta(minutes=1)
    results = await pub.reconcile(bound)
    assert [set(r) for r in results] == [set(results[0])] * 2, "one shape per result"
    failed, settled = results
    assert failed["error"]["code"] == "rate_limited" and failed["state"] == "unknown"
    assert pub.ledger.get_plan(failed["idempotency_key"]).state == "unknown", "left as it was"
    assert settled["error"] is None and settled["state"] == "failed"


class ExpiredTimeline(FakeChannel):
    """A timeline read whose credentials are gone."""

    reads: int = 0

    async def recent_posts(self, since: datetime) -> RecentPosts:
        self.reads += 1
        raise AuthExpired("refresh token rejected")


class BrokenTimeline(FakeChannel):
    """A timeline read that hits a bug (not a provider failure)."""

    async def recent_posts(self, since: datetime) -> RecentPosts:
        raise ValueError("bad timestamp")


async def test_reconcile_stops_at_auth_expired_instead_of_reporting_each_row(
    paths, clock, media_root
):
    pub = make_publisher(paths, clock, media_root)
    channel = ExpiredTimeline(clock=clock, fail_on={0: "unknown-lost", 1: "unknown-lost"})
    bound = bound_to(channel)
    for key, text in (("k1", "first"), ("k2", "second")):
        await pub.publish(pub.prepare(thread(text), bound), caller="t", idempotency_key=key)
    with pytest.raises(AuthExpired):
        await pub.reconcile(bound)
    assert channel.reads == 1, "no call per remaining row"


async def test_a_bug_in_reconcile_is_internal_not_a_provider_error(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    channel = BrokenTimeline(clock=clock, fail_on={0: "unknown-lost"})
    bound = bound_to(channel)
    await pub.publish(pub.prepare(thread("one"), bound), caller="t", idempotency_key="k1")
    (result,) = await pub.reconcile(bound)
    assert result["error"]["code"] == "internal" and result["error"]["retryable"] is False


class FailingLedger(SqliteLedger):
    """A ledger whose settle writes fail, as under a held lock or a full disk."""

    def item_unknown(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def item_failed(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


async def test_a_failing_settle_write_never_replaces_the_send_error(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    pub.ledger = FailingLedger(paths)
    channel = FakeChannel(clock=clock, fail_on={0: "unknown-lost"})
    out = await pub.publish(pub.prepare(thread("one"), bound_to(channel)), caller="t",
                            idempotency_key="k1")  # fmt: skip
    assert out.error is not None and out.error.code == "outcome_unknown"
