"""Media loading for ``upload_media``: confinement, type sniffing, limits, secret scan.

``upload_media`` publishes bytes to X, so reading a file is an exfiltration
path: without confinement, ``path="~/.ssh/id_rsa", mime="image/png"`` would
post a private key. The secret scanner is not a defence against that — most
secrets match none of its patterns. The defences here are, in order:

1. **Roots.** A path is resolved (``~`` expanded, symlinks followed, relative
   paths against the server's cwd) and must land inside one of the configured
   media roots (``config.toml [media] roots``; with none configured, path
   uploads are refused and only ``base64`` is accepted). A symlink that
   escapes the roots is refused; the pulsar home, where the token bundle and
   key live, is refused even when a root contains it.
2. **Regular files only**, opened without following symlinks at any
   component (a walk from the root with ``O_NOFOLLOW``), so a path swapped
   for a symlink after resolution cannot redirect the read. The opened fd
   must be the inode that was checked, and its size is checked against the
   limit *before* any byte is read; the read itself is bounded.
3. **Magic bytes are authoritative.** The content must be a PNG, JPEG, GIF,
   WebP or MP4. A declared ``mime`` (or, without one, the extension's guess)
   that disagrees with the sniffed type is refused rather than trusted.
4. The existing **secret scan** over the bytes, as a last net.

Errors name paths and types, never file contents.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

from .config import IMAGE_MIME_TYPES, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, VIDEO_MIME_TYPES
from .errors import INVALID_CONFIG, INVALID_MEDIA, SECRET_DETECTED, PulsarError
from .guard import scan_for_secrets

SUPPORTED_MIME_TYPES = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES

# ISO BMFF major brands that carry an ``ftyp`` box but are not MP4 video:
# QuickTime, and the HEIF/AVIF still-image family.
_NOT_MP4_BRANDS = frozenset({b"qt  ", b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"})

_SNIFF_BYTES = 16
_READ_CHUNK = 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024
_SCAN_OVERLAP = 128


def limit_for(mime: str) -> int:
    return MAX_VIDEO_BYTES if mime in VIDEO_MIME_TYPES else MAX_IMAGE_BYTES


def sniff_mime(head: bytes) -> str | None:
    """The media type the leading bytes prove, or None when they prove nothing we accept."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    # ISO BMFF: the first box is ``ftyp`` (size, 'ftyp', major brand, minor version).
    if (
        len(head) >= 16
        and head[4:8] == b"ftyp"
        and int.from_bytes(head[:4], "big") >= 16
        and head[8:12] not in _NOT_MP4_BRANDS
    ):
        return "video/mp4"
    return None


def _claimed_mime(mime: str | None, name: str | None) -> str | None:
    """What the caller says the media is: the declared ``mime``, else the extension's guess."""
    claimed = mime.strip().lower() if mime else None
    if not claimed and name:
        claimed = mimetypes.guess_type(name)[0]
    if claimed and claimed not in SUPPORTED_MIME_TYPES:
        raise PulsarError(
            INVALID_MEDIA,
            f"unsupported media type {claimed}; accepts {sorted(SUPPORTED_MIME_TYPES)}",
        )
    return claimed


def _check_size(size: int, limit: int) -> None:
    if size == 0:
        raise PulsarError(INVALID_MEDIA, "media is empty")
    if size > limit:
        raise PulsarError(INVALID_MEDIA, f"media is {size} bytes; limit is {limit}")


def _check_content(data: bytes, claimed: str | None) -> str:
    """Sniff, reconcile with the claim, apply the per-type limit, scan. Returns the MIME."""
    if not data:
        raise PulsarError(INVALID_MEDIA, "media is empty")
    sniffed = sniff_mime(data[:_SNIFF_BYTES])
    if sniffed is None:
        raise PulsarError(
            INVALID_MEDIA,
            "content is not a recognised png/jpeg/gif/webp/mp4 file",
            detail={"declared": claimed, "sniffed": None},
        )
    if claimed and claimed != sniffed:
        raise PulsarError(
            INVALID_MEDIA,
            f"declared type {claimed} does not match the content, which is {sniffed}",
            detail={"declared": claimed, "sniffed": sniffed},
        )
    _check_size(len(data), limit_for(sniffed))
    # Scan ASCII runs in binary media before any X write; overlap catches a
    # credential pattern split between chunks without decoding the whole file.
    for start in range(0, len(data), _SCAN_CHUNK):
        segment = data[max(0, start - _SCAN_OVERLAP) : start + _SCAN_CHUNK]
        hits = scan_for_secrets(segment.decode("ascii", errors="replace"))
        if hits:
            raise PulsarError(
                SECRET_DETECTED,
                "media contains something that looks like a credential; refusing to upload",
                detail={"matched": hits},
            )
    return sniffed


def _resolve_confined(path: str, roots: Iterable[Path], deny: Iterable[Path]) -> tuple[Path, Path]:
    """Resolve ``path`` strictly; return it with the root that contains it, or refuse."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise PulsarError(INVALID_MEDIA, f"no such file: {path}") from exc
    except (OSError, RuntimeError) as exc:
        raise PulsarError(INVALID_MEDIA, f"cannot resolve {path}: {type(exc).__name__}") from exc
    for denied in deny:
        if resolved.is_relative_to(denied.expanduser().resolve()):
            raise PulsarError(INVALID_MEDIA, f"refusing to read pulsar's own state: {path}")
    resolved_roots = [r.expanduser().resolve() for r in roots]
    for root in resolved_roots:
        if resolved.is_relative_to(root):
            return resolved, root
    raise PulsarError(
        INVALID_MEDIA,
        f"{path} is outside the allowed media roots",
        detail={"resolved": str(resolved), "roots": [str(r) for r in resolved_roots]},
    )


def _open_nofollow(resolved: Path, root: Path) -> int:
    """Open ``resolved`` by walking down from ``root`` without following any symlink.

    ``resolved`` has no symlinks in it, so meeting one here means the tree
    changed after resolution; ``O_NOFOLLOW`` turns that into an error instead
    of a redirected read. ``O_NONBLOCK`` keeps a FIFO swapped in from hanging
    the open; the caller refuses anything that is not a regular file.
    """
    parts = resolved.relative_to(root).parts
    if not parts:
        raise PulsarError(INVALID_MEDIA, f"not a regular file: {resolved}")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _read_bounded(fd: int, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes, so a file that grew after fstat still trips the limit."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _load_path(
    path: str, mime: str | None, roots: Iterable[Path], deny: Iterable[Path]
) -> tuple[bytes, str | None]:
    resolved, root = _resolve_confined(path, roots, deny)
    # lstat before opening so a device node or FIFO is refused without ever being opened.
    before = os.lstat(resolved)
    if not stat.S_ISREG(before.st_mode):
        raise PulsarError(INVALID_MEDIA, f"not a regular file: {path}")
    claimed = _claimed_mime(mime, Path(path).name)
    try:
        fd = _open_nofollow(resolved, root)
    except OSError as exc:
        raise PulsarError(
            INVALID_MEDIA, f"cannot open {path} safely: {type(exc).__name__}"
        ) from exc
    try:
        st = os.fstat(fd)
        same = (st.st_dev, st.st_ino) == (before.st_dev, before.st_ino)
        if not stat.S_ISREG(st.st_mode) or not same:
            raise PulsarError(INVALID_MEDIA, f"{path} changed while it was being opened")
        # Before reading: the claimed type's limit, or the largest limit when
        # nothing is claimed (the sniffed type's own limit is applied after).
        limit = limit_for(claimed) if claimed else max(MAX_IMAGE_BYTES, MAX_VIDEO_BYTES)
        _check_size(st.st_size, limit)
        return _read_bounded(fd, limit), claimed
    finally:
        os.close(fd)


def _load_base64(payload: str, mime: str | None) -> tuple[bytes, str | None]:
    claimed = _claimed_mime(mime, None)
    limit = limit_for(claimed) if claimed else max(MAX_IMAGE_BYTES, MAX_VIDEO_BYTES)
    # Four base64 characters per three bytes; refuse an oversized payload before decoding it.
    if len(payload) > (limit // 3 + 1) * 4:
        raise PulsarError(INVALID_MEDIA, f"media is over {limit} bytes; limit is {limit}")
    try:
        return base64.b64decode(payload, validate=True), claimed
    except (ValueError, binascii.Error) as exc:
        raise PulsarError(INVALID_MEDIA, "base64 payload is not valid") from exc


def load_media(
    path: str | None,
    base64_data: str | None,
    mime: str | None,
    *,
    roots: Sequence[Path],
    deny: Iterable[Path] = (),
) -> tuple[bytes, str]:
    """Return ``(bytes, mime)`` ready for X, or raise ``invalid_media`` / ``secret_detected``.

    ``roots`` are the directories a ``path`` may resolve into; ``deny`` are
    directories refused even inside a root (the pulsar home). The returned
    MIME is always the sniffed one.
    """
    if bool(path) == bool(base64_data):
        raise PulsarError(INVALID_MEDIA, "pass exactly one of `path` or `base64`")
    if path:
        if not roots:
            raise PulsarError(
                INVALID_CONFIG,
                "upload by path is off: the operator has not set [media] roots in "
                "config.toml; pass `base64` instead",
            )
        try:
            data, claimed = _load_path(path, mime, roots, deny)
        except OSError as exc:
            # A file removed or replaced mid-load; the errno name, never its contents.
            raise PulsarError(INVALID_MEDIA, f"cannot read {path}: {type(exc).__name__}") from exc
    else:
        data, claimed = _load_base64(base64_data or "", mime)
    return data, _check_content(data, claimed)
