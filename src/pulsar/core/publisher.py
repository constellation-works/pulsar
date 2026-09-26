"""Publishing a plan: validate, admit, claim, then one recorded step at a time.

The order is the safety argument:

1. **Validate offline**: capabilities, provider text rules, reply/quote ids,
   the secret scanner over every post and every alt text, and media loaded
   under confinement and checked against the provider's rules. Nothing is
   written and nothing is sent until all of it passes.
2. **Digest** the plan, with the loaded media bytes' hashes, so the key and
   (in phase 4) the approval are bound to exactly what goes out.
3. **Claim** the ledger row. Policy (budget, daily cap, quiet hours) runs
   inside the claim's write transaction, and a claimed plan's unsent posts
   are reserved against the budget until they are sent or settled, so two
   concurrent callers cannot both spend the last dollar.
4. **Publish item by item**. Each post is marked ``submitting`` before its
   media upload and re-stamped (a compare-and-set) just before the post
   request leaves, and settled after. A thread replies to the previous
   item's id, and a resumed thread continues from the last published item.
   A definitive failure after at least one post leaves the row ``partial``;
   an ambiguous one leaves it ``unknown`` for ``reconcile``.

Media are uploaded per item, just before the item is posted, so no media id
has to outlive the call. The one exception is the legacy ``create_post``
tool, whose caller uploaded earlier and passes media ids: those ride along
as ``PreparedPost.uploaded``, and the surface binds them into the digest.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .adapter import Channel, LoadedMedia, PostCheck, Published
from .errors import (
    API_ERROR,
    INTERNAL,
    INVALID_MEDIA,
    NOT_DUE,
    SECRET_DETECTED,
    UNSUPPORTED,
    AuthExpired,
    OutcomeUnknown,
    PulsarError,
)
from .guard import scan_for_secrets
from .ledger import (
    PUBLISHED,
    SKIPPED,
    AccountRef,
    ItemIntent,
    Ledger,
    PlanRecord,
    check_key,
    default_key,
    is_ambiguous,
    parse_ts,
)
from .media import load_ref
from .plan import MediaRef, Plan, PostSpec
from .policy import Policy, day_window, month_window
from .settings import Settings

log = logging.getLogger(__name__)

# Reconcile waits this long after a post was sent before treating "not on the
# timeline" as "not posted": X's timeline can lag a fresh post.
RECONCILE_GRACE = timedelta(minutes=5)
# Rows stuck in `submitting` this long belong to a sender that died mid-call.
STALE_SUBMITTING = timedelta(minutes=10)
# Look this far before the first submit when listing the account's posts.
CLOCK_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class Bound:
    """One account, resolved and checked by the surface, with its open channel."""

    alias: str
    provider: str
    user_id: str
    handle: str
    channel: Channel

    @property
    def ref(self) -> AccountRef:
        return AccountRef(
            alias=self.alias, provider=self.provider, user_id=self.user_id, handle=self.handle
        )


@dataclass(frozen=True)
class PreparedPost:
    spec: PostSpec
    check: PostCheck
    media: tuple[LoadedMedia, ...]
    uploaded: tuple[str, ...] = ()  # provider media ids uploaded before this call


@dataclass(frozen=True)
class Prepared:
    """A plan validated for one account: what would be published, and its cost."""

    plan: Plan
    bound: Bound
    digest: str
    posts: tuple[PreparedPost, ...]

    @property
    def estimated_cost_usd(self) -> float:
        return round(sum(p.check.estimated_cost_usd for p in self.posts), 6)

    def report(self) -> dict[str, Any]:
        return {
            "account": self.bound.alias,
            "provider": self.bound.provider,
            "digest": self.digest,
            "estimated_cost_usd": self.estimated_cost_usd,
            "reply_to": self.plan.reply_to,
            "quote": self.plan.quote,
            "not_before": self.plan.not_before.isoformat() if self.plan.not_before else None,
            "posts": [
                {
                    "text": p.check.text,
                    "length": p.check.length,
                    "max_length": self.bound.channel.capabilities.max_length,
                    "has_url": p.check.has_url,
                    "estimated_cost_usd": p.check.estimated_cost_usd,
                    "media": [
                        {"mime": m.mime, "bytes": len(m.data), "sha256": m.sha256, "alt": m.alt}
                        for m in p.media
                    ],
                    **({"media_ids": list(p.uploaded)} if p.uploaded else {}),
                }
                for p in self.posts
            ],
            "pricing_note": "from the configured price table; verify on the provider's portal",
        }


@dataclass
class Outcome:
    """What a publish call did. ``error`` is set unless every post is live."""

    record: PlanRecord
    replayed: bool = False
    error: PulsarError | None = None
    # Posts this call made, by item index: the truth even when the ledger
    # could not record them (the record then still says ``submitting``).
    live: dict[int, Published] = field(default_factory=dict)

    def receipt(self) -> dict[str, Any]:
        items = [
            {"idx": i.idx, "state": i.state, "post_id": i.post_id, "url": i.url}
            for i in self.record.items
        ]
        return {
            "idempotency_key": self.record.key,
            "state": self.record.state,
            "account": self.record.account_alias,
            "digest": self.record.digest,
            "replayed": self.replayed,
            "items": items,
            "note": self.record.note,
        }


class Publisher:
    def __init__(
        self,
        *,
        ledger: Ledger,
        settings: Settings,
        deny: Sequence[Path] = (),
        media_base: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.policy = Policy(settings.policy)
        self.deny = tuple(deny)
        self.media_base = media_base
        self._now = now

    # -- validate -------------------------------------------------------------

    def prepare(self, plan: Plan, bound: Bound) -> Prepared:
        """Everything that can be checked without the network. Raises on the first problem."""
        channel = bound.channel
        caps = channel.capabilities
        posts = plan.posts_for(bound.provider)
        if len(posts) > 1 and not caps.threads:
            raise PulsarError(UNSUPPORTED, f"{caps.provider} does not support threads")
        if plan.reply_to is not None and not caps.reply:
            raise PulsarError(UNSUPPORTED, f"{caps.provider} does not support replies")
        if plan.quote is not None and not caps.quote:
            raise PulsarError(UNSUPPORTED, f"{caps.provider} does not support quotes")
        channel.check_target(reply_to=plan.reply_to, quote=plan.quote)
        prices = self.settings.prices_for(bound.provider)
        loaded: dict[MediaRef, LoadedMedia] = {}
        prepared: list[PreparedPost] = []
        for idx, spec in enumerate(posts):
            # Every provider's check_post scans too; this one does not depend on it.
            hits = scan_for_secrets(spec.text)
            if hits:
                raise PulsarError(
                    SECRET_DETECTED,
                    "post text contains something that looks like a credential",
                    detail={"matched": hits, "post": idx},
                )
            try:
                check = channel.check_post(spec, prices)
            except PulsarError as exc:
                exc.detail = {**(exc.detail or {}), "post": idx}
                raise
            media = tuple(self._load(ref, idx, loaded) for ref in spec.media)
            for m in media:
                if m.mime not in caps.media.mime_types:
                    raise PulsarError(
                        INVALID_MEDIA,
                        f"{caps.provider} does not accept {m.mime}",
                        detail={"post": idx},
                    )
                if len(m.data) > caps.media.limit_for(m.mime):
                    raise PulsarError(
                        INVALID_MEDIA,
                        f"media is {len(m.data)} bytes; {caps.provider} allows "
                        f"{caps.media.limit_for(m.mime)} for {m.mime}",
                        detail={"post": idx},
                    )
            channel.check_media(media)
            prepared.append(PreparedPost(spec=spec, check=check, media=media))
        # The digest covers every variant's media, including ones this provider
        # does not post, so one digest identifies the plan for every account.
        for ref in plan.media_refs():
            self._load(ref, None, loaded)
        digest = plan.digest(lambda ref: loaded[ref].sha256)
        return Prepared(plan=plan, bound=bound, digest=digest, posts=tuple(prepared))

    def _load(
        self, ref: MediaRef, idx: int | None, cache: dict[MediaRef, LoadedMedia]
    ) -> LoadedMedia:
        if ref in cache:
            return cache[ref]
        hits = scan_for_secrets(ref.alt)
        if hits:
            raise PulsarError(
                SECRET_DETECTED,
                "alt text contains something that looks like a credential",
                detail={"matched": hits, "post": idx},
            )
        media = load_ref(
            ref.path,
            ref.alt,
            roots=self.settings.media_roots,
            deny=self.deny,
            base=self.media_base,
        )
        cache[ref] = media
        return media

    # -- publish --------------------------------------------------------------

    def preflight(self, prepared: Prepared, *, idempotency_key: str | None = None) -> None:
        """The checks ``publish`` makes before claiming, without claiming: when
        the plan is due and whether the policy admits it now. A dry run calls
        this so it refuses what the live call would (STD-02 §R34)."""
        now = self._now()
        self._check_due(prepared.plan, now)
        key = check_key(idempotency_key) or default_key(prepared.digest, prepared.bound.user_id)
        # Read-only: a dry run neither creates the ledger nor migrates it.
        ledger = self.ledger.reader()
        remaining = self._remaining(prepared, key, ledger)
        tz = self.settings.policy.tz
        day_start, _ = day_window(now, tz)
        month_start, _ = month_window(now, tz)
        usage = ledger.usage(prepared.bound.alias, day_start=day_start, month_start=month_start)
        self._admit(usage, remaining, now)

    @staticmethod
    def _check_due(plan: Plan, now: datetime) -> None:
        if plan.not_before is not None and now < plan.not_before:
            raise PulsarError(
                NOT_DUE,
                f"not before {plan.not_before.isoformat()}",
                detail={"retry_after": plan.not_before.isoformat()},
            )

    def _intents(self, prepared: Prepared) -> list[ItemIntent]:
        channel = prepared.bound.channel
        return [
            ItemIntent(
                text_sha256=hashlib.sha256(p.check.text.encode("utf-8")).hexdigest(),
                fingerprint=channel.fingerprint(p.check.text),
                est_cost_usd=p.check.estimated_cost_usd,
            )
            for p in prepared.posts
        ]

    def _remaining(
        self, prepared: Prepared, key: str, ledger: Ledger | None = None
    ) -> list[ItemIntent]:
        """The posts a claim of ``key`` would still send: a replay pays only for those."""
        already = (ledger or self.ledger).get_plan(key)
        done = {i.idx for i in already.items if i.state == PUBLISHED} if already else set[int]()
        return [intent for idx, intent in enumerate(self._intents(prepared)) if idx not in done]

    def _admit(self, usage: Any, remaining: Sequence[ItemIntent], now: datetime) -> None:
        self.policy.check(
            usage=usage,
            planned_cost_usd=round(sum(i.est_cost_usd for i in remaining), 6),
            planned_posts=len(remaining),
            now=now,
        )

    async def publish(
        self,
        prepared: Prepared,
        *,
        idempotency_key: str | None = None,
        caller: str,
        tool: str = "publish",
    ) -> Outcome:
        now = self._now()
        plan, bound = prepared.plan, prepared.bound
        self._check_due(plan, now)
        key = check_key(idempotency_key) or default_key(prepared.digest, bound.user_id)
        if scan_for_secrets(caller):
            raise PulsarError(SECRET_DETECTED, "caller looks like it contains a credential")
        intents = self._intents(prepared)
        tz = self.settings.policy.tz
        day_start, _ = day_window(now, tz)
        month_start, _ = month_window(now, tz)
        remaining = self._remaining(prepared, key)

        def admit(usage: Any) -> None:
            self._admit(usage, remaining, now)

        record = self.ledger.claim_plan(
            key=key,
            tool=tool,
            digest=prepared.digest,
            provider=bound.provider,
            account=bound.ref,
            caller=caller,
            items=intents,
            admit=admit,
            day_start=day_start,
            month_start=month_start,
        )
        if record.state in (PUBLISHED, SKIPPED):
            return Outcome(record=record, replayed=True)
        return await self._run(prepared, key, record)

    @staticmethod
    def _settle(record: Callable[[str, int, PulsarError], object], key: str, idx: int,
                error: PulsarError) -> None:  # fmt: skip
        """Record an item's failed or unknown outcome from inside an ``except``.

        A ledger failure here is logged, never raised over the error or
        cancellation being handled (STD-03 §R4); the item stays
        ``submitting``, which reconcile settles.
        """
        try:
            record(key, idx, error)
        except Exception:
            log.exception(
                "ledger: could not record %s item %d as %s; it stays submitting",
                key, idx, error.code,
            )  # fmt: skip

    def _settle_finish(self, key: str) -> None:
        try:
            self.ledger.finish(key)
        except Exception:
            log.exception("ledger: could not finish %s", key)

    async def _run(self, prepared: Prepared, key: str, record: PlanRecord) -> Outcome:
        plan, channel = prepared.plan, prepared.bound.channel
        post_ids = {i.idx: i.post_id for i in record.items if i.state == PUBLISHED}
        error: PulsarError | None = None
        live: dict[int, Published] = {}
        lost = False  # the row stopped being ours: leave it to whoever holds it now
        for idx, post in enumerate(prepared.posts):
            if idx in post_ids:
                continue
            reply_to = post_ids.get(idx - 1) if idx else plan.reply_to
            stamp = self.ledger.begin_item(key, idx)
            media_ids: list[str] = list(post.uploaded)
            try:
                for m in post.media:
                    media_ids.append(await channel.upload(m))
            except PulsarError as exc:
                # An upload publishes nothing: the item definitively did not post.
                self._settle(self.ledger.item_failed, key, idx, exc)
                error = exc
                break
            except Exception as exc:
                error = _unexpected(exc)
                self._settle(self.ledger.item_failed, key, idx, error)
                break
            except BaseException as exc:  # cancelled: settle, then let it propagate
                self._settle(self.ledger.item_failed, key, idx, _unexpected(exc))
                self._settle_finish(key)
                raise
            if self.ledger.item_sending(key, idx, stamp) is None:
                # A slow upload let reconcile settle this item (or a retry
                # take it over). Posting now could publish it twice.
                error = PulsarError(
                    API_ERROR,
                    "this post was settled by another process while its media uploaded; "
                    "nothing was posted",
                    retryable=False,
                )
                lost = True
                break
            try:
                published = await channel.create(
                    post.check.text,
                    reply_to=reply_to,
                    quote=plan.quote if idx == 0 else None,
                    media_ids=tuple(media_ids),
                )
            except OutcomeUnknown as exc:
                self._settle(self.ledger.item_unknown, key, idx, exc)
                error = exc
                break
            except PulsarError as exc:
                self._settle(self.ledger.item_failed, key, idx, exc)
                error = exc
                break
            except BaseException as exc:
                # A bug or a cancellation with the request possibly in flight:
                # the post may be live, so the item is unknown, never failed.
                unknown = OutcomeUnknown(f"{exc.__class__.__name__} while posting")
                self._settle(self.ledger.item_unknown, key, idx, unknown)
                if not isinstance(exc, Exception):
                    self._settle_finish(key)
                    raise
                log.error("publish %s item %d: %r", key, idx, exc)
                error = unknown
                break
            live[idx] = published
            post_ids[idx] = published.post_id
            try:
                self.ledger.item_published(
                    key,
                    idx,
                    post_id=published.post_id,
                    url=published.url,
                    media_ids=tuple(media_ids),
                )
            except Exception:
                # The post is live whatever happens here. Log it so it can be
                # recovered, and stop: a thread must not go on unrecorded.
                log.exception(
                    "ledger: could not record %s item %d as published (post_id=%s url=%s);"
                    " the item stays submitting",
                    key,
                    idx,
                    published.post_id,
                    published.url,
                )
                if idx + 1 < len(prepared.posts):
                    error = PulsarError(
                        API_ERROR,
                        "a post went live but the ledger could not record it; stopped the thread",
                        retryable=False,
                    )
                break
        try:
            final = (self.ledger.get_plan(key) or record) if lost else self.ledger.finish(key)
        except Exception:
            log.exception("ledger: could not finish %s", key)
            final = record
        if error is not None:
            error.detail = {
                **(error.detail or {}),
                "idempotency_key": key,
                "state": final.state,
                "published": sorted(post_ids),
            }
        return Outcome(record=final, error=error, live=live)

    # -- reconcile ------------------------------------------------------------

    async def reconcile(self, bound: Bound) -> list[dict[str, Any]]:
        """Settle this account's unknown (and abandoned submitting) rows from its timeline.

        An item is published if a post with its fingerprint appeared after it
        was sent and is not already another item's post; failed if the
        provider's listing is complete, the grace period has passed and no
        such post exists; otherwise it stays unknown. An item without a
        fingerprint (a row from before ledger v2) is never found absent.

        Verdicts are written in one transaction only if the row is still as
        it was listed; a row a live sender touched meanwhile is left alone.
        One row that cannot be reconciled (a rate limit, a bad timestamp) is
        reported with its key and cause, and the others still run (STD-02
        §R32); its row is left as it was. ``auth_expired`` is the account's,
        not a row's, and ends the run.
        """
        now = self._now()
        results: list[dict[str, Any]] = []
        for record in self.ledger.unresolved(stale_after=STALE_SUBMITTING, now=now):
            if record.account_alias != bound.alias:
                continue
            try:
                results.append(await self._reconcile_one(record, bound, now))
            except AuthExpired:
                # The account, not the row: every other row would fail the
                # same way, and the caller must see it to mark the account.
                raise
            except PulsarError as exc:
                results.append(_reconcile_result(record.key, record.state, error=exc))
            except Exception as exc:
                log.exception("reconcile %s: unexpected error", record.key)
                bug = PulsarError(
                    INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False
                )
                results.append(_reconcile_result(record.key, record.state, error=bug))
        return results

    async def _reconcile_one(
        self, record: PlanRecord, bound: Bound, now: datetime
    ) -> dict[str, Any]:
        open_items = [i for i in record.items if is_ambiguous(i.state)]
        seen = {i.idx: (i.state, i.submitted_at) for i in open_items}
        created = parse_ts(record.created_at)
        sent = [parse_ts(i.submitted_at) for i in open_items if i.submitted_at is not None]
        since = (min(sent) if sent else created) - CLOCK_SKEW
        recent = await bound.channel.recent_posts(since)
        taken = self.ledger.known_post_ids([p.post_id for p in recent.posts])
        verdicts: list[dict[str, Any]] = []
        resolved: dict[int, tuple[str, str | None] | None] = {}
        for item in open_items:
            sent_at = parse_ts(item.submitted_at) if item.submitted_at else created
            if item.fingerprint is None:
                verdicts.append(
                    {
                        "idx": item.idx,
                        "resolved": None,
                        "reason": "no fingerprint (recorded before ledger v2): repeat the "
                        "same request to attach one, then reconcile again",
                    }
                )
                continue
            match = next(
                (
                    p
                    for p in reversed(recent.posts)  # oldest first
                    if p.fingerprint == item.fingerprint
                    and p.created_at >= sent_at - CLOCK_SKEW
                    and p.post_id not in taken
                ),
                None,
            )
            if match is not None:
                resolved[item.idx] = (match.post_id, match.url)
                taken.add(match.post_id)
                verdicts.append(
                    {"idx": item.idx, "resolved": "published", "post_id": match.post_id}
                )
            elif recent.complete and now - sent_at >= RECONCILE_GRACE:
                resolved[item.idx] = None
                verdicts.append({"idx": item.idx, "resolved": "absent"})
            else:
                reason = "listing incomplete" if not recent.complete else "within grace period"
                verdicts.append({"idx": item.idx, "resolved": None, "reason": reason})
        final = self.ledger.settle(record.key, seen=seen, verdicts=resolved)
        if final is None:
            return _reconcile_result(
                record.key,
                "changed",
                note="a sender updated this row while reconcile ran; left as is",
            )
        return _reconcile_result(record.key, final.state, items=verdicts)


def _reconcile_result(
    key: str,
    state: str,
    *,
    items: list[dict[str, Any]] | None = None,
    note: str | None = None,
    error: PulsarError | None = None,
) -> dict[str, Any]:
    """One reconcile result; every field is present (``null`` when it does not apply)."""
    return {
        "idempotency_key": key,
        "state": state,
        "items": items or [],
        "note": note,
        "error": error.to_result() if error is not None else None,
    }


def _unexpected(exc: BaseException) -> PulsarError:
    return PulsarError(API_ERROR, f"unexpected {exc.__class__.__name__}", retryable=True)
