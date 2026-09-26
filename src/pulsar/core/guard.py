"""The secret scanner: runs over every post, alt text and media before any write.

It matches credential *shapes* (provider key prefixes, bearer headers, PEM
blocks, JWTs) and machine-generated values assigned to a secret-named key,
never vocabulary. A key prefix must start a token, so
``task-sk-learning-pipeline-v2`` is an identifier, not a key; and
``password: correct-horse-battery-staple`` is prose, because the value does
not look generated. ``redact`` masks the same shapes for text that is about
to be persisted.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# A key prefix only counts at the start of a token: not after a letter, digit,
# underscore or hyphen, so it never matches inside a hyphenated identifier.
_START = r"(?<![\w-])"
# Where a secret-named key's value counts as a credential: long, varied, and
# not words joined by separators.
GENERATED_MIN_LENGTH = 20
GENERATED_MIN_ENTROPY_BITS = 3.5
_SEPARATORS = re.compile(r"[-_.~+/=]+")


@dataclass(frozen=True)
class SecretPattern:
    label: str
    regex: re.Pattern[str]
    # The ``value`` group must look machine-generated (``looks_generated``);
    # for the other patterns the prefix alone is high-confidence.
    generated_only: bool = False

    def matches(self, text: str) -> list[re.Match[str]]:
        return [m for m in self.regex.finditer(text) if self._counts(m)]

    def _counts(self, match: re.Match[str]) -> bool:
        return not self.generated_only or looks_generated(match.group("value"))


def _is_phrase(value: str) -> bool:
    """Words joined by separators: every part all letters, all digits, or tiny (``v2``)."""
    parts = [p for p in _SEPARATORS.split(value) if p]
    return len(parts) >= 2 and all(p.isalpha() or p.isdigit() or len(p) <= 3 for p in parts)


def looks_generated(value: str) -> bool:
    """True for a value shaped like a generated key or token, not a phrase.

    At least ``GENERATED_MIN_LENGTH`` characters, an empirical entropy of at
    least ``GENERATED_MIN_ENTROPY_BITS`` bits per character (so no
    ``hunter2hunter2...``), and not words joined by ``-``, ``_``, ``.`` and the
    like (``correct-horse-battery-staple``, ``v2-release-candidate-2026-09``).
    """
    if len(value) < GENERATED_MIN_LENGTH or _is_phrase(value):
        return False
    counts = Counter(value)
    entropy = -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())
    return entropy >= GENERATED_MIN_ENTROPY_BITS


# Ordered most specific first, so ``redact`` labels an Anthropic key as one
# before the generic ``sk-`` shape can claim it.
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("anthropic key", re.compile(_START + r"sk-ant-[A-Za-z0-9_-]{16,}")),
    SecretPattern("openai-style key", re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}")),
    SecretPattern("github token", re.compile(_START + r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    SecretPattern("github fine-grained pat", re.compile(_START + r"github_pat_[A-Za-z0-9_]{20,}")),
    SecretPattern("slack token", re.compile(_START + r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    SecretPattern("aws access key", re.compile(_START + r"(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    SecretPattern("google api key", re.compile(_START + r"AIza[0-9A-Za-z_-]{35}(?![\w-])")),
    SecretPattern("stripe key", re.compile(_START + r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    SecretPattern(
        "x/twitter bearer", re.compile(_START + r"AAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{20,}")
    ),
    SecretPattern(
        "pem block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?"
        ),
    ),
    SecretPattern(
        "bearer header", re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/=-]{20,})")
    ),
    SecretPattern(
        "jwt",
        re.compile(_START + r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    SecretPattern(
        "generic secret assignment",
        re.compile(
            r"(?i)" + _START + r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret|token|password|passwd)"
            r"\s*[=:]\s*['\"]?(?P<value>[A-Za-z0-9._~+/=-]+)"
        ),
        generated_only=True,
    ),
)


# The credential values this process has loaded (tokens, the store key). X's
# OAuth 2 tokens have no recognisable shape, so they are matched by value.
# Only long values are kept: a short one could be a word.
LIVE_LABEL = "live credential"
LIVE_MIN_LENGTH = 16
_live: set[str] = set()


def register_live_secret(value: str | None) -> None:
    """Mask ``value`` wherever it appears from now on, in this process."""
    if value is not None and len(value) >= LIVE_MIN_LENGTH:
        _live.add(value)


def scan_for_secrets(text: str) -> list[str]:
    """The labels of every credential found in ``text``: a live value first,
    then each shape in pattern order."""
    live = [LIVE_LABEL] if any(v in text for v in _live) else []
    return live + [p.label for p in SECRET_PATTERNS if p.matches(text)]


def redact(text: str) -> str:
    """``text`` with every live value and credential shape masked.

    The secret (a pattern's ``value`` group, else the whole match) becomes
    ``[redacted:<label>]``; the words around it, such as ``Bearer`` or
    ``api_key =``, stay so the record still reads.
    """
    for value in sorted(_live, key=len, reverse=True):
        text = text.replace(value, f"[redacted:{LIVE_LABEL}]")
    for pattern in SECRET_PATTERNS:
        text = _mask(pattern, text)
    return text


def _mask(pattern: SecretPattern, text: str) -> str:
    out: list[str] = []
    last = 0
    for match in pattern.matches(text):
        group = "value" if "value" in pattern.regex.groupindex else 0
        start, end = match.span(group)
        out.append(text[last:start])
        out.append(f"[redacted:{pattern.label}]")
        last = end
    out.append(text[last:])
    return "".join(out)
