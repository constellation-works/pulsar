"""The secret scanner: runs over every post, alt text and media before any write."""

from __future__ import annotations

import re

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


def scan_for_secrets(text: str) -> list[str]:
    return [label for label, pat in SECRET_PATTERNS if pat.search(text)]
