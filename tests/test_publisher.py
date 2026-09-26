"""The publisher over a fake channel: no HTTP, every step observable."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta

import pytest

from pulsar.core.adapter import (
    Capabilities,
    Identity,
    LoadedMedia,
    MediaCapabilities,
    PostCheck,
    Published,
    RecentPosts,
    RemotePost,
)
from pulsar.core.errors import OutcomeUnknown, PulsarError
from pulsar.core.ledger import Ledger
from pulsar.core.plan import Plan, PostSpec
from pulsar.core.publisher import RECONCILE_GRACE, Bound, Publisher
from pulsar.core.settings import PolicyConfig, Prices, Settings

from .media_samples import PNG

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
        provider_prices=(("fake", Prices(plain_post_usd=0.01, url_post_usd=0.2)),),
        media_roots=(media_root,),
        policy=PolicyConfig(**policy),
    )
    return Publisher(
        ledger=Ledger(paths, clock=lambda: clock[0]),
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
