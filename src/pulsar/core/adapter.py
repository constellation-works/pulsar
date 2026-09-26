"""The channel adapter contract: what core needs from a provider.

A ``Channel`` is one provider bound to one account's credentials. Core never
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

``AuthFlow`` is separate because logins are human-only and differ per
provider (X: OAuth 2.0 PKCE on a loopback; Mastodon: per-instance app
registration; Bluesky: DPoP/PAR; LinkedIn: a client secret).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .plan import PostSpec
from .settings import Prices


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
    metrics: bool
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


class Channel(Protocol):
    """One provider, bound to one account's stored credentials."""

    @property
    def capabilities(self) -> Capabilities: ...

    def check_post(self, post: PostSpec, prices: Prices) -> PostCheck:
        """Provider text rules, offline: raises ``invalid_text`` or ``unsupported``."""
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
