"""Typed views over parsed JSON, so untrusted shapes are checked where they are read."""

from __future__ import annotations

from typing import Any, cast

type JSONObject = dict[str, Any]


def as_object(value: object) -> JSONObject | None:
    """``value`` as a JSON object, or None when it is anything else."""
    return cast(JSONObject, value) if isinstance(value, dict) else None


def obj(value: object) -> JSONObject:
    """``value`` as a JSON object; anything else reads as empty."""
    return as_object(value) or {}


def as_list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []
