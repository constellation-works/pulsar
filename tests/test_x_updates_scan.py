"""Public GitHub collection with injected responses; no test calls the network."""

import io
import json
import runpy
import subprocess
from datetime import UTC, datetime
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

HELPER = Path(__file__).resolve().parents[1] / ".orbit-plugin/skills/publish/scripts/x_updates.py"
scan = SimpleNamespace(**runpy.run_path(str(HELPER)))
NOW = datetime(2026, 10, 3, 10, tzinfo=UTC)
RECENT = "2026-10-02T12:00:00Z"
OLD = "2026-09-20T12:00:00Z"


def repo(name="orbit", **changes):
    return {
        "name": name,
        "full_name": f"constellation-works/{name}",
        "private": False,
        "archived": False,
        "created_at": OLD,
        "pushed_at": RECENT,
        "description": f"About {name}",
        "html_url": f"https://github.com/constellation-works/{name}",
        **changes,
    }


def release(**changes):
    return {
        "tag_name": "v1",
        "name": "Version one",
        "html_url": "https://github.com/constellation-works/orbit/releases/tag/v1",
        "published_at": RECENT,
        "draft": False,
        "prerelease": False,
        **changes,
    }


def pr(name="orbit", **changes):
    return {
        "repository_url": f"https://api.github.com/repos/constellation-works/{name}",
        "pull_request": {"merged_at": RECENT},
        "number": 41,
        "title": "Faster drains",
        "html_url": f"https://github.com/constellation-works/{name}/pull/41",
        **changes,
    }


def event(name="orbit", **changes):
    return {
        "type": "PublicEvent",
        "repo": {"name": f"constellation-works/{name}"},
        "public": True,
        "created_at": RECENT,
        **changes,
    }


def response(body, **headers):
    return scan.Response(200, headers, body)


class FakeGitHub:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def collect(fetcher, request=None):
    return scan.collect(request or {}, fetcher, source="public-rest", now=NOW)


def test_collect_preserves_public_candidate_shapes_keys_and_window():
    fetcher = FakeGitHub(
        response(
            [
                repo(),
                repo("nebula", created_at=RECENT, pushed_at=OLD),
                repo("private", private=True),
                repo("archived", archived=True),
                repo("foreign", full_name="other/foreign"),
                repo("bad/name"),
            ]
        ),
        response(
            [
                release(),
                release(draft=True),
                release(prerelease=True),
                release(published_at=OLD),
                release(published_at="2026-10-04T00:00:00Z"),
            ]
        ),
        response(
            {
                "items": [
                    pr(
                        labels=[{"name": "feature"}, {"name": "user-facing"}], user={"login": "dev"}
                    ),
                    pr("private"),
                    pr("archived"),
                    pr("foreign"),
                    pr(pull_request={"merged_at": OLD}),
                ],
                "total_count": 5,
                "incomplete_results": False,
            }
        ),
        response(
            [
                event(),
                event("nebula"),
                event("private"),
                event(public=False),
                event(repo={"name": "other/orbit"}),
                event(created_at=OLD),
            ]
        ),
    )
    out = collect(fetcher)
    assert out["partial"] is False and out["error"] is None
    assert out["window"] == {"start": "2026-09-26T10:00:00+00:00", "end": NOW.isoformat()}
    assert out["requests"] == 4 and not fetcher.replies
    assert scan.keys(out["candidates"]) == [
        "repo:nebula",
        "release:orbit:v1",
        "pr:orbit:41",
        "repo:orbit",
    ]
    for candidate in out["candidates"]:
        fields = {"kind", "repo", "title", "url", "at"}
        if candidate["kind"] == "release":
            fields.add("tag")
        elif candidate["kind"] == "pr":
            fields.update({"number", "labels", "author"})
            assert candidate["labels"] == ["feature", "user-facing"]
            assert candidate["author"] == "dev"
        assert set(candidate) == fields
    paths = [urlsplit(path).path for path in fetcher.calls]
    assert paths == [
        "orgs/constellation-works/repos",
        "repos/constellation-works/orbit/releases",
        "search/issues",
        "orgs/constellation-works/events",
    ]
    query = parse_qs(urlsplit(fetcher.calls[2]).query)["q"][0]
    assert "is:public" in query and "org:constellation-works" in query
    assert "merged:>=2026-09-26T10:00:00+00:00" in query


def test_empty_complete_scan_is_distinct_from_partial():
    fetcher = FakeGitHub(
        response([]),
        response({"items": [], "total_count": 0, "incomplete_results": False}),
        response([]),
    )
    assert collect(fetcher)["partial"] is False
    failed = collect(FakeGitHub(URLError("offline")))
    assert failed["partial"] is True and failed["candidates"] == []
    assert failed["error"]["code"] == "network_failure"


@pytest.mark.parametrize(
    "failure",
    [
        scan.Response(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"}, None),
        scan.Response(429, {"x-ratelimit-remaining": "0"}, None),
        scan.Response(429, {"Retry-After": "60"}, None),
        scan.Response(403, {"Retry-After": "60"}, None),
        scan.Response(500, {}, None),
        URLError("offline"),
        TimeoutError("timeout"),
        IncompleteRead(b"interrupted"),
        BadStatusLine("invalid HTTP"),
        subprocess.TimeoutExpired("gh", 30),
        ValueError("invalid JSON"),
    ],
)
def test_failure_stops_and_retains_partial_candidates(failure):
    fetcher = FakeGitHub(response([repo(created_at=RECENT)]), failure)
    out = collect(fetcher)
    assert out["partial"] is True and out["error"]["endpoint"].startswith("repos/")
    assert scan.keys(out["candidates"]) == ["repo:orbit"]
    assert out["requests"] == 2 and len(fetcher.calls) == 2
    if isinstance(failure, scan.Response):
        assert out["error"]["code"] == (
            "rate_limit" if failure.status in (403, 429) else "http_failure"
        )
        assert out["error"]["status"] == failure.status


def test_exhausted_success_response_stops_before_next_request():
    fetcher = FakeGitHub(response([repo(created_at=RECENT)], **{"X-RateLimit-Remaining": "0"}))
    out = collect(fetcher)
    assert out["partial"] is True and out["error"]["code"] == "rate_limit"
    assert out["requests"] == 1


def test_request_budget_is_never_exceeded():
    fetcher = FakeGitHub(
        *[response([], Link='<https://other.invalid/>; rel="next"') for _ in range(60)]
    )
    out = collect(fetcher)
    assert out["partial"] is True and out["error"]["code"] == "rate_limit"
    assert out["requests"] == len(fetcher.calls) == 60
    assert all(path.startswith("orgs/constellation-works/repos?") for path in fetcher.calls)
    assert parse_qs(urlsplit(fetcher.calls[-1]).query)["page"] == ["60"]


def test_repo_and_release_pagination_uses_owned_paths():
    fetcher = FakeGitHub(
        response([repo()], Link='<https://untrusted.invalid>; rel="next"'),
        response([repo("idle", pushed_at=OLD)]),
        response([release(prerelease=True)], Link='<https://untrusted.invalid>; rel="next"'),
        response([release()]),
        response({"items": [], "total_count": 0, "incomplete_results": False}),
        response([event(created_at=OLD)], Link='<https://untrusted.invalid>; rel="next"'),
    )
    out = collect(fetcher)
    assert out["partial"] is False and scan.keys(out["candidates"]) == ["release:orbit:v1"]
    assert len(fetcher.calls) == 6
    assert parse_qs(urlsplit(fetcher.calls[1]).query)["page"] == ["2"]
    assert parse_qs(urlsplit(fetcher.calls[3]).query)["page"] == ["2"]


@pytest.mark.parametrize(
    "body",
    [
        {"items": [], "total_count": 0, "incomplete_results": True},
        {"items": [], "total_count": 1001, "incomplete_results": False},
        {"items": [], "total_count": 1, "incomplete_results": False},
        {"unexpected": "shape"},
    ],
)
def test_search_incomplete_or_malformed_is_partial(body):
    out = collect(FakeGitHub(response([]), response(body)))
    assert out["partial"] is True and out["requests"] == 2


def test_event_ceiling_is_partial():
    fetcher = FakeGitHub(
        response([repo(pushed_at=OLD)]),
        response(
            {
                "items": [],
                "total_count": 0,
                "incomplete_results": False,
            }
        ),
        response([event()] * 100, Link='<x>; rel="next"'),
        response([event()] * 100, Link='<x>; rel="next"'),
        response([event()] * 100),
    )
    out = collect(fetcher)
    assert out["partial"] is True and out["error"]["code"] == "event_limit"
    assert scan.keys(out["candidates"]) == ["repo:orbit"]


@pytest.mark.parametrize("start", ["2026-09-25T00:00:00Z", "2026-10-04T00:00:00Z", "2026-10-02"])
def test_invalid_window_makes_no_request(start):
    fetcher = FakeGitHub()
    with pytest.raises(ValueError):
        collect(fetcher, {"start": start})
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "auth",
    [
        0,
        4,
        FileNotFoundError("no gh"),
        PermissionError("denied"),
        subprocess.TimeoutExpired("gh auth status", 30),
    ],
)
def test_authentication_selects_transport_without_exposing_output(auth):
    commands = []
    requests = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        if command[1] == "auth":
            assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
            if isinstance(auth, Exception):
                raise auth
            return subprocess.CompletedProcess(command, auth)
        assert "--method" in command and "GET" in command and "--include" in command
        return subprocess.CompletedProcess(
            command, 0, "HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 59\r\n\r\n[]"
        )

    def opener(request, **kwargs):
        requests.append(request)
        reply = io.BytesIO(b"[]")
        reply.status, reply.headers = 200, {"X-RateLimit-Remaining": "59"}
        assert kwargs == {"timeout": 30}
        return reply

    source, fetcher = scan.github_fetcher(runner, opener)
    reply = fetcher("orgs/constellation-works/repos?type=public&per_page=100")
    assert reply.status == 200 and reply.body == []
    assert commands[0][0] == ["gh", "auth", "status", "--hostname", "github.com"]
    if auth == 0:
        assert source == "gh" and len(commands) == 2 and requests == []
    else:
        assert source == "public-rest" and len(commands) == 1 and len(requests) == 1
        assert requests[0].full_url.startswith("https://api.github.com/orgs/constellation-works/")
        assert requests[0].get_method() == "GET"
        assert not any("auth" in header.lower() for header in requests[0].headers)


def test_urllib_http_error_headers_reach_partial_report():
    def opener(request, **kwargs):
        raise HTTPError(
            request.full_url,
            403,
            "rate limit",
            {"X-RateLimit-Remaining": "0"},
            io.BytesIO(b"untrusted error body"),
        )

    out = collect(lambda endpoint: scan.public_fetch(endpoint, opener=opener))
    assert out["partial"] is True and out["error"]["code"] == "rate_limit"
    assert "untrusted" not in json.dumps(out)


def test_gh_nonzero_rate_limit_preserves_http_status():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            'HTTP/2.0 403 Forbidden\nX-RateLimit-Remaining: 0\n\n{"message":"rate limited"}',
        )

    out = collect(lambda endpoint: scan.gh_fetch(endpoint, run=runner))
    assert out["partial"] is True and out["error"]["code"] == "rate_limit"


def test_redirects_are_not_followed():
    assert (
        scan.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other/")
        is None
    )


def test_busy_week_scan_completes_before_editorial_filtering():
    prs = [pr(number=number) for number in range(1, 151)]
    fetcher = FakeGitHub(
        response([repo(created_at=RECENT)]),
        response([release(), release(tag_name="v2")]),
        response(
            {"items": prs[:100], "total_count": 150, "incomplete_results": False},
            Link='<https://untrusted.invalid>; rel="next"',
        ),
        response({"items": prs[100:], "total_count": 150, "incomplete_results": False}),
        response([event("orbit"), event(created_at=OLD)]),
    )
    out = collect(fetcher)
    assert out["partial"] is False and out["error"] is None
    assert out["window"] == {"start": "2026-09-26T10:00:00+00:00", "end": NOW.isoformat()}
    assert out["requests"] == len(fetcher.calls) == 5 < scan.MAX_REQUESTS
    assert not fetcher.replies
    assert scan.keys(out["candidates"][:3]) == [
        "repo:orbit",
        "release:orbit:v1",
        "release:orbit:v2",
    ]
    candidates = [c for c in out["candidates"] if c["kind"] == "pr"]
    assert len(out["candidates"]) == 153 and len(candidates) == 150
    assert [c["number"] for c in candidates] == list(range(1, 151))
    assert all(c["labels"] == [] and "author" not in c for c in candidates)
    assert urlsplit(fetcher.calls[3]).path == "search/issues"
    assert parse_qs(urlsplit(fetcher.calls[3]).query)["page"] == ["2"]


@pytest.mark.parametrize("mode", ["keys", "plan"])
def test_lookup_accepts_100_candidates_and_refuses_more(mode, tmp_path):
    candidates = [{"kind": "pr", "repo": "orbit", "number": n} for n in range(1, 102)]

    def lookup(selected):
        if mode == "keys":
            return scan.keys(selected)
        return scan.plan(
            {
                "candidates": selected,
                "history": {"rows": [], "truncated": False},
                "tasks": {"tasks": [], "total": 0, "truncated": False},
                "date": "2026-10-03",
            },
            root=tmp_path,
        )

    result = lookup(candidates[:100])
    if mode == "keys":
        assert len(result) == 100
    else:
        assert len(result["drafts"]) == 3 and len(result["deferred"]) == 97
    with pytest.raises(ValueError, match="at most 100 candidates; filter notability first"):
        lookup(candidates)


def test_scan_entrypoint_prints_partial_json_and_writes_nothing(monkeypatch, capsys, tmp_path):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 4)

    def opener(request, **kwargs):
        raise URLError("offline")

    class Opener:
        open = staticmethod(opener)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    monkeypatch.setattr("sys.argv", [str(HELPER), "scan"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    runpy.run_path(str(HELPER), run_name="__main__")
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "public-rest" and out["partial"] is True
    assert out["error"]["code"] == "network_failure" and out["candidates"] == []
    assert list(tmp_path.iterdir()) == []
