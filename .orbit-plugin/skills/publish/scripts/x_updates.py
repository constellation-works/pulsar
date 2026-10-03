"""Plan x-updates drafts: which announcements are new, under which idempotency key.

This skill helper reads JSON on stdin and prints JSON on stdout. It calls no
service and writes nothing; it reads only the plan files under ``x-updates/``
in the current directory (the workspace), to see what is already drafted.

``keys``: ``{"candidates": [...]}`` -> ``{"keys": [...]}``, the keys to look up
with ``pulsar.history``. A candidate is one of::

    {"kind": "release", "repo": "orbit", "tag": "v0.26.0", ...}
    {"kind": "repo", "repo": "pulsar", ...}
    {"kind": "pr", "repo": "orbit", "number": 123, ...}

and its key is ``release:<repo>:<tag>``, ``repo:<name>`` or ``pr:<repo>:<n>``,
the keys the retired routine used (its history is imported into the ledger).

``plan``: ``{"candidates", "history", "tasks", "date"?, "max_drafts"?}`` ->
``{"drafts", "skipped", "deferred"}``. ``history`` is the ``pulsar.history``
output for those keys; ``tasks`` the complete ``orbit.task.list`` envelope of
the workspace's ``pulsar-x-update-posts`` tasks. A key is skipped when the
ledger holds it (published, skipped, imported, or any other state), when an
open task lists it, or when a plan file under ``x-updates/`` carries it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date as Date
from pathlib import Path
from typing import Any
from urllib.parse import quote

PLANS = Path("x-updates")
MAX_CANDIDATES = 100  # pulsar.history looks up at most 100 keys
MAX_DRAFTS = 3
KINDS = ("release", "repo", "pr")  # drafting order when over the cap
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_KEY_LINE = re.compile(r"""^key:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


def key_of(candidate: dict[str, Any]) -> str:
    kind, repo = candidate.get("kind"), candidate.get("repo")
    if not isinstance(repo, str) or not _NAME.fullmatch(repo):
        raise ValueError(f"repo must be a repository name without its owner: {repo!r}")
    if kind == "release":
        tag = candidate.get("tag")
        if not isinstance(tag, str) or not tag or any(c.isspace() for c in tag):
            raise ValueError(f"release tag must be a non-empty tag name: {tag!r}")
        return f"release:{repo}:{tag}"
    if kind == "repo":
        return f"repo:{repo}"
    if kind == "pr":
        number = candidate.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError(f"pr number must be a positive integer: {number!r}")
        return f"pr:{repo}:{number}"
    raise ValueError(f"kind must be one of {KINDS}: {kind!r}")


def keys(candidates: list[dict[str, Any]]) -> list[str]:
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates; narrow the scan window")
    return list(dict.fromkeys(key_of(c) for c in candidates))


def drafted(root: Path) -> dict[str, str]:
    """Each key a plan file under ``root`` carries, and that file."""
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.yaml")):
        if path.is_symlink() or not path.is_file():
            continue
        for match in _KEY_LINE.finditer(path.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(match.group(1), path.as_posix())
    return found


def plan(request: dict[str, Any], root: Path = PLANS) -> dict[str, Any]:
    """Return the drafts to write (at most ``max_drafts``), what was skipped and why,
    and what is left for a later run."""
    candidates: list[dict[str, Any]] = request["candidates"]
    history, tasks = request["history"], request["tasks"]
    if history["truncated"] is not False:
        raise ValueError("pulsar.history was truncated; look the keys up in full")
    if tasks["truncated"] is not False or tasks["total"] != len(tasks["tasks"]):
        raise ValueError("a complete task list is required for x-updates dedupe")
    cap = request.get("max_drafts", MAX_DRAFTS)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= MAX_DRAFTS:
        raise ValueError(f"max_drafts must be 0..{MAX_DRAFTS}")
    day = Date.fromisoformat(request.get("date") or Date.today().isoformat()).isoformat()
    keys(candidates)  # every candidate well-formed, and few enough to look up
    ledger = {row["key"]: row for row in history["rows"]}
    files = drafted(root)
    open_tasks = [t for t in tasks["tasks"] if t["terminal"] is False]
    skipped: list[dict[str, Any]] = []
    fresh: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = key_of(candidate)
        if key in seen:
            continue
        seen.add(key)
        if key in ledger:
            row = ledger[key]
            reason = f"in the ledger ({row['state']}, {row['tool']})"
            skipped.append({"key": key, "reason": reason, "url": row.get("url")})
        elif key in files:
            skipped.append({"key": key, "reason": f"already drafted in {files[key]}"})
        elif ids := [t["id"] for t in open_tasks if f"`{key}`" in (t.get("description") or "")]:
            skipped.append({"key": key, "reason": "an open task lists it", "task_ids": ids})
        else:
            fresh.append((key, candidate))
    fresh.sort(key=lambda kc: (KINDS.index(kc[1]["kind"]), str(kc[1].get("at") or "")))
    drafts = [
        {**candidate, "key": key, "plan": (root / day / f"{_slug(key)}.yaml").as_posix()}
        for key, candidate in fresh[:cap]
    ]
    deferred = [key for key, _ in fresh[cap:]]
    return {"drafts": drafts, "skipped": skipped, "deferred": deferred}


def _slug(key: str) -> str:
    """Encode the entire key reversibly, including separators and literal percent signs."""
    return quote(key, safe="")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    request = json.load(sys.stdin)
    if mode == "keys":
        result: dict[str, Any] = {"keys": keys(request["candidates"])}
    elif mode == "plan":
        result = plan(request)
    else:
        raise SystemExit("usage: x_updates.py keys|plan < request.json")
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
