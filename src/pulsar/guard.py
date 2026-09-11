"""Pre-flight checks that run before any network call: text validation,
secret scanning, and the cost estimate."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import COST_PLAIN_POST_USD, COST_URL_POST_USD, MAX_POST_WEIGHTED_LENGTH
from .errors import INVALID_TEXT, SECRET_DETECTED, PulsarError

# Close enough to twitter-text's URL extraction for length/cost purposes.
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>\"']+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[^\s<>\"']*)?"
)
URL_WEIGHT = 23

# Code-point ranges that weigh 1 under X's counting rules; everything else weighs 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github fine-grained pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("x/twitter bearer", re.compile(r"\bAAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{20,}")),
    ("pem block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "generic secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\s*[=:]\s*['\"]?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
)


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


def scan_for_secrets(text: str) -> list[str]:
    return [label for label, pat in SECRET_PATTERNS if pat.search(text)]


@dataclass(frozen=True)
class TextReport:
    text: str
    weighted_length: int
    has_url: bool
    estimated_cost_usd: float


def validate_text(text: str | None) -> TextReport:
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
        estimated_cost_usd=COST_URL_POST_USD if has_url else COST_PLAIN_POST_USD,
    )
