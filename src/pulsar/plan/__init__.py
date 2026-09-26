"""A publish plan: the validated, channel-neutral description of a thread to
post (``model``), and the ``provider:handle`` account aliases it names
(``aliases``).
"""

from __future__ import annotations

from .aliases import alias_provider, normalize_alias
from .model import MediaRef, Plan, PostSpec

__all__ = [
    # aliases
    "alias_provider",
    "normalize_alias",
    # model
    "MediaRef",
    "Plan",
    "PostSpec",
]
