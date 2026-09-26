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

Alt text goes to ``POST /2/media/metadata`` after the upload and before
the post. X accepts alt text for images and GIFs; for video it is kept in
the plan and ledger but not sent.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pulsar.errors import API_ERROR, INVALID_MEDIA, PulsarError
from pulsar.home import Prices
from pulsar.jsonx import as_list, as_object, obj
from pulsar.plan import PostSpec
from pulsar.publishing import IMAGE_MIME_TYPES, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, VIDEO_MIME_TYPES

from .. import (
    Capabilities,
    Identity,
    LoadedMedia,
    MediaCapabilities,
    PostCheck,
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
    metrics=False,
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

    async def aclose(self) -> None:
        await self.client.aclose()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
