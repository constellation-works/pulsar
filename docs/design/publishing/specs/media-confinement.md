---
type: design
summary: "Spec: media confinement — roots, regular files, bounded reads, content-typed uploads"
last_validated: 2026-09-26
---

# Spec: Media Confinement

pulsar publishes whatever bytes it reads, so it reads media only from configured roots, only
from regular files it opened itself without following symlinks, only up to the size limit, and
only when the content is an allowed image or video type.

## Why This Exists

A file path is an exfiltration route: `upload_media(path="~/.ssh/id_rsa", mime="image/png")`
would publish a private key. The secret scanner is not the defence; most secrets match none of
its patterns.

## Roots

- `media.roots` in `config.toml` are the only directories a `path` is read from. With none
  configured, path uploads are refused with `invalid_config`; `base64` still works. The
  server's cwd is not a default: some MCP hosts start servers in `/` or `$HOME`.
- Roots must be absolute (`~` is expanded). `/`, the home directory and their ancestors are
  refused.
- The Orbit plugin replaces the roots with the workspace root for its calls: the sandbox can
  read nothing else.
- `path` is `~`-expanded and fully resolved (a relative path against the cwd, or the workspace
  for plugin calls). The result must be inside a root. A symlink that stays inside is fine; one
  that points outside is refused. The pulsar home is refused even when a root contains it.

## Opening

- Directories, FIFOs and devices are refused before they are opened.
- The file is opened by walking down from the root with no-follow opens, and the opened file
  must be the one that was checked, so swapping the path after the check does not redirect the
  read.
- The size limit is checked from the open file's metadata before any byte is read, and the read
  stops one byte past the limit.

## Type

- The leading bytes decide the type: PNG, JPEG, GIF, WebP, or MP4 (ISO BMFF `ftyp`; QuickTime
  and HEIF/AVIF brands are refused).
- A `mime` argument, or without one the file extension, that disagrees with the content is
  `invalid_media` with `detail: {declared, sniffed}`. The returned `mime` is always the sniffed
  one. The same rules apply to `base64` payloads.
- Limits (X): images 5 MiB, video 100 MiB (a local cap; X also checks the account's
  entitlement).

## After Loading

- The bytes go through the secret scanner.
- Errors name paths and types, never file contents.
- The ledger row records MIME, byte count, processing state and the media id; never the bytes.
