"""Bluesky as a ``Channel``: capabilities, offline checks, and one network step per call.

A post is an ``app.bsky.feed.post`` record in the account's repository; its
id is the record's AT URI (``at://<did>/app.bsky.feed.post/<rkey>``), and
reply and quote targets are AT URIs too.

``create`` builds the record the network needs that the plan does not say:

- **facets**: links, ``#tags`` and ``@mentions`` as UTF-8 byte ranges
  (``text.detect_facets``). A mention's handle is resolved to its DID first
  (``com.atproto.identity.resolveHandle``); one that does not resolve stays
  plain text, as in the Bluesky app.
- **reply refs**: a reply names its parent and its thread's root, each by
  URI and CID. Posts this channel created are remembered; others are looked
  up (``com.atproto.repo.getRecord`` for the account's own, else
  ``app.bsky.feed.getPosts``), all before the record is sent.
- **embeds**: images (up to 4, each with its alt text) or one video, from
  the blobs ``upload`` stored; a quote is a record embed, or
  ``recordWithMedia`` when the post also has media.

``upload`` sends the bytes as a blob and returns its CID; the blob and its
alt text stay in this channel until ``create`` embeds them, so a media id is
good only on the channel that uploaded it (the publisher uploads each item's
media just before posting it).

Reconcile: Bluesky stores the text as sent, so ``fingerprint`` only
normalises (NFC, whitespace collapsed). ``recent_posts`` lists the account's
post records (``com.atproto.repo.listRecords``) back to ``since``; the
listing is the repository itself, so it is complete unless it hit the page
cap or a record could not be read.

Reads are free on Bluesky: ``mentions`` pages the account's notifications for
mentions, replies and quotes and takes their counts from
``app.bsky.feed.getPosts``; ``own_posts`` pages the account's author feed
(reposts left out). Bluesky reports no impressions or clicks.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from pulsar.internal.errors import (
    API_ERROR,
    INVALID_ARGUMENT,
    INVALID_MEDIA,
    NOT_FOUND,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.internal.fs import as_list, as_object, obj

from ..contract import (
    IMAGE_MIME_TYPES,
    VIDEO_MIME_TYPES,
    Capabilities,
    Identity,
    LoadedMedia,
    MediaCapabilities,
    Mention,
    Metrics,
    OwnPost,
    Page,
    PostCheck,
    PostSpec,
    Prices,
    Published,
    RecentPosts,
    RemotePost,
)
from .config import APP_URL, MAX_POST_GRAPHEMES, POST_COLLECTION
from .interfaces import BlueskyApi
from .text import Facet, detect_facets, grapheme_count, validate_text

MAX_MEDIA_PER_POST = 4
# app.bsky.embed.images: 1,000,000 bytes an image; app.bsky.embed.video: 100,000,000.
MAX_IMAGE_BYTES = 1_000_000
MAX_VIDEO_BYTES = 100_000_000
# Graphemes: the Bluesky app's image alt limit, and the video lexicon's.
MAX_ALT_TEXT = 2000
MAX_VIDEO_ALT_TEXT = 1000
# Pages of 100 records one reconcile reads (listing is free; this bounds the time).
RECONCILE_MAX_PAGES = 5
# Pages one read may fetch, whatever ``max_posts`` asks for.
READ_MAX_PAGES = 3
READ_PAGE_MAX = 100
# app.bsky.feed.getPosts takes at most 25 URIs.
GET_POSTS_MAX = 25
MENTION_REASONS = ("mention", "reply", "quote")

_WS = re.compile(r"\s+")
_DID = r"did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-]"
_POST_URI = re.compile(rf"at://({_DID})/app\.bsky\.feed\.post/([A-Za-z0-9._:~-]{{1,512}})")

CAPABILITIES = Capabilities(
    provider="bsky",
    max_length=MAX_POST_GRAPHEMES,
    length_unit="graphemes",
    threads=True,
    reply=True,
    quote=True,
    delete=True,
    mentions=True,
    metrics=True,
    media=MediaCapabilities(
        mime_types=IMAGE_MIME_TYPES | VIDEO_MIME_TYPES,
        max_bytes=tuple(
            sorted(
                [(m, MAX_IMAGE_BYTES) for m in IMAGE_MIME_TYPES]
                + [(m, MAX_VIDEO_BYTES) for m in VIDEO_MIME_TYPES]
            )
        ),
        max_per_post=MAX_MEDIA_PER_POST,
        alt_text=True,
        alt_max_length=MAX_ALT_TEXT,
    ),
)


def parse_post_uri(value: object) -> tuple[str, str] | None:
    """``(did, rkey)`` of a post's AT URI, or None when ``value`` is not one."""
    match = _POST_URI.fullmatch(value.strip()) if isinstance(value, str) else None
    return (match.group(1), match.group(2)) if match else None


def check_post_uri(value: object, field: str) -> str:
    """A post's AT URI, or ``invalid_argument``: ids name records, so nothing else goes."""
    text = value.strip() if isinstance(value, str) else ""
    if parse_post_uri(text) is None:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"{field} must be a Bluesky post's AT URI (at://did:.../app.bsky.feed.post/<rkey>)",
            detail={field: text},
        )
    return text


def post_url(handle: str, rkey: str) -> str:
    return f"{APP_URL}/profile/{handle}/post/{rkey}"


def fingerprint(text: str) -> str:
    """The match key for a post's text: Bluesky stores it as sent."""
    plain = _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Ref:
    """A post as a reply or quote names it, and its thread's root if it is a reply."""

    uri: str
    cid: str
    root: dict[str, str] | None

    @property
    def strong(self) -> dict[str, str]:
        return {"uri": self.uri, "cid": self.cid}


@dataclass(frozen=True)
class _Blob:
    blob: dict[str, Any]
    mime: str
    alt: str


def _strong_ref(value: object) -> dict[str, str] | None:
    ref = as_object(value)
    if ref is None or not isinstance(ref.get("uri"), str) or not isinstance(ref.get("cid"), str):
        return None
    return {"uri": ref["uri"], "cid": ref["cid"]}


def _ref_uri(value: object) -> str | None:
    ref = _strong_ref(value)
    return ref["uri"] if ref else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _count(view: dict[str, Any], key: str) -> int | None:
    value = view.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metrics(view: dict[str, Any]) -> Metrics:
    return Metrics(
        likes=_count(view, "likeCount") or 0,
        replies=_count(view, "replyCount") or 0,
        reposts=_count(view, "repostCount") or 0,
        quotes=_count(view, "quoteCount") or 0,
        bookmarks=_count(view, "bookmarkCount"),
    )


def _created_at(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# What a read's ``parse`` makes of one listed item: (when it was listed, the
# post), "skip" for an item that is not one of the read's posts, None for one
# that could not be read.
type _Parsed[T] = tuple[datetime, T] | Literal["skip"] | None


class BlueskyChannel:
    """Bluesky, bound to one account: its client (credentials) and bound identity."""

    def __init__(
        self,
        client: BlueskyApi,
        *,
        did: str,
        handle: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.did = did
        self.handle = handle
        self._now = now
        self._blobs: dict[str, _Blob] = {}  # by CID, until create embeds them
        self._refs: dict[str, _Ref] = {}  # posts this channel created, by URI
        self._dids: dict[str, str | None] = {}  # resolved mention handles

    @property
    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    # -- offline --------------------------------------------------------------

    def check_post(self, post: PostSpec, prices: Prices) -> PostCheck:
        report = validate_text(post.text, prices)
        if len(post.media) > MAX_MEDIA_PER_POST:
            raise PulsarError(
                INVALID_MEDIA, f"Bluesky allows at most {MAX_MEDIA_PER_POST} images per post"
            )
        return PostCheck(
            text=report.text,
            length=report.graphemes,
            has_url=report.has_url,
            estimated_cost_usd=report.estimated_cost_usd,
        )

    def check_media(self, media: tuple[LoadedMedia, ...]) -> None:
        kinds = [m.mime for m in media]
        if len(kinds) > MAX_MEDIA_PER_POST:
            raise PulsarError(
                INVALID_MEDIA, f"Bluesky allows at most {MAX_MEDIA_PER_POST} images per post"
            )
        if any(k in VIDEO_MIME_TYPES for k in kinds) and len(kinds) > 1:
            raise PulsarError(
                INVALID_MEDIA,
                "Bluesky allows one video per post, not mixed with other media",
                detail={"mime_types": kinds},
            )
        for m in media:
            video = m.mime in VIDEO_MIME_TYPES
            limit = MAX_VIDEO_ALT_TEXT if video else MAX_ALT_TEXT
            length = grapheme_count(m.alt)
            if length > limit:
                raise PulsarError(
                    INVALID_MEDIA,
                    f"alt text is {length} graphemes; Bluesky allows {limit} for "
                    f"{'a video' if video else 'an image'}",
                )

    def check_target(self, *, reply_to: str | None, quote: str | None) -> None:
        if reply_to is not None:
            check_post_uri(reply_to, "reply_to")
        if quote is not None:
            check_post_uri(quote, "quote")

    def fingerprint(self, text: str) -> str:
        return fingerprint(text)

    # -- network: writes ----------------------------------------------------------

    async def whoami(self) -> Identity:
        me = await self.client.me()
        return Identity(provider_user_id=me["user_id"], handle=me["username"].lower())

    async def upload(self, media: LoadedMedia) -> str:
        blob = await self.client.upload_blob(media.data, media.mime)
        cid = str(obj(blob.get("ref"))["$link"])
        self._blobs[cid] = _Blob(blob=blob, mime=media.mime, alt=media.alt)
        return cid

    async def create(
        self,
        text: str,
        *,
        reply_to: str | None = None,
        quote: str | None = None,
        media_ids: tuple[str, ...] = (),
    ) -> Published:
        """Everything the record needs is looked up first; only ``createRecord`` writes."""
        record: dict[str, Any] = {
            "$type": POST_COLLECTION,
            "text": text,
            "createdAt": _created_at(self._now()),
        }
        facets = await self._facets(detect_facets(text))
        if facets:
            record["facets"] = facets
        root: dict[str, str] | None = None
        if reply_to is not None:
            parent = await self._ref(reply_to, "reply_to")
            root = parent.root or parent.strong
            record["reply"] = {"root": root, "parent": parent.strong}
        quoted = await self._ref(quote, "quote") if quote is not None else None
        embed = self._embed(media_ids, quoted)
        if embed is not None:
            record["embed"] = embed
        created = await self.client.procedure(
            "com.atproto.repo.createRecord",
            {"repo": self.did, "collection": POST_COLLECTION, "record": record},
            non_idempotent=True,
        )
        uri, cid = created.get("uri"), created.get("cid")
        parsed = parse_post_uri(uri)
        if parsed is None or not isinstance(uri, str) or not isinstance(cid, str):
            # Bluesky said yes, but we cannot tell which record it made.
            raise OutcomeUnknown("Bluesky created a record without a readable uri and cid")
        self._refs[uri] = _Ref(uri=uri, cid=cid, root=root)
        for media_id in media_ids:
            self._blobs.pop(media_id, None)
        return Published(post_id=uri, url=post_url(self.handle, parsed[1]), text=text)

    async def delete(self, post_id: str) -> bool:
        uri = check_post_uri(post_id, "post_id")
        did, rkey = parse_post_uri(uri) or ("", "")
        if did != self.did:
            raise PulsarError(
                INVALID_ARGUMENT,
                "that post is not in this account's repository",
                detail={"post_id": uri},
            )
        await self.client.procedure(
            "com.atproto.repo.deleteRecord",
            {"repo": self.did, "collection": POST_COLLECTION, "rkey": rkey},
        )
        return True

    async def _facets(self, found: tuple[Facet, ...]) -> list[dict[str, Any]]:
        facets: list[dict[str, Any]] = []
        for facet in found:
            if facet.kind == "link":
                feature = {"$type": "app.bsky.richtext.facet#link", "uri": facet.value}
            elif facet.kind == "tag":
                feature = {"$type": "app.bsky.richtext.facet#tag", "tag": facet.value}
            else:
                did = await self._resolve(facet.value)
                if did is None:
                    continue
                feature = {"$type": "app.bsky.richtext.facet#mention", "did": did}
            facets.append(
                {
                    "index": {"byteStart": facet.byte_start, "byteEnd": facet.byte_end},
                    "features": [feature],
                }
            )
        return facets

    async def _resolve(self, handle: str) -> str | None:
        """The DID ``handle`` names, or None when Bluesky cannot resolve it."""
        if handle not in self._dids:
            try:
                body = await self.client.query(
                    "com.atproto.identity.resolveHandle", {"handle": handle}
                )
            except PulsarError as exc:
                # An unknown handle is a 400; anything else (the network) is not an answer.
                if exc.code != NOT_FOUND and obj(exc.detail).get("status") != 400:
                    raise
                body = {}
            did = body.get("did")
            self._dids[handle] = did if isinstance(did, str) and did.startswith("did:") else None
        return self._dids[handle]

    async def _ref(self, uri: str, field: str) -> _Ref:
        """``uri``'s CID and thread root: remembered, else read from the network."""
        if uri in self._refs:
            return self._refs[uri]
        did, rkey = parse_post_uri(check_post_uri(uri, field)) or ("", "")
        if did == self.did:
            body = await self.client.query(
                "com.atproto.repo.getRecord",
                {"repo": did, "collection": POST_COLLECTION, "rkey": rkey},
            )
            cid, value = body.get("cid"), obj(body.get("value"))
        else:
            body = await self.client.query("app.bsky.feed.getPosts", {"uris": [uri]})
            views = [v for raw in as_list(body.get("posts")) if (v := as_object(raw)) is not None]
            view = next((v for v in views if v.get("uri") == uri), None)
            if view is None:
                raise PulsarError(NOT_FOUND, f"Bluesky has no post {uri}", detail={field: uri})
            cid, value = view.get("cid"), obj(view.get("record"))
        if not isinstance(cid, str):
            raise PulsarError(API_ERROR, f"Bluesky returned no CID for {uri}", detail={field: uri})
        ref = _Ref(uri=uri, cid=cid, root=_strong_ref(obj(value.get("reply")).get("root")))
        self._refs[uri] = ref
        return ref

    def _embed(self, media_ids: tuple[str, ...], quoted: _Ref | None) -> dict[str, Any] | None:
        blobs: list[_Blob] = []
        for media_id in media_ids:
            blob = self._blobs.get(media_id)
            if blob is None:
                raise PulsarError(
                    INVALID_MEDIA,
                    "a Bluesky media id is good only on the channel that uploaded it; "
                    "nothing was posted",
                    detail={"media_id": media_id},
                )
            blobs.append(blob)
        media: dict[str, Any] | None = None
        if len(blobs) == 1 and blobs[0].mime in VIDEO_MIME_TYPES:
            media = {"$type": "app.bsky.embed.video", "video": blobs[0].blob, "alt": blobs[0].alt}
        elif blobs:
            media = {
                "$type": "app.bsky.embed.images",
                "images": [{"image": b.blob, "alt": b.alt} for b in blobs],
            }
        if quoted is None:
            return media
        record = {"$type": "app.bsky.embed.record", "record": quoted.strong}
        if media is None:
            return record
        return {"$type": "app.bsky.embed.recordWithMedia", "record": record, "media": media}

    # -- network: reads -----------------------------------------------------------

    async def recent_posts(self, since: datetime) -> RecentPosts:
        """The account's post records since ``since``, newest first, up to the page cap.

        A record without a usable URI or ``createdAt`` is left out and the
        listing marked incomplete: a record reconcile could not read is not
        evidence of absence.
        """
        params: dict[str, Any] = {"repo": self.did, "collection": POST_COLLECTION, "limit": 100}
        found: list[RemotePost] = []
        unreadable = False
        for _ in range(RECONCILE_MAX_PAGES):
            body = await self.client.query("com.atproto.repo.listRecords", params)
            older = False
            for raw in as_list(body.get("records")):
                item = obj(raw)
                value = obj(item.get("value"))
                parsed = parse_post_uri(item.get("uri"))
                created = _parse_time(value.get("createdAt"))
                if parsed is None or created is None:
                    unreadable = True
                    continue
                if created < since:
                    older = True
                    continue
                found.append(
                    RemotePost(
                        post_id=str(item["uri"]),
                        url=post_url(self.handle, parsed[1]),
                        created_at=created,
                        fingerprint=fingerprint(str(value.get("text", ""))),
                    )
                )
            cursor = body.get("cursor")
            if older or not isinstance(cursor, str) or not cursor:
                found.sort(key=lambda p: p.created_at, reverse=True)
                return RecentPosts(posts=tuple(found), complete=not unreadable)
            params["cursor"] = cursor
        found.sort(key=lambda p: p.created_at, reverse=True)
        return RecentPosts(posts=tuple(found), complete=False)

    async def mentions(self, since: datetime, *, max_posts: int) -> Page[Mention]:
        def parse(item: dict[str, Any]) -> _Parsed[Mention]:
            if item.get("reason") not in MENTION_REASONS:
                return "skip"
            record = obj(item.get("record"))
            parsed = parse_post_uri(item.get("uri"))
            created = _parse_time(record.get("createdAt"))
            listed = _parse_time(item.get("indexedAt")) or created
            if parsed is None or created is None or listed is None:
                return None
            author = obj(item.get("author"))
            handle = str(author.get("handle", "")).lower()
            reply = obj(record.get("reply"))
            uri = str(item["uri"])
            return listed, Mention(
                post_id=uri,
                url=post_url(handle or parsed[0], parsed[1]),
                author=handle,
                text=str(record.get("text", "")),
                created_at=created,
                conversation_id=_ref_uri(reply.get("root")) or uri,
                reply_to=_ref_uri(reply.get("parent")),
                metrics=Metrics(),
            )

        params: dict[str, Any] = {"reasons": list(MENTION_REASONS)}
        page = await self._read("app.bsky.notification.listNotifications", "notifications",
                                params, since, max_posts, parse)  # fmt: skip
        counts = await self._counts([m.post_id for m in page.posts])
        posts = tuple(replace(m, metrics=counts.get(m.post_id, m.metrics)) for m in page.posts)
        return Page(posts=posts, complete=page.complete, fetched=page.fetched)

    async def own_posts(self, since: datetime, *, max_posts: int) -> Page[OwnPost]:
        def parse(item: dict[str, Any]) -> _Parsed[OwnPost]:
            view = obj(item.get("post"))
            if item.get("reason") is not None or obj(view.get("author")).get("did") != self.did:
                return "skip"  # a repost, or not the account's own post
            record = obj(view.get("record"))
            parsed = parse_post_uri(view.get("uri"))
            created = _parse_time(record.get("createdAt"))
            listed = _parse_time(view.get("indexedAt")) or created
            if parsed is None or created is None or listed is None:
                return None
            return listed, OwnPost(
                post_id=str(view["uri"]),
                url=post_url(self.handle, parsed[1]),
                text=str(record.get("text", "")),
                created_at=created,
                reply_to=_ref_uri(obj(record.get("reply")).get("parent")),
                metrics=_metrics(view),
            )

        params: dict[str, Any] = {"actor": self.did, "filter": "posts_with_replies"}
        return await self._read("app.bsky.feed.getAuthorFeed", "feed", params, since, max_posts,
                                parse)  # fmt: skip

    async def _read[T](
        self,
        nsid: str,
        key: str,
        params: dict[str, Any],
        since: datetime,
        max_posts: int,
        parse: Callable[[dict[str, Any]], _Parsed[T]],
    ) -> Page[T]:
        """Page through a newest-first listing back to ``since``."""
        if max_posts < 1:
            raise ValueError(f"max_posts must be >= 1, got {max_posts}")
        params = dict(params)
        found: list[T] = []
        fetched = 0
        complete = True
        for page in range(READ_MAX_PAGES):
            params["limit"] = min(max_posts - len(found), READ_PAGE_MAX)
            body = await self.client.query(nsid, params)
            older = False
            for raw in as_list(body.get(key)):
                fetched += 1
                item = as_object(raw)
                parsed = parse(item) if item is not None else None
                if parsed == "skip":
                    continue
                if parsed is None:
                    complete = False  # a post that cannot be read is not silently dropped
                    continue
                listed, post = parsed
                if listed < since:
                    older = True
                elif len(found) < max_posts:
                    found.append(post)
                else:
                    complete = False
            cursor = body.get("cursor")
            if older or not isinstance(cursor, str) or not cursor:
                break
            if len(found) >= max_posts or page == READ_MAX_PAGES - 1:
                complete = False
                break
            params["cursor"] = cursor
        return Page(posts=tuple(found), complete=complete, fetched=fetched)

    async def _counts(self, uris: list[str]) -> dict[str, Metrics]:
        """Each post's counts, from the AppView; a post it no longer shows is left out."""
        counts: dict[str, Metrics] = {}
        for start in range(0, len(uris), GET_POSTS_MAX):
            chunk = uris[start : start + GET_POSTS_MAX]
            body = await self.client.query("app.bsky.feed.getPosts", {"uris": chunk})
            for raw in as_list(body.get("posts")):
                view = as_object(raw)
                if view is not None and isinstance(view.get("uri"), str):
                    counts[view["uri"]] = _metrics(view)
        return counts

    async def aclose(self) -> None:
        await self.client.aclose()
