"""The channel adapter contract: what the publisher and accounts need from a channel.

A ``Channel`` is one provider bound to one account's credentials. The publisher never
speaks HTTP; it validates against ``capabilities``, calls ``check_post`` for
provider text rules, and drives ``upload`` / ``create`` / ``delete`` /
``recent_posts`` one network step at a time so the ledger can record each
step before it happens.

Failure semantics every adapter must keep (the ledger depends on them):

- a ``PulsarError`` that is not ``outcome_unknown`` means the provider did
  not act (nothing was published);
- ``OutcomeUnknown`` means it may have acted (a timeout or 5xx after the
  request left, or a success response without a readable id);
- ``create`` is the only non-idempotent step. ``upload`` creates nothing
  visible, and ``delete`` is idempotent at the provider.

Reads (``mentions``, ``own_posts``) are paid too: they return at most
``max_posts`` posts and say whether that was everything since ``since``.
What they return goes to the caller; nothing of it is stored.

``AuthFlow`` is separate because logins are human-only and differ per
provider (X: OAuth 2.0 PKCE on a loopback; Mastodon: per-instance app
registration; Bluesky: DPoP/PAR; LinkedIn: a client secret).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# -- media: the types and size ceilings pulsar loads at all; a channel's
# ``MediaCapabilities`` may narrow them.

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VIDEO_MIME_TYPES = frozenset({"video/mp4"})
SUPPORTED_MIME_TYPES = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES

# -- what a post costs, per channel; ``config.toml`` sets these per provider.

DEFAULT_PLAIN_POST_USD = 0.015
DEFAULT_URL_POST_USD = 0.20
# X bills reads per post returned; an estimate until checked on the portal.
DEFAULT_READ_POST_USD = 0.005


@dataclass(frozen=True)
class Prices:
    plain_post_usd: float = DEFAULT_PLAIN_POST_USD
    url_post_usd: float = DEFAULT_URL_POST_USD
    read_post_usd: float = DEFAULT_READ_POST_USD

    def for_post(self, *, has_url: bool) -> float:
        return self.url_post_usd if has_url else self.plain_post_usd

    def for_read(self, posts: int) -> float:
        return round(self.read_post_usd * posts, 6)


FREE = Prices(plain_post_usd=0.0, url_post_usd=0.0, read_post_usd=0.0)

# -- a post, as a plan names it and a channel checks it.


@dataclass(frozen=True)
class MediaRef:
    path: str
    alt: str


@dataclass(frozen=True)
class PostSpec:
    text: str
    media: tuple[MediaRef, ...] = ()


@dataclass(frozen=True)
class MediaCapabilities:
    mime_types: frozenset[str]
    max_bytes: tuple[tuple[str, int], ...]  # (mime, limit)
    max_per_post: int
    alt_text: bool
    alt_max_length: int

    def limit_for(self, mime: str) -> int:
        return dict(self.max_bytes)[mime]


@dataclass(frozen=True)
class Capabilities:
    provider: str
    max_length: int  # in the provider's own length unit (see Channel.measure)
    length_unit: str  # e.g. "weighted characters", "graphemes"
    threads: bool
    reply: bool
    quote: bool
    delete: bool
    mentions: bool  # Channel.mentions works
    metrics: bool  # Channel.own_posts carries metrics
    media: MediaCapabilities


@dataclass(frozen=True)
class Identity:
    provider_user_id: str
    handle: str  # without "@", lower-case


@dataclass(frozen=True)
class PostCheck:
    """One post, validated offline."""

    text: str
    length: int
    has_url: bool
    estimated_cost_usd: float


@dataclass(frozen=True)
class LoadedMedia:
    data: bytes
    mime: str  # sniffed, never the caller's claim
    alt: str
    sha256: str


@dataclass(frozen=True)
class Published:
    post_id: str
    url: str
    text: str  # as the provider echoed it


@dataclass(frozen=True)
class RemotePost:
    post_id: str
    url: str
    created_at: datetime
    fingerprint: str  # Channel.fingerprint of the text as the provider shows it


@dataclass(frozen=True)
class RecentPosts:
    posts: tuple[RemotePost, ...]
    # True when every post of the account since ``since`` is included, so a
    # missing match proves the post was not made. False when the provider
    # truncated the listing; reconcile then leaves the row unknown.
    complete: bool


@dataclass(frozen=True)
class Metrics:
    """A post's counts as the provider reports them; None where it reports none
    (non-public counts exist only for the account's own recent posts)."""

    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int | None = None
    impressions: int | None = None
    url_clicks: int | None = None
    profile_clicks: int | None = None


@dataclass(frozen=True)
class Mention:
    """A post by someone else that mentions the account."""

    post_id: str
    url: str
    author: str  # handle without "@", lower-case; "" when the provider did not say
    text: str
    created_at: datetime
    conversation_id: str | None
    reply_to: str | None  # the post it replies to, if it is a reply
    metrics: Metrics


@dataclass(frozen=True)
class OwnPost:
    """One of the account's own posts, with its metrics."""

    post_id: str
    url: str
    text: str
    created_at: datetime
    reply_to: str | None
    metrics: Metrics


@dataclass(frozen=True)
class Page[T]:
    posts: tuple[T, ...]
    # False when ``max_posts`` or the provider cut the listing short.
    complete: bool
    # Posts the provider returned, and billed: at least ``len(posts)``, more
    # when a page's minimum size overshot ``max_posts`` or a post was unreadable.
    fetched: int


class Channel(Protocol):
    """One provider, bound to one account's stored credentials."""

    @property
    def capabilities(self) -> Capabilities: ...

    def check_post(self, post: PostSpec, prices: Prices) -> PostCheck:
        """Provider text rules, offline: raises ``invalid_text`` or ``unsupported``."""
        ...

    def check_media(self, media: tuple[LoadedMedia, ...]) -> None:
        """Provider rules for one post's media set (count, mixing, alt length), offline."""
        ...

    def check_target(self, *, reply_to: str | None, quote: str | None) -> None:
        """Validate reply/quote ids offline: ``invalid_argument`` or ``unsupported``."""
        ...

    def fingerprint(self, text: str) -> str:
        """A hash that survives the provider's rewriting of posted text
        (link shortening, entity escaping), for reconcile to match on."""
        ...

    async def whoami(self) -> Identity: ...

    async def upload(self, media: LoadedMedia) -> str:
        """Upload one media item; returns the provider's media id."""
        ...

    async def create(
        self,
        text: str,
        *,
        reply_to: str | None = None,
        quote: str | None = None,
        media_ids: tuple[str, ...] = (),
    ) -> Published: ...

    async def delete(self, post_id: str) -> bool: ...

    async def recent_posts(self, since: datetime) -> RecentPosts: ...

    async def mentions(self, since: datetime, *, max_posts: int) -> Page[Mention]:
        """Posts mentioning the account since ``since``, newest first."""
        ...

    async def own_posts(self, since: datetime, *, max_posts: int) -> Page[OwnPost]:
        """The account's posts since ``since`` with their metrics, newest first."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class AuthStart:
    url: str  # where the human approves
    state: str  # opaque; round-trips through the callback


class AuthFlow(Protocol):
    """Human-only account binding. Never exposed as a tool."""

    provider: str

    def begin(self) -> AuthStart: ...

    def complete(self, start: AuthStart, callback: dict[str, list[str]]) -> Identity:
        """Exchange the callback for credentials, store them, return who was bound."""
        ...
