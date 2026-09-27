"""``client.json``: the OAuth client id each provider's logins use, one per
provider and shared by every account of it.

It is written under the registry's ``accounts.lock`` so two logins for
different providers cannot drop each other's record.
"""

from __future__ import annotations

import json

from pulsar.internal.fs import Paths, as_object, obj, require_private, write_private_atomic

from .registry import AccountRegistry

# The phase 1 file held X's record at the top level.
LEGACY_PROVIDER = "x"


def _client_records(paths: Paths) -> dict[str, dict[str, str]]:
    """``client.json`` by provider. The phase 1 file was X's record at the top level.

    A symlinked, foreign or group/world-writable file is ``insecure_storage``:
    the client id decides which app the human authorizes.
    """
    require_private(
        paths.client_file,
        readable=True,
        consequence="pulsar will not use an OAuth client id others could have changed",
    )
    try:
        data = obj(json.loads(paths.client_file.read_text()))
    except (FileNotFoundError, ValueError):
        return {}
    if "client_id" in data:  # phase 1: {"client_id", "redirect_uri"}
        return {LEGACY_PROVIDER: {k: str(v) for k, v in data.items()}}
    out: dict[str, dict[str, str]] = {}
    for provider, record in data.items():
        fields = as_object(record)
        if fields is not None:
            out[provider] = {k: str(v) for k, v in fields.items()}
    return out


def save_client_id(paths: Paths, provider: str, client_id: str, *, redirect_uri: str) -> None:
    """Remember ``provider``'s OAuth client id, under ``accounts.lock``."""
    with AccountRegistry(paths).locked_update("client.json update"):
        records = _client_records(paths)
        records[provider] = {"client_id": client_id, "redirect_uri": redirect_uri}
        doc = json.dumps(records, indent=2, sort_keys=True) + "\n"
        write_private_atomic(paths.client_file, doc.encode())


def load_client_id(paths: Paths, provider: str) -> str | None:
    return _client_records(paths).get(provider, {}).get("client_id") or None
