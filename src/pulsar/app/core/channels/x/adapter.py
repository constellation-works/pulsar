"""X as a ``Channel``: capabilities, offline checks, and one network step per call.

Reconcile matching: X rewrites posted text. Every link becomes a ``t.co``
link, ``&``/``<``/``>`` come back HTML-escaped, a post with media gets a
trailing ``t.co`` link to the media, and a reply gets the replied-to
accounts' ``@handles`` prepended. ``fingerprint`` therefore hashes the text
with every URL and any leading run of ``@handles`` removed, NFC and
whitespace collapsed; ``remote_fingerprint`` unescapes X's entities first
(only X's copy: a local ``&amp;`` is literal text). Two posts that differ
only in their links or leading mentions fingerprint the same; reconcile only
compares posts by one account inside the claim's time window, where that
collision is not a realistic risk (and would err towards "published", never
towards a second post).

Reads: ``mentions`` and ``own_posts`` page through X's user timelines
(``/users/:id/mentions``, ``/users/:id/tweets``) up to ``max_posts`` and
``READ_MAX_PAGES``. X bills every post a page returns, so a page asks for no
more than is still wanted (X's minimum page is 5). Own posts carry
``non_public_metrics`` (impressions, link and profile clicks), which X gives
only to the author and only for the last 30 days.

Alt text goes to ``POST /2/media/metadata`` after the upload and before
the post. X accepts alt text for images and GIFs; for video it is kept in
the plan and ledger but not sent.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pulsar.internal.errors import API_ERROR, INVALID_MEDIA, PulsarError
from pulsar.internal.fs import as_list, as_object, obj

from ..contract import (
    IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
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
from .client import check_x_id
from .config import MAX_POST_WEIGHTED_LENGTH
from .interfaces import XApi
from .text import URL_RE, validate_text

MAX_MEDIA_PER_POST = 4
MAX_ALT_TEXT = 1000
# Pages of 100 posts read by one reconcile; X bills post reads, so this is a cost cap too.
RECONCILE_MAX_PAGES = 3
# Pages one read may fetch, whatever ``max_posts`` asks for.
READ_MAX_PAGES = 3
READ_PAGE_MIN, READ_PAGE_MAX = 5, 100

_TCO = re.compile(r"https?://t\.co/\S+")
_WS = re.compile(r"\s+")
# The @handles X puts in front of a reply (after whitespace is collapsed).
_LEADING_MENTIONS = re.compile(r"^(?:@[A-Za-z0-9_]{1,15} )+")

CAPABILITIES = Capabilities(
    provider="x",
    max_length=MAX_POST_WEIGHTED_LENGTH,
    length_unit="weighted characters",
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


def post_url(handle: str, post_id: str) -> str:
    return f"https://x.com/{handle}/status/{post_id}"


def fingerprint(text: str) -> str:
    """The match key for text as pulsar sends it."""
    plain = unicodedata.normalize("NFC", text)
    plain = _TCO.sub(" ", plain)
    plain = URL_RE.sub(" ", plain)
    plain = _WS.sub(" ", plain).strip()
    plain = _LEADING_MENTIONS.sub("", plain)
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def remote_fingerprint(text: str) -> str:
    """The match key for a post's ``text`` as X returns it (entity-escaped)."""
    return fingerprint(html.unescape(text))


class XChannel:
    """X, bound to one account: its client (credentials) and bound identity."""

    def __init__(self, client: XApi, *, user_id: str, handle: str) -> None:
        self.client = client
        self.user_id = user_id
        self.handle = handle

    @property
    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    # -- offline --------------------------------------------------------------

    def check_post(self, post: PostSpec, prices: Prices) -> PostCheck:
        report = validate_text(post.text, prices)
        if len(post.media) > MAX_MEDIA_PER_POST:
            raise PulsarError(
                INVALID_MEDIA, f"X allows at most {MAX_MEDIA_PER_POST} media items per post"
            )
        return PostCheck(
            text=report.text,
            length=report.weighted_length,
            has_url=report.has_url,
            estimated_cost_usd=report.estimated_cost_usd,
        )

    def check_media(self, media: tuple[LoadedMedia, ...]) -> None:
        kinds = [m.mime for m in media]
        if len(kinds) > MAX_MEDIA_PER_POST:
            raise PulsarError(
                INVALID_MEDIA, f"X allows at most {MAX_MEDIA_PER_POST} media items per post"
            )
        solo = [k for k in kinds if k in VIDEO_MIME_TYPES or k == "image/gif"]
        if solo and len(kinds) > 1:
            raise PulsarError(
                INVALID_MEDIA,
                "X allows one video or one GIF per post, not mixed with other media",
                detail={"mime_types": kinds},
            )
        for m in media:
            if len(m.alt) > MAX_ALT_TEXT:
                raise PulsarError(
                    INVALID_MEDIA,
                    f"alt text is {len(m.alt)} characters; X allows {MAX_ALT_TEXT}",
                )

    def check_target(self, *, reply_to: str | None, quote: str | None) -> None:
        if reply_to is not None:
            check_x_id(reply_to, "reply_to")
        if quote is not None:
            check_x_id(quote, "quote")

    def fingerprint(self, text: str) -> str:
        return fingerprint(text)

    # -- network --------------------------------------------------------------

    async def whoami(self) -> Identity:
        me = await self.client.me()
        return Identity(provider_user_id=me["user_id"], handle=me["username"].lower())

    async def upload(self, media: LoadedMedia) -> str:
        media_id, _state = await self.client.upload_media(media.data, media.mime)
        if media.alt and media.mime in IMAGE_MIME_TYPES:
            await self.client.request(
                "POST",
                "/media/metadata",
                json={"id": media_id, "metadata": {"alt_text": {"text": media.alt}}},
            )
        return media_id

    async def create(
        self,
        text: str,
        *,
        reply_to: str | None = None,
        quote: str | None = None,
        media_ids: tuple[str, ...] = (),
    ) -> Published:
        created = await self.client.create_post(
            text,
            reply_to_post_id=reply_to,
            quote_post_id=quote,
            media_ids=list(media_ids) or None,
        )
        post_id = created["post_id"]
        return Published(post_id=post_id, url=post_url(self.handle, post_id), text=created["text"])

    async def delete(self, post_id: str) -> bool:
        return await self.client.delete_post(post_id)

    async def recent_posts(self, since: datetime) -> RecentPosts:
        """The account's posts since ``since``, newest first, up to the page cap.

        A post that comes back without a usable id or ``created_at`` is left
        out and the listing is marked incomplete: reconcile may call an item
        absent only on a complete listing, and a post it could not read is
        not evidence of absence (no invented timestamp).
        """
        params: dict[str, Any] = {
            "max_results": 100,
            "start_time": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tweet.fields": "created_at",
        }
        found: list[RemotePost] = []
        unreadable = False
        for _ in range(RECONCILE_MAX_PAGES):
            resp = await self.client.request(
                "GET", f"/users/{check_x_id(self.user_id, 'user_id')}/tweets", params=params
            )
            try:
                body = obj(resp.json())
            except ValueError as exc:
                raise PulsarError(API_ERROR, "X timeline response is not JSON") from exc
            for raw in as_list(body.get("data")):
                post = as_object(raw)
                created = _parse_time(post.get("created_at")) if post is not None else None
                if post is None or not isinstance(post.get("id"), str) or created is None:
                    unreadable = True
                    continue
                found.append(
                    RemotePost(
                        post_id=post["id"],
                        url=post_url(self.handle, post["id"]),
                        created_at=created,
                        fingerprint=remote_fingerprint(str(post.get("text", ""))),
                    )
                )
            token = obj(body.get("meta")).get("next_token")
            if not isinstance(token, str) or not token:
                return RecentPosts(posts=tuple(found), complete=not unreadable)
            params["pagination_token"] = token
        return RecentPosts(posts=tuple(found), complete=False)

    async def mentions(self, since: datetime, *, max_posts: int) -> Page[Mention]:
        params: dict[str, Any] = {
            "tweet.fields": "created_at,author_id,conversation_id,referenced_tweets,public_metrics",
            "expansions": "author_id",
            "user.fields": "username",
        }

        def parse(post: dict[str, Any], users: dict[str, str]) -> Mention | None:
            created = _parse_time(post.get("created_at"))
            if not isinstance(post.get("id"), str) or created is None:
                return None
            author = users.get(str(post.get("author_id")), "")
            return Mention(
                post_id=post["id"],
                url=post_url(author or "i", post["id"]),
                author=author,
                text=html.unescape(str(post.get("text", ""))),
                created_at=created,
                conversation_id=_str(post.get("conversation_id")),
                reply_to=_replied_to(post),
                metrics=_metrics(post),
            )

        return await self._read("mentions", since, max_posts, params, parse)

    async def own_posts(self, since: datetime, *, max_posts: int) -> Page[OwnPost]:
        params: dict[str, Any] = {
            "exclude": "retweets",
            "tweet.fields": "created_at,referenced_tweets,public_metrics,non_public_metrics",
        }

        def parse(post: dict[str, Any], _users: dict[str, str]) -> OwnPost | None:
            created = _parse_time(post.get("created_at"))
            if not isinstance(post.get("id"), str) or created is None:
                return None
            return OwnPost(
                post_id=post["id"],
                url=post_url(self.handle, post["id"]),
                text=html.unescape(str(post.get("text", ""))),
                created_at=created,
                reply_to=_replied_to(post),
                metrics=_metrics(post),
            )

        return await self._read("tweets", since, max_posts, params, parse)

    async def _read[T](
        self,
        timeline: str,
        since: datetime,
        max_posts: int,
        params: dict[str, Any],
        parse: Callable[[dict[str, Any], dict[str, str]], T | None],
    ) -> Page[T]:
        """Page through ``/users/:id/<timeline>`` since ``since``, newest first."""
        if max_posts < 1:
            raise ValueError(f"max_posts must be >= 1, got {max_posts}")
        path = f"/users/{check_x_id(self.user_id, 'user_id')}/{timeline}"
        params = {**params, "start_time": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        found: list[T] = []
        fetched = 0
        complete = True
        for page in range(READ_MAX_PAGES):
            wanted = max_posts - len(found)
            params["max_results"] = min(max(wanted, READ_PAGE_MIN), READ_PAGE_MAX)
            resp = await self.client.request("GET", path, params=params)
            try:
                body = obj(resp.json())
            except ValueError as exc:
                raise PulsarError(API_ERROR, f"X {timeline} response is not JSON") from exc
            users = {
                str(u["id"]): str(u["username"]).lower()
                for raw in as_list(obj(body.get("includes")).get("users"))
                if (u := as_object(raw)) is not None and "id" in u and "username" in u
            }
            for raw in as_list(body.get("data")):
                fetched += 1
                post = as_object(raw)
                parsed = parse(post, users) if post is not None else None
                if parsed is None:
                    complete = False  # a post that cannot be read is not silently dropped
                elif len(found) < max_posts:
                    found.append(parsed)
                else:
                    complete = False
            token = obj(body.get("meta")).get("next_token")
            if not isinstance(token, str) or not token:
                break
            if len(found) >= max_posts or page == READ_MAX_PAGES - 1:
                complete = False
                break
            params["pagination_token"] = token
        return Page(posts=tuple(found), complete=complete, fetched=fetched)

    async def aclose(self) -> None:
        await self.client.aclose()


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _replied_to(post: dict[str, Any]) -> str | None:
    for raw in as_list(post.get("referenced_tweets")):
        ref = as_object(raw)
        if ref is not None and ref.get("type") == "replied_to":
            return _str(ref.get("id"))
    return None


def _count(section: dict[str, Any], key: str) -> int | None:
    value = section.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metrics(post: dict[str, Any]) -> Metrics:
    public = obj(post.get("public_metrics"))
    private = obj(post.get("non_public_metrics"))
    impressions = _count(private, "impression_count")
    return Metrics(
        likes=_count(public, "like_count") or 0,
        replies=_count(public, "reply_count") or 0,
        reposts=_count(public, "retweet_count") or 0,
        quotes=_count(public, "quote_count") or 0,
        bookmarks=_count(public, "bookmark_count"),
        impressions=impressions if impressions is not None else _count(public, "impression_count"),
        url_clicks=_count(private, "url_link_clicks"),
        profile_clicks=_count(private, "user_profile_clicks"),
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
