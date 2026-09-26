"""Smallest byte strings that sniff as each accepted media type (plus a few that must not)."""

import base64

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00;"
WEBP = (
    b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00"
    b"/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"
)
# An ``ftyp`` box (isom major brand, isom/mp41 compatible) and an empty ``mdat``.
MP4 = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp41"
    + b"\x00\x00\x00\x08mdat"
    + b"distinct-video-payload"
)
QUICKTIME = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  "

PEM_KEY = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    b"-----END OPENSSH PRIVATE KEY-----\n"
)
