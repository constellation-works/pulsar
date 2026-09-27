"""Account aliases: ``provider:handle``, lower-cased, a leading ``@`` dropped
(``X:@ConstWorks`` is ``x:constworks``)."""

from __future__ import annotations

from pulsar.internal.errors import INVALID_PLAN, PulsarError


def normalize_alias(value: str) -> str:
    """``X:@ConstWorks`` -> ``x:constworks``. Raises ``invalid_plan`` without a provider."""
    provider, sep, handle = value.strip().partition(":")
    handle = handle.strip().removeprefix("@")
    if not sep or not provider.strip() or not handle:
        raise PulsarError(
            INVALID_PLAN,
            f"account: account {value!r} must be provider:handle, e.g. x:<handle>",
            detail={"at": "account"},
        )
    return f"{provider.strip().lower()}:{handle.lower()}"


def alias_provider(alias: str) -> str:
    return alias.partition(":")[0]
