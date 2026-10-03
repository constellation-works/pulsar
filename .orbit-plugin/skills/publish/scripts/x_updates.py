"""Plan x-updates drafts: which announcements are new, under which idempotency key.

This skill helper reads JSON on stdin and prints JSON on stdout. ``scan`` reads
public GitHub metadata through authenticated gh or unauthenticated HTTPS. It
writes nothing. ``keys`` and ``plan`` stay offline; only ``plan`` reads existing
plan files under ``x-updates/`` in the current directory (the workspace).

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
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from functools import partial
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

PLANS = Path("x-updates")
MAX_CANDIDATES = 100  # editorial lookup/plan only; pulsar.history accepts at most 100 keys
MAX_DRAFTS = 3
KINDS = ("release", "repo", "pr")  # drafting order when over the cap
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_KEY_LINE = re.compile(r"""^key:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


ORG = "constellation-works"
MAX_REQUESTS = 60


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class Fetcher(Protocol):
    def __call__(self, endpoint: str) -> Response: ...


class CommandRunner(Protocol):
    def __call__(self, command: list[str], **kwargs: Any) -> Any: ...


class HttpOpener(Protocol):
    def __call__(self, request: Request, *, timeout: int) -> Any: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # Every request stays on api.github.com, including pagination.


def public_fetch(endpoint: str, *, opener: HttpOpener) -> Response:
    request = Request(
        f"https://api.github.com/{endpoint}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pulsar-x-updates",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=30) as reply:
            return Response(reply.status, dict(reply.headers), json.load(reply))
    except HTTPError as error:
        # Do not echo server error bodies or credentials into the scan report.
        error.close()
        return Response(error.code, dict(error.headers), None)


def gh_fetch(endpoint: str, *, run: CommandRunner) -> Response:
    reply = run(
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "--include",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            endpoint,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    head, separator, body = reply.stdout.replace("\r\n", "\n").partition("\n\n")
    if not separator or not head.startswith("HTTP/"):
        raise OSError(f"gh API command failed (exit {reply.returncode})")
    lines = head.splitlines()
    status_line = lines[0].split()
    if len(status_line) < 2:
        raise ValueError("invalid gh HTTP status line")
    status = int(status_line[1])
    headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
    return Response(status, headers, json.loads(body) if status == 200 else None)


def github_fetcher(run: CommandRunner, opener: HttpOpener) -> tuple[str, Fetcher]:
    try:
        auth = run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if auth.returncode == 0:
            return "gh", partial(gh_fetch, run=run)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "public-rest", partial(public_fetch, opener=opener)


class PartialScan(Exception):
    def __init__(self, code: str, endpoint: str, **details: Any):
        self.error = {"code": code, "endpoint": endpoint, **details}


class Scan:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self.requests = 0
        self.exhausted = False

    def get(self, endpoint: str) -> Response:
        if self.exhausted or self.requests >= MAX_REQUESTS:
            raise PartialScan("rate_limit", endpoint, request_limit=MAX_REQUESTS)
        self.requests += 1
        try:
            reply = self.fetcher(endpoint)
        except (OSError, HTTPException, subprocess.TimeoutExpired) as error:
            raise PartialScan("network_failure", endpoint, reason=type(error).__name__) from error
        except ValueError as error:
            raise PartialScan("invalid_response", endpoint) from error
        headers = {k.lower(): v.strip() for k, v in reply.headers.items()}
        self.exhausted = headers.get("x-ratelimit-remaining") == "0"
        if reply.status in (403, 429) and (
            self.exhausted or reply.status == 429 or "retry-after" in headers
        ):
            raise PartialScan(
                "rate_limit",
                endpoint,
                status=reply.status,
                remaining=headers.get("x-ratelimit-remaining"),
                reset=headers.get("x-ratelimit-reset"),
                retry_after=headers.get("retry-after"),
            )
        if reply.status != 200:
            raise PartialScan("http_failure", endpoint, status=reply.status)
        return Response(reply.status, headers, reply.body)

    def pages(self, path: str, **params: Any):
        page = 1
        collected = 0
        while True:
            endpoint = f"{path}?{urlencode({**params, 'page': page})}"
            reply = self.get(endpoint)
            if path == "search/issues":
                if reply.body["incomplete_results"] or reply.body["total_count"] > 1000:
                    raise PartialScan("incomplete_search", endpoint)
                rows = reply.body["items"]
            else:
                rows = reply.body
            if not isinstance(rows, list):
                raise ValueError("expected a list")
            collected += len(rows)
            yield from rows
            if not re.search(r'rel="next"', reply.headers.get("link", "")):
                if path == "search/issues" and collected < reply.body["total_count"]:
                    raise PartialScan("incomplete_search", endpoint)
                return
            page += 1


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def collect(
    request: dict[str, Any], fetcher: Fetcher, *, source: str, now: datetime
) -> dict[str, Any]:
    """Collect public candidates; the agent applies the unchanged editorial notability rules."""
    end = now.astimezone(UTC).replace(microsecond=0)
    start = timestamp(request["start"]) if "start" in request else end - timedelta(days=7)
    if not end - timedelta(days=7) <= start <= end:
        raise ValueError("scan window must be within the last 7 days")
    scan = Scan(fetcher)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    endpoint = f"orgs/{ORG}/repos"

    def within(value: str | None) -> bool:
        return value is not None and start <= timestamp(value) <= end

    def add(candidate: dict[str, Any]) -> None:
        key = key_of(candidate)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    error = None
    try:
        repos = {}
        for repo in scan.pages(endpoint, type="public", per_page=100):
            name = repo["name"]
            if (
                repo["private"] is not False
                or repo["archived"] is not False
                or repo["full_name"] != f"{ORG}/{name}"
                or not _NAME.fullmatch(name)
            ):
                continue
            repos[name] = repo
            if within(repo["created_at"]):
                add(
                    {
                        "kind": "repo",
                        "repo": name,
                        "title": repo.get("description") or name,
                        "url": repo["html_url"],
                        "at": repo["created_at"],
                    }
                )
        for name, repo in repos.items():
            if not within(repo["pushed_at"]):
                continue
            endpoint = f"repos/{ORG}/{name}/releases"
            for release in scan.pages(endpoint, per_page=10):
                if release["draft"] or release["prerelease"] or not within(release["published_at"]):
                    continue
                add(
                    {
                        "kind": "release",
                        "repo": name,
                        "tag": release["tag_name"],
                        "title": release["name"] or release["tag_name"],
                        "url": release["html_url"],
                        "at": release["published_at"],
                    }
                )
        endpoint = "search/issues"
        query = f"org:{ORG} is:public is:pr is:merged merged:>={start.isoformat()}"
        for pr in scan.pages(endpoint, q=query, per_page=100):
            prefix = f"https://api.github.com/repos/{ORG}/"
            repo_url = pr["repository_url"]
            if not repo_url.startswith(prefix) or repo_url[len(prefix) :] not in repos:
                continue
            name = repo_url[len(prefix) :]
            merged = pr["pull_request"]["merged_at"]
            if within(merged):
                candidate = {
                    "kind": "pr",
                    "repo": name,
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr["html_url"],
                    "at": merged,
                    "labels": [label["name"] for label in pr.get("labels", [])],
                }
                if author := (pr.get("user") or {}).get("login"):
                    candidate["author"] = author
                add(candidate)
        endpoint = f"orgs/{ORG}/events"
        count = 0
        for event in scan.pages(endpoint, per_page=100):
            count += 1
            if timestamp(event["created_at"]) < start:
                break  # Events are newest first; don't spend requests on older pages.
            name = event["repo"]["name"].removeprefix(f"{ORG}/")
            if (
                event["type"] == "PublicEvent"
                and event["public"] is True
                and event["repo"]["name"] == f"{ORG}/{name}"
                and name in repos
                and within(event["created_at"])
            ):
                repo = repos[name]
                add(
                    {
                        "kind": "repo",
                        "repo": name,
                        "title": repo.get("description") or name,
                        "url": repo["html_url"],
                        "at": event["created_at"],
                    }
                )
        else:
            if count >= 300:
                raise PartialScan("event_limit", endpoint, limit=300)
    except PartialScan as partial_scan:
        error = partial_scan.error
    except (KeyError, TypeError, ValueError, AttributeError) as invalid:
        error = {"code": "invalid_response", "endpoint": endpoint, "reason": type(invalid).__name__}
    return {
        "source": source,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "partial": error is not None,
        "error": error,
        "requests": scan.requests,
        "candidates": candidates,
    }


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
        raise ValueError(
            f"at most {MAX_CANDIDATES} candidates; filter notability first, "
            "then select releases and repos before the newest notable PRs"
        )
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
    if mode == "scan":
        source, fetcher = github_fetcher(subprocess.run, build_opener(NoRedirect()).open)
        result = collect(request, fetcher, source=source, now=datetime.now(UTC))
    elif mode == "keys":
        result = {"keys": keys(request["candidates"])}
    elif mode == "plan":
        result = plan(request)
    else:
        raise SystemExit("usage: x_updates.py scan|keys|plan < request.json")
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
