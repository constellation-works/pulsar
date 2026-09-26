"""X's text rules: weighted length, URL detection, and the pre-flight report."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from pulsar.core import INVALID_TEXT, SECRET_DETECTED, Prices, PulsarError, scan_for_secrets

from .config import MAX_POST_WEIGHTED_LENGTH

# Close enough to twitter-text's URL extraction for length/cost purposes.
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>\"']+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[^\s<>\"']*)?"
)
URL_WEIGHT = 23

# Code-point ranges that weigh 1 under X's counting rules; everything else weighs 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def weighted_length(text: str) -> int:
    """Length as X counts it: NFC, URLs at 23, light code points at 1, others at 2."""
    text = unicodedata.normalize("NFC", text)
    total = 0
    last = 0
    for m in URL_RE.finditer(text):
        total += _weigh(text[last : m.start()]) + URL_WEIGHT
        last = m.end()
    total += _weigh(text[last:])
    return total


def _weigh(segment: str) -> int:
    n = 0
    for ch in segment:
        cp = ord(ch)
        n += 1 if any(lo <= cp <= hi for lo, hi in _LIGHT_RANGES) else 2
    return n


def contains_url(text: str) -> bool:
    return URL_RE.search(text) is not None


@dataclass(frozen=True)
class TextReport:
    text: str
    weighted_length: int
    has_url: bool
    estimated_cost_usd: float


def validate_text(text: object, prices: Prices | None = None) -> TextReport:
    """Raise PulsarError(invalid_text | secret_detected) or return a report."""
    if text is None or not isinstance(text, str):
        raise PulsarError(INVALID_TEXT, "text is required")
    stripped = text.strip()
    if not stripped:
        raise PulsarError(INVALID_TEXT, "text is empty")
    if "\x00" in text or any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise PulsarError(INVALID_TEXT, "text contains control characters")
    length = weighted_length(text)
    if length > MAX_POST_WEIGHTED_LENGTH:
        raise PulsarError(
            INVALID_TEXT,
            f"text is {length} weighted characters; limit is {MAX_POST_WEIGHTED_LENGTH}",
            detail={"weighted_length": length, "limit": MAX_POST_WEIGHTED_LENGTH},
        )
    hits = scan_for_secrets(text)
    if hits:
        raise PulsarError(
            SECRET_DETECTED,
            "text contains something that looks like a credential; refusing to post",
            detail={"matched": hits},
        )
    has_url = contains_url(text)
    return TextReport(
        text=text,
        weighted_length=length,
        has_url=has_url,
        estimated_cost_usd=(prices or Prices()).for_post(has_url=has_url),
    )
