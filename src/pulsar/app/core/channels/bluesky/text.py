"""Bluesky's text rules: grapheme length, rich-text facets, and the pre-flight report.

Bluesky counts a post in graphemes (user-perceived characters, UAX #29), at
most 300, and caps its UTF-8 size at 3000 bytes. ``grapheme_count`` follows the
UAX #29 extended grapheme cluster rules pulsar can apply from ``unicodedata``
(CR LF, controls, extenders and spacing marks, Hangul syllables, regional
indicator pairs, emoji ZWJ sequences with an approximate pictographic range);
it leaves out the Indic conjunct rule, so it can count a conjunct as more than
one grapheme and refuse a post Bluesky would take, never the reverse.

Links, mentions and hashtags are not markup in Bluesky: a post carries
``facets`` that point at them by UTF-8 byte offsets. ``detect_facets`` finds
them the way the Bluesky app does (a URL or a bare domain with a recognised
TLD, ``@handle.domain``, ``#tag``); a mention still needs its handle resolved
to a DID, which ``create`` does.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pulsar.internal.errors import INVALID_TEXT, SECRET_DETECTED, PulsarError
from pulsar.internal.guard import scan_for_secrets

from ..contract import Prices
from .config import MAX_POST_BYTES, MAX_POST_GRAPHEMES
from .tlds import TLDS

# -- graphemes ----------------------------------------------------------------

# Extended_Pictographic, approximately: the emoji blocks and the older symbols
# emoji presentation uses. Only the ZWJ rule reads it.
_PICTOGRAPHIC = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x231A, 0x23FF),
    (0x24C2, 0x24C2),
    (0x25AA, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3299),
    (0x1F000, 0x1FAFF),
)


def _hangul(cp: int) -> str | None:
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _kind(ch: str) -> str:
    """The grapheme break class of ``ch``, as far as the rules below need it."""
    cp = ord(ch)
    if ch == "\r":
        return "CR"
    if ch == "\n":
        return "LF"
    if cp == 0x200D:
        return "ZWJ"
    category = unicodedata.category(ch)
    if (
        category in ("Mn", "Me", "Mc")
        or cp == 0x200C
        or 0x1F3FB <= cp <= 0x1F3FF  # emoji skin-tone modifiers
        or 0xE0020 <= cp <= 0xE007F  # tags (subdivision flags)
    ):
        return "Extend"
    if category in ("Cc", "Cf", "Zl", "Zp"):
        return "Control"
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return "RI"
    if (hangul := _hangul(cp)) is not None:
        return hangul
    if any(lo <= cp <= hi for lo, hi in _PICTOGRAPHIC):
        return "Pict"
    return "Other"


def _joins(prev: str, kind: str, *, pict: bool, ri_open: bool) -> bool:
    """Whether ``kind`` continues the cluster ``prev`` ended (no break between them)."""
    if prev == "CR" and kind == "LF":
        return True
    if prev in ("CR", "LF", "Control") or kind in ("CR", "LF", "Control"):
        return False
    if prev == "L" and kind in ("L", "V", "LV", "LVT"):
        return True
    if prev in ("LV", "V") and kind in ("V", "T"):
        return True
    if prev in ("LVT", "T") and kind == "T":
        return True
    if kind in ("Extend", "ZWJ"):
        return True
    if prev == "ZWJ" and kind == "Pict" and pict:
        return True
    return prev == "RI" and kind == "RI" and ri_open


def grapheme_count(text: str) -> int:
    """User-perceived characters in ``text`` (UAX #29 extended grapheme clusters)."""
    count = 0
    prev: str | None = None
    pict = False  # the cluster's base is pictographic (for emoji ZWJ sequences)
    ri_open = False  # the cluster is one regional indicator, waiting for its pair
    for ch in text:
        kind = _kind(ch)
        if prev is None or not _joins(prev, kind, pict=pict, ri_open=ri_open):
            count += 1
            pict = kind == "Pict"
            ri_open = kind == "RI"
        elif kind == "RI":
            ri_open = False
        prev = kind
    return count


# -- facets -------------------------------------------------------------------

# A URL, or a bare domain (checked against the offline TLD list below), after the
# start, whitespace or "(" — the Bluesky app's detection.
_LINK = re.compile(r"(?:^|(?<=[\s(]))(https?://\S+|[a-z][a-z0-9]*(?:\.[a-z0-9]+)+\S*)", re.I)
_BARE_DOMAIN = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)+", re.I)
_MENTION = re.compile(r"(?:^|(?<=[\s(]))@([a-zA-Z0-9.-]+)")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HANDLE = re.compile(rf"(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")
_TAG = re.compile(r"(?:^|(?<=\s))[#\uff03]([^\s\u00ad\u2060\u200a\u200b\u200c\u200d\u20e2]+)")
MAX_TAG_LENGTH = 64

FacetKind = Literal["link", "mention", "tag"]


@dataclass(frozen=True)
class Facet:
    """A span of the post's text (UTF-8 byte offsets, end exclusive) and what it means."""

    byte_start: int
    byte_end: int
    kind: FacetKind
    value: str  # the link's URI, the mentioned handle, or the tag without "#"


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _links(text: str) -> list[tuple[int, int, FacetKind, str]]:
    found: list[tuple[int, int, FacetKind, str]] = []
    for m in _LINK.finditer(text):
        span = m.group(1)
        if not span.lower().startswith(("http://", "https://")):
            domain = _BARE_DOMAIN.match(span)
            tld = domain.group(0).rsplit(".", 1)[-1] if domain else ""
            if tld.lower() not in TLDS:
                continue
        if span[-1] in ".,;:!?":
            span = span[:-1]
        if span.endswith(")") and "(" not in span:
            span = span[:-1]
        uri = span if span.lower().startswith(("http://", "https://")) else f"https://{span}"
        found.append((m.start(1), m.start(1) + len(span), "link", uri))
    return found


def _mentions(text: str) -> list[tuple[int, int, FacetKind, str]]:
    found: list[tuple[int, int, FacetKind, str]] = []
    for m in _MENTION.finditer(text):
        handle = m.group(1).rstrip(".-")
        if len(handle) <= 253 and _HANDLE.fullmatch(handle.lower()):
            found.append((m.start(), m.start(1) + len(handle), "mention", handle.lower()))
    return found


def _tags(text: str) -> list[tuple[int, int, FacetKind, str]]:
    found: list[tuple[int, int, FacetKind, str]] = []
    for m in _TAG.finditer(text):
        tag = m.group(1)
        if tag.startswith("\ufe0f"):  # a keycap number sign, not a tag
            continue
        while tag and _is_punct(tag[-1]):
            tag = tag[:-1]
        if not tag or len(tag) > MAX_TAG_LENGTH:
            continue
        if all(c.isdigit() or _is_punct(c) for c in tag):
            continue
        found.append((m.start(), m.start(1) + len(tag), "tag", tag))
    return found


def detect_facets(text: str) -> tuple[Facet, ...]:
    """Links, mentions and tags in ``text``, in order; a span overlapping an earlier one is
    dropped."""
    spans = sorted(_links(text) + _mentions(text) + _tags(text), key=lambda s: (s[0], -s[1]))
    facets: list[Facet] = []
    end = 0
    for start, stop, kind, value in spans:
        if start < end:
            continue
        facets.append(
            Facet(
                byte_start=len(text[:start].encode("utf-8")),
                byte_end=len(text[:stop].encode("utf-8")),
                kind=kind,
                value=value,
            )
        )
        end = stop
    return tuple(facets)


# -- the report -----------------------------------------------------------------


@dataclass(frozen=True)
class TextReport:
    text: str
    graphemes: int
    has_url: bool
    estimated_cost_usd: float


def validate_text(text: object, prices: Prices) -> TextReport:
    """Raise PulsarError(invalid_text | secret_detected) or return a report."""
    if not isinstance(text, str) or not text.strip():
        raise PulsarError(INVALID_TEXT, "text is empty")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise PulsarError(INVALID_TEXT, "text contains control characters")
    length = grapheme_count(text)
    if length > MAX_POST_GRAPHEMES:
        raise PulsarError(
            INVALID_TEXT,
            f"text is {length} graphemes; Bluesky allows {MAX_POST_GRAPHEMES}",
            detail={"graphemes": length, "limit": MAX_POST_GRAPHEMES},
        )
    size = len(text.encode("utf-8"))
    if size > MAX_POST_BYTES:
        raise PulsarError(
            INVALID_TEXT,
            f"text is {size} UTF-8 bytes; Bluesky allows {MAX_POST_BYTES}",
            detail={"bytes": size, "limit": MAX_POST_BYTES},
        )
    hits = scan_for_secrets(text)
    if hits:
        raise PulsarError(
            SECRET_DETECTED,
            "text contains something that looks like a credential; refusing to post",
            detail={"matched": hits},
        )
    has_url = any(f.kind == "link" for f in detect_facets(text))
    return TextReport(
        text=text,
        graphemes=length,
        has_url=has_url,
        estimated_cost_usd=prices.for_post(has_url=has_url),
    )
