"""The plan: a provider-neutral description of what to publish, and its digest.

A plan is what a human approves and what the ledger deduplicates, so it has
one canonical form and one digest. Inputs arrive as a mapping (JSON from a
tool call, or YAML from a content record)::

    account: x:constworks            # or accounts: [x:constworks, bsky:...]
    posts:                           # a thread; a single post is a thread of one
      - text: "First post"
        media: [{path: content/2026-09/launch/hero.png, alt: "The launch banner"}]
      - text: "Second post"
    reply_to: "1790000000000000000"  # or quote: ...; never both
    variants:                        # per-provider replacement for `posts`
      bsky: {posts: [{text: "Shorter copy for Bluesky"}]}
    not_before: 2026-10-01T16:00:00Z

``text`` / ``media`` at the top level are shorthand for a single post.

**Normalisation.** Account aliases are ``provider:handle``, lower-cased, a
leading ``@`` dropped (``X:@ConstWorks`` is ``x:constworks``). Text and alt
text are Unicode NFC with ``\\r\\n`` turned into ``\\n`` and outer whitespace
stripped; that normalised text is what gets posted, so the digest is of the
exact bytes that go out.

**Digest.** SHA-256 over canonical JSON (sorted keys, no insignificant
whitespace) of: the sorted account aliases, every post's text, every media
item's *content* hash and alt text, reply/quote, and the variants. Key
order, YAML formatting and alias spelling do not change it; any change to
what would be published does. Two things are deliberately left out:

- the media *path* (the bytes are what is published, so a rename is not a
  change), and
- ``not_before``: rescheduling is not a content change, and keeping it out
  of the digest means a moved post keeps its approval and its idempotency
  key, so it cannot go out twice.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml

from .errors import INVALID_PLAN, PulsarError
from .jsonx import as_list, as_object

DIGEST_VERSION = 1
MAX_THREAD_POSTS = 25

_PLAN_KEYS = frozenset(
    {"account", "accounts", "posts", "text", "media", "reply_to", "quote", "variants", "not_before"}
)
_POST_KEYS = frozenset({"text", "media"})
_MEDIA_KEYS = frozenset({"path", "alt"})
_VARIANT_KEYS = frozenset({"posts", "text", "media"})


def _invalid(message: str, where: str) -> PulsarError:
    return PulsarError(INVALID_PLAN, f"{where}: {message}", detail={"at": where})


def normalize_text(value: str) -> str:
    """What is posted and digested: NFC, LF line endings, outer whitespace stripped."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def normalize_alias(value: str) -> str:
    """``X:@ConstWorks`` -> ``x:constworks``. Raises ``invalid_plan`` without a provider."""
    provider, sep, handle = value.strip().partition(":")
    handle = handle.strip().removeprefix("@")
    if not sep or not provider.strip() or not handle:
        raise _invalid(f"account {value!r} must be provider:handle, e.g. x:<handle>", "account")
    return f"{provider.strip().lower()}:{handle.lower()}"


def alias_provider(alias: str) -> str:
    return alias.partition(":")[0]


@dataclass(frozen=True)
class MediaRef:
    path: str
    alt: str


@dataclass(frozen=True)
class PostSpec:
    text: str
    media: tuple[MediaRef, ...] = ()


@dataclass(frozen=True)
class Plan:
    posts: tuple[PostSpec, ...]
    accounts: tuple[str, ...] = ()  # canonical aliases; empty means the default account
    reply_to: str | None = None
    quote: str | None = None
    variants: tuple[tuple[str, tuple[PostSpec, ...]], ...] = ()  # sorted by provider
    not_before: datetime | None = None

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: object) -> Plan:
        plan = as_object(data)
        if plan is None:
            raise _invalid("a plan must be a mapping", "plan")
        _no_unknown(plan, _PLAN_KEYS, "plan")
        if "account" in plan and "accounts" in plan:
            raise _invalid("give `account` or `accounts`, not both", "plan")
        if plan.get("reply_to") is not None and plan.get("quote") is not None:
            raise _invalid("a plan cannot both reply and quote", "plan")
        raw_accounts: list[Any] = (
            [plan["account"]] if "account" in plan else as_list(plan.get("accounts", []))
        )
        if "accounts" in plan and not isinstance(plan["accounts"], list):
            raise _invalid("must be a list of provider:handle aliases", "accounts")
        accounts: list[str] = []
        for i, raw in enumerate(raw_accounts):
            if not isinstance(raw, str):
                raise _invalid("must be a provider:handle string", f"accounts[{i}]")
            accounts.append(normalize_alias(raw))
        variants: dict[str, tuple[PostSpec, ...]] = {}
        raw_variants = plan.get("variants", {})
        variant_map = as_object(raw_variants)
        if variant_map is None:
            raise _invalid("must be a mapping of provider to posts", "variants")
        for provider, body in variant_map.items():
            where = f"variants.{provider}"
            body_map = as_object(body)
            if body_map is None:
                raise _invalid("must be a mapping with `posts` (or `text`)", where)
            _no_unknown(body_map, _VARIANT_KEYS, where)
            variants[provider.strip().lower()] = _posts(body_map, where)
        return cls(
            posts=_posts(plan, "plan"),
            accounts=tuple(sorted(set(accounts))),
            reply_to=_id(plan.get("reply_to"), "reply_to"),
            quote=_id(plan.get("quote"), "quote"),
            variants=tuple(sorted(variants.items())),
            not_before=_when(plan.get("not_before")),
        )

    @classmethod
    def from_yaml(cls, text: str) -> Plan:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PulsarError(INVALID_PLAN, f"plan is not valid YAML: {exc}") from exc
        return cls.from_mapping(data)

    # -- views ----------------------------------------------------------------

    def posts_for(self, provider: str) -> tuple[PostSpec, ...]:
        """The posts published on ``provider``: its variant if there is one."""
        return dict(self.variants).get(provider, self.posts)

    def media_refs(self) -> list[MediaRef]:
        """Every media reference across the base posts and all variants."""
        refs = [m for p in self.posts for m in p.media]
        for _, posts in self.variants:
            refs.extend(m for p in posts for m in p.media)
        return refs

    def with_accounts(self, accounts: tuple[str, ...]) -> Plan:
        """The same plan bound to explicit accounts (e.g. the default one)."""
        return Plan(
            posts=self.posts,
            accounts=tuple(sorted(set(accounts))),
            reply_to=self.reply_to,
            quote=self.quote,
            variants=self.variants,
            not_before=self.not_before,
        )

    # -- digest ---------------------------------------------------------------

    def canonical(self, media_sha256: Callable[[MediaRef], str]) -> dict[str, Any]:
        """The digested form. ``media_sha256`` returns a media item's content hash."""

        def posts(items: tuple[PostSpec, ...]) -> list[dict[str, Any]]:
            return [
                {
                    "text": p.text,
                    "media": [{"sha256": media_sha256(m), "alt": m.alt} for m in p.media],
                }
                for p in items
            ]

        return {
            "v": DIGEST_VERSION,
            "accounts": list(self.accounts),
            "posts": posts(self.posts),
            "reply_to": self.reply_to,
            "quote": self.quote,
            "variants": {provider: posts(items) for provider, items in self.variants},
        }

    def digest(self, media_sha256: Callable[[MediaRef], str]) -> str:
        blob = json.dumps(
            self.canonical(media_sha256),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _no_unknown(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise _invalid(f"unknown keys {sorted(unknown)}; allowed {sorted(allowed)}", where)


def _posts(data: Mapping[str, Any], where: str) -> tuple[PostSpec, ...]:
    has_posts = "posts" in data
    has_single = "text" in data or "media" in data
    if has_posts and has_single:
        raise _invalid("give `posts` or the single-post `text`/`media`, not both", where)
    if has_single:
        raw_posts: list[Any] = [{k: data[k] for k in ("text", "media") if k in data}]
    elif has_posts:
        if not isinstance(data["posts"], list):
            raise _invalid("must be a list of posts", f"{where}.posts")
        raw_posts = as_list(data.get("posts"))
    else:
        raise _invalid("no posts: give `posts` or `text`", where)
    if not raw_posts:
        raise _invalid("must not be empty", f"{where}.posts")
    if len(raw_posts) > MAX_THREAD_POSTS:
        raise _invalid(f"at most {MAX_THREAD_POSTS} posts per thread", f"{where}.posts")
    return tuple(_post(raw, f"{where}.posts[{i}]") for i, raw in enumerate(raw_posts))


def _post(raw: object, where: str) -> PostSpec:
    post = as_object(raw)
    if post is None:
        raise _invalid("must be a mapping with `text`", where)
    _no_unknown(post, _POST_KEYS, where)
    text = post.get("text")
    if not isinstance(text, str):
        raise _invalid("`text` must be a string", where)
    text = normalize_text(text)
    if not text:
        raise _invalid("`text` is empty", where)
    raw_media = post.get("media", [])
    if not isinstance(raw_media, list):
        raise _invalid("`media` must be a list of {path, alt}", where)
    entries = as_list(post.get("media"))
    media = tuple(_media(m, f"{where}.media[{j}]") for j, m in enumerate(entries))
    return PostSpec(text=text, media=media)


def _media(raw: object, where: str) -> MediaRef:
    item = as_object(raw)
    if item is None:
        raise _invalid("must be a mapping {path, alt}", where)
    _no_unknown(item, _MEDIA_KEYS, where)
    path = item.get("path")
    if not isinstance(path, str) or not path.strip():
        raise _invalid("`path` is required", where)
    alt = item.get("alt")
    if not isinstance(alt, str) or not normalize_text(alt):
        raise _invalid("`alt` text is required for every media item", where)
    return MediaRef(path=path.strip(), alt=normalize_text(alt))


def _id(value: object, where: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid("must be a post id string", where)
    text = str(value).strip()
    if not text:
        raise _invalid("must not be empty", where)
    return text


def _when(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        when = value
    elif isinstance(value, str):
        try:
            when = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise _invalid("must be an ISO 8601 timestamp", "not_before") from exc
    else:
        raise _invalid("must be an ISO 8601 timestamp", "not_before")
    if when.tzinfo is None:
        raise _invalid("needs a timezone (e.g. 2026-10-01T16:00:00Z)", "not_before")
    return when.astimezone(UTC)
