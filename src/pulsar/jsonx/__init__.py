"""Typed views over parsed JSON: narrow an untyped value to an object or a
list, or fail with a ``PulsarError``.
"""

from __future__ import annotations

from .views import JSONObject, as_list, as_object, obj

__all__ = [
    # views
    "as_list",
    "as_object",
    "JSONObject",
    "obj",
]
