"""Encrypted token bundle on disk.

Fernet with a host-local key file (mode 0600). Anyone with the key file can
decrypt, so this is defence against casual disclosure (backups, ``cat``, a
stray ``git add``), not against a compromised host account. That matches
the spec: the *agent* never sees raw secrets; the connector process does.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from cryptography.fernet import Fernet, InvalidToken

from .config import Paths


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    scope: str
    client_id: str
    token_type: str = "bearer"

    def expires_within(self, seconds: float) -> bool:
        return time.time() + seconds >= self.expires_at

    @classmethod
    def from_token_response(
        cls, data: dict, *, client_id: str, now: float | None = None
    ) -> TokenBundle:
        now = time.time() if now is None else now
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=now + float(data.get("expires_in", 7200)),
            scope=data.get("scope", ""),
            client_id=client_id,
            token_type=data.get("token_type", "bearer"),
        )


def _write_private(path: os.PathLike[str] | str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


class TokenStore:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def _fernet(self, create: bool) -> Fernet:
        key_file = self.paths.key_file
        if key_file.exists():
            return Fernet(key_file.read_bytes().strip())
        if not create:
            raise FileNotFoundError(str(key_file))
        self.paths.ensure()
        key = Fernet.generate_key()
        _write_private(key_file, key)
        return Fernet(key)

    def exists(self) -> bool:
        return self.paths.token_file.exists() and self.paths.key_file.exists()

    def load(self) -> TokenBundle | None:
        if not self.exists():
            return None
        try:
            raw = self._fernet(create=False).decrypt(self.paths.token_file.read_bytes())
        except (InvalidToken, FileNotFoundError):
            return None
        return TokenBundle(**json.loads(raw))

    def save(self, bundle: TokenBundle) -> None:
        self.paths.ensure()
        blob = self._fernet(create=True).encrypt(json.dumps(asdict(bundle)).encode())
        _write_private(self.paths.token_file, blob)

    def clear(self) -> None:
        for p in (self.paths.token_file, self.paths.whoami_cache):
            if p.exists():
                p.unlink()
