"""Media confinement: roots, symlinks, file kinds, bounded reads, and magic-byte sniffing.

These call ``load_media`` directly; ``test_server.py`` covers the same
refusals through the MCP tool and asserts no request reaches X.
"""

import base64
import os
from pathlib import Path

import pytest

from pulsar.errors import PulsarError
from pulsar.publishing import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, load_media, media, sniff_mime

from .media_samples import GIF, JPEG, MP4, PEM_KEY, PNG, QUICKTIME, WEBP


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def outside(tmp_path):
    o = tmp_path / "outside"
    o.mkdir()
    return o


def _refused(*args, code="invalid_media", **kwargs) -> PulsarError:
    with pytest.raises(PulsarError) as exc:
        load_media(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


def _no_contents(err: PulsarError, secret: bytes) -> None:
    blob = f"{err.message} {err.detail}"
    for line in secret.decode().splitlines():
        if line.strip() and "-----" not in line:
            assert line not in blob


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        ("a.png", PNG, "image/png"),
        ("a.jpg", JPEG, "image/jpeg"),
        ("a.gif", GIF, "image/gif"),
        ("a.webp", WEBP, "image/webp"),
        ("a.mp4", MP4, "video/mp4"),
        ("noext", PNG, "image/png"),
    ],
)
def test_valid_media_under_a_root_is_accepted(root, name, data, mime):
    (root / name).write_bytes(data)
    assert load_media(str(root / name), None, None, roots=[root]) == (data, mime)
    assert load_media(str(root / name), None, mime, roots=[root]) == (data, mime)
    b64 = base64.b64encode(data).decode()
    assert load_media(None, b64, mime, roots=[]) == (data, mime)
    assert load_media(None, b64, None, roots=[]) == (data, mime), "mime is sniffed when omitted"


def test_private_key_declared_png_outside_roots_is_refused_before_reading(
    root, outside, monkeypatch
):
    key = outside / "id_rsa"
    key.write_bytes(PEM_KEY)

    def no_open(*_a, **_k):
        raise AssertionError("a file outside the roots must not be opened")

    monkeypatch.setattr(media.os, "open", no_open)
    err = _refused(str(key), None, "image/png", roots=[root])
    assert "outside the allowed media roots" in err.message
    _no_contents(err, PEM_KEY)


def test_private_key_declared_png_inside_a_root_is_refused_by_sniffing(root):
    key = root / "id_rsa"
    key.write_bytes(PEM_KEY)
    err = _refused(str(key), None, "image/png", roots=[root])
    assert err.detail == {"declared": "image/png", "sniffed": None}
    _no_contents(err, PEM_KEY)
    # Renaming it to look like an image changes nothing.
    disguised = root / "photo.png"
    key.rename(disguised)
    err = _refused(str(disguised), None, None, roots=[root])
    assert "not a recognised" in err.message
    _no_contents(err, PEM_KEY)


def test_symlink_escaping_the_root_is_refused(root, outside):
    (outside / "pic.png").write_bytes(PNG)
    (root / "link.png").symlink_to(outside / "pic.png")
    err = _refused(str(root / "link.png"), None, None, roots=[root])
    assert "outside the allowed media roots" in err.message


def test_symlinked_directory_escaping_the_root_is_refused(root, outside):
    (outside / "pic.png").write_bytes(PNG)
    (root / "sub").symlink_to(outside, target_is_directory=True)
    _refused(str(root / "sub" / "pic.png"), None, None, roots=[root])


def test_symlink_within_the_root_is_allowed(root):
    (root / "real").mkdir()
    (root / "real" / "pic.png").write_bytes(PNG)
    (root / "link.png").symlink_to(root / "real" / "pic.png")
    assert load_media(str(root / "link.png"), None, None, roots=[root]) == (PNG, "image/png")


def test_a_symlinked_root_is_followed(tmp_path, outside):
    (outside / "pic.png").write_bytes(PNG)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert load_media(str(alias / "pic.png"), None, None, roots=[alias])[1] == "image/png"


def test_dotdot_traversal_is_refused(root, outside):
    (outside / "pic.png").write_bytes(PNG)
    err = _refused(str(root / ".." / "outside" / "pic.png"), None, None, roots=[root])
    assert "outside the allowed media roots" in err.message


def test_relative_paths_resolve_against_the_given_base(root, monkeypatch):
    (root / "pic.png").write_bytes(PNG)
    monkeypatch.chdir(root.parent)  # the process cwd is never consulted
    assert load_media("pic.png", None, None, roots=[root], base=root)[1] == "image/png"
    _refused("pic.png", None, None, roots=[root], base=root.parent)  # no such file there


def test_a_relative_path_without_a_base_is_refused(root, monkeypatch):
    (root / "pic.png").write_bytes(PNG)
    monkeypatch.chdir(root)
    err = _refused("pic.png", None, None, roots=[root])
    assert "relative" in err.message


def test_a_tilde_path_is_not_expanded(root, monkeypatch):
    monkeypatch.setenv("HOME", str(root))
    (root / "pic.png").write_bytes(PNG)
    _refused("~/pic.png", None, None, roots=[root], base=root.parent)


def test_pulsar_home_inside_a_root_is_refused(tmp_path):
    home = tmp_path / "pulsar-home"
    home.mkdir()
    (home / "key.png").write_bytes(PNG)
    err = _refused(str(home / "key.png"), None, None, roots=[tmp_path], deny=[home])
    assert "pulsar's own state" in err.message


def test_extension_that_disagrees_with_content_is_refused(root):
    fake = root / "photo.png"
    fake.write_bytes(JPEG)
    err = _refused(str(fake), None, None, roots=[root])
    assert err.detail == {"declared": "image/png", "sniffed": "image/jpeg"}
    err = _refused(str(fake), None, "image/png", roots=[root])
    assert "image/png" in err.message and "image/jpeg" in err.message
    # A declared mime that matches the content wins over a misleading extension.
    assert load_media(str(fake), None, "image/jpeg", roots=[root]) == (JPEG, "image/jpeg")


def test_quicktime_is_not_mp4(root):
    assert sniff_mime(QUICKTIME) is None
    (root / "clip.mp4").write_bytes(QUICKTIME)
    _refused(str(root / "clip.mp4"), None, None, roots=[root])


@pytest.mark.parametrize(
    "head",
    [b"", b"\x89PNG", b"RIFF\x00\x00\x00\x00WAVE", b"\x00\x00\x00\x04ftypisom\x00\x00\x00\x00"],
)
def test_unrecognised_magic_sniffs_as_nothing(head):
    assert sniff_mime(head) is None


def test_directory_is_refused(root):
    (root / "dir.png").mkdir()
    err = _refused(str(root / "dir.png"), None, None, roots=[root])
    assert "not a regular file" in err.message


def test_fifo_is_refused_without_blocking(root):
    fifo = root / "pipe.png"
    os.mkfifo(fifo)
    err = _refused(str(fifo), None, None, roots=[root])
    assert "not a regular file" in err.message


def test_device_is_refused():
    err = _refused("/dev/zero", None, "image/png", roots=[Path("/dev")])
    assert "not a regular file" in err.message


def test_empty_file_is_refused(root):
    (root / "empty.png").write_bytes(b"")
    assert "empty" in _refused(str(root / "empty.png"), None, None, roots=[root]).message


@pytest.mark.parametrize(
    ("name", "size", "limit"),
    [
        ("big.png", MAX_IMAGE_BYTES + 1, MAX_IMAGE_BYTES),
        ("big.mp4", MAX_VIDEO_BYTES + 1, MAX_VIDEO_BYTES),
        ("big", MAX_VIDEO_BYTES + 1, MAX_VIDEO_BYTES),  # nothing claimed: the largest limit
    ],
)
def test_oversize_file_is_refused_without_reading(root, monkeypatch, name, size, limit):
    sparse = root / name
    with sparse.open("wb") as fh:
        fh.truncate(size)
    real_read = os.read
    read_bytes = []

    def counting_read(fd, n):
        chunk = real_read(fd, n)
        read_bytes.append(len(chunk))
        return chunk

    monkeypatch.setattr(media.os, "read", counting_read)
    err = _refused(str(sparse), None, None, roots=[root])
    assert f"limit is {limit}" in err.message
    assert sum(read_bytes) == 0


def test_unclaimed_image_over_the_image_limit_is_refused_after_sniffing(root):
    big = root / "big"
    big.write_bytes(PNG + b"\x00" * MAX_IMAGE_BYTES)
    err = _refused(str(big), None, None, roots=[root])
    assert f"limit is {MAX_IMAGE_BYTES}" in err.message


def test_read_is_bounded_even_if_the_file_grew(root, monkeypatch):
    """A file that grows between fstat and read still cannot exceed the limit."""
    (root / "pic.png").write_bytes(PNG + b"\x00" * 64)
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 16)
    real_fstat = os.fstat

    def shrunk_fstat(fd):
        st = real_fstat(fd)
        return os.stat_result((*st[:6], 8, *st[7:]))  # st_size index is 6

    monkeypatch.setattr(media.os, "fstat", shrunk_fstat)
    err = _refused(str(root / "pic.png"), None, None, roots=[root])
    assert "limit is 16" in err.message and "media is 17 bytes" in err.message


def test_file_replaced_after_the_check_is_refused(root, monkeypatch):
    """The fd must be the inode that was checked, not whatever is at the path by then."""
    (root / "pic.png").write_bytes(PNG)
    (root / "other.png").write_bytes(PNG)
    real_open = media.open_beneath

    def replace_then_open(resolved, r):
        os.replace(root / "other.png", root / "pic.png")
        return real_open(resolved, r)

    monkeypatch.setattr(media, "open_beneath", replace_then_open)
    err = _refused(str(root / "pic.png"), None, None, roots=[root])
    assert "changed while" in err.message


def test_symlink_swapped_in_after_resolution_is_not_followed(root, outside, monkeypatch):
    (outside / "pic.png").write_bytes(PNG)
    (root / "pic.png").write_bytes(PNG)
    real_open = media.open_beneath

    def swap_then_open(resolved, r):
        (root / "pic.png").unlink()
        (root / "pic.png").symlink_to(outside / "pic.png")
        return real_open(resolved, r)

    monkeypatch.setattr(media, "open_beneath", swap_then_open)
    err = _refused(str(root / "pic.png"), None, None, roots=[root])
    assert "cannot open" in err.message


def test_base64_with_mismatched_mime_is_refused():
    err = _refused(None, base64.b64encode(JPEG).decode(), "image/png", roots=[])
    assert err.detail == {"declared": "image/png", "sniffed": "image/jpeg"}
    err = _refused(None, base64.b64encode(PEM_KEY).decode(), "image/png", roots=[])
    _no_contents(err, PEM_KEY)


def test_oversize_base64_is_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 8)

    def no_decode(*_a, **_k):
        raise AssertionError("an oversized payload must not be decoded")

    monkeypatch.setattr(media.base64, "b64decode", no_decode)
    err = _refused(None, base64.b64encode(PNG).decode(), "image/png", roots=[])
    assert "limit is 8" in err.message


def test_secret_in_valid_media_is_still_caught(root):
    (root / "pic.png").write_bytes(PNG + b" sk-abcdefghijklmnopqrstuvwxyz ")
    err = _refused(str(root / "pic.png"), None, None, roots=[root], code="secret_detected")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in f"{err.message} {err.detail}"
