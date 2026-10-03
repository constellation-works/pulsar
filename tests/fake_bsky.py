"""A scripted Bluesky PDS (and the AppView reads it proxies), for tests: no network.

It keeps the account's repository (post records by rkey), the blobs uploaded
to it, other people's posts, the handles it can resolve and the account's
notifications, and records every request. It checks what a real PDS would
refuse: a record for another repository, facets outside the text, a reply
without root and parent refs, an embed naming a blob it never stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from .conftest import ACCESS, ROTATED_ACCESS, ROTATED_REFRESH

DID = "did:plc:constworks7x2kq4mv3a5bmrn"
HANDLE = "constworks.bsky.social"
ALICE_DID = "did:plc:alice5k2rlq4vymdyb3oh6ng"
ALICE = "alice.bsky.social"
COLLECTION = "app.bsky.feed.post"


def cid_of(value: object) -> str:
    blob = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    return "bafyrei" + hashlib.sha256(blob).hexdigest()[:40]


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _error(status: int, name: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": name, "message": message or name})


@dataclass
class FakeBsky:
    did: str = DID
    handle: str = HANDLE
    # rkey -> {"uri", "cid", "value"}, in creation order
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # other accounts' posts, by URI: {"uri", "cid", "author", "record", counts...}
    others: dict[str, dict[str, Any]] = field(default_factory=dict)
    handles: dict[str, str] = field(default_factory=lambda: {ALICE: ALICE_DID})
    notifications: list[dict[str, Any]] = field(default_factory=list)  # newest first
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # by URI
    reposts: list[dict[str, Any]] = field(default_factory=list)  # feed items with a reason
    requests: list[httpx.Request] = field(default_factory=list)
    next_rkey: int = 1000
    fail_auth_once: bool = False
    refresh_status: int = 200
    create_status: int | None = None
    # the next createRecord makes the record, then the connection drops
    create_raise_after_accept: type[httpx.TransportError] | None = None
    # DPoP: require proofs, and a nonce the client must learn from a 401 first
    dpop_nonce: str | None = None
    page_size: int | None = None  # caps every listing's page

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_request)

    def calls(self, nsid: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/xrpc/{nsid}"]

    def bodies(self, nsid: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.calls(nsid)]

    def posts(self) -> list[dict[str, Any]]:
        """The post records in the repository, oldest first."""
        return [r["value"] for r in self.records.values()]

    def uri(self, rkey: str) -> str:
        return f"at://{self.did}/{COLLECTION}/{rkey}"

    # -- seeding ------------------------------------------------------------------

    def add_other(self, rkey: str, text: str, *, did: str = ALICE_DID, handle: str = ALICE,
                  created_at: str = "2026-09-26T10:00:00.000Z",
                  reply: dict[str, Any] | None = None) -> str:  # fmt: skip
        uri = f"at://{did}/{COLLECTION}/{rkey}"
        record: dict[str, Any] = {"$type": COLLECTION, "text": text, "createdAt": created_at}
        if reply is not None:
            record["reply"] = reply
        self.others[uri] = {
            "uri": uri,
            "cid": cid_of(record),
            "author": {"did": did, "handle": handle},
            "record": record,
            "indexedAt": created_at,
        }
        return uri

    def mention(self, rkey: str, text: str, *, reason: str = "mention",
                created_at: str = "2026-09-26T10:00:00.000Z",
                reply: dict[str, Any] | None = None) -> str:  # fmt: skip
        """Alice's post naming the account, as a notification (newest first)."""
        uri = self.add_other(rkey, text, created_at=created_at, reply=reply)
        view = self.others[uri]
        self.notifications.insert(
            0,
            {
                "uri": uri,
                "cid": view["cid"],
                "author": view["author"],
                "reason": reason,
                "record": view["record"],
                "isRead": False,
                "indexedAt": created_at,
            },
        )
        return uri

    # -- the server -----------------------------------------------------------------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            return self._token(request)
        refused = self._authorize(request)
        if refused is not None:
            return refused
        nsid = request.url.path.removeprefix("/xrpc/")
        params = {k: v for k, v in parse_qs(request.url.query.decode()).items()}
        if request.method == "GET":
            handler = getattr(self, "q_" + nsid.replace(".", "_"), None)
            if handler is None:
                return _error(400, "MethodNotImplemented")
            return handler(params)
        if nsid == "com.atproto.repo.uploadBlob":
            return self.upload_blob(request)
        body = json.loads(request.content)
        if nsid == "com.atproto.repo.createRecord":
            return self.create_record(request, body)
        if nsid == "com.atproto.repo.deleteRecord":
            return self.delete_record(body)
        return _error(400, "MethodNotImplemented")

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.dpop_nonce is not None:
            proof = request.headers.get("DPoP")
            if proof is None or jwt_claims(proof).get("nonce") != self.dpop_nonce:
                return httpx.Response(
                    400, json={"error": "use_dpop_nonce"}, headers={"DPoP-Nonce": self.dpop_nonce}
                )
        if self.refresh_status != 200:
            return httpx.Response(self.refresh_status, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": ROTATED_ACCESS,
                "refresh_token": ROTATED_REFRESH,
                "token_type": "DPoP" if self.dpop_nonce is not None else "Bearer",
                "expires_in": 3600,
                "scope": "atproto transition:generic",
                "sub": self.did,
            },
        )

    def _authorize(self, request: httpx.Request) -> httpx.Response | None:
        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if self.dpop_nonce is not None:
            proof = request.headers.get("DPoP")
            if scheme != "DPoP" or proof is None:
                return _error(401, "InvalidToken", "DPoP-bound token sent without a proof")
            if jwt_claims(proof).get("nonce") != self.dpop_nonce:
                return httpx.Response(
                    401,
                    json={"error": "use_dpop_nonce"},
                    headers={
                        "WWW-Authenticate": 'DPoP error="use_dpop_nonce"',
                        "DPoP-Nonce": self.dpop_nonce,
                    },
                )
        elif scheme != "Bearer":
            return _error(401, "InvalidToken")
        if self.fail_auth_once and token == ACCESS:
            self.fail_auth_once = False
            return _error(400, "ExpiredToken", "Token has expired")
        if token not in (ACCESS, ROTATED_ACCESS):
            return _error(401, "InvalidToken")
        return None

    def _view(self, uri: str) -> dict[str, Any] | None:
        if uri in self.others:
            view = dict(self.others[uri])
        else:
            rkey = uri.rsplit("/", 1)[-1]
            rec = self.records.get(rkey)
            if rec is None or rec["uri"] != uri:
                return None
            view = {
                "uri": uri,
                "cid": rec["cid"],
                "author": {"did": self.did, "handle": self.handle},
                "record": rec["value"],
                "indexedAt": rec["value"]["createdAt"],
            }
        counts = {"likeCount": 0, "replyCount": 0, "repostCount": 0, "quoteCount": 0}
        return {**view, **counts, **self.counts.get(uri, {})}

    def _page(self, items: list[Any], params: dict[str, list[str]]) -> tuple[list[Any], str | None]:
        limit = int(params.get("limit", ["50"])[0])
        if self.page_size is not None:
            limit = min(limit, self.page_size)
        start = int(params.get("cursor", ["0"])[0])
        chunk = items[start : start + limit]
        more = len(items) > start + limit
        return chunk, str(start + limit) if more else None

    def q_com_atproto_server_getSession(self, _params: dict[str, list[str]]) -> httpx.Response:
        return httpx.Response(200, json={"did": self.did, "handle": self.handle, "active": True})

    def q_com_atproto_identity_resolveHandle(self, params: dict[str, list[str]]) -> httpx.Response:
        did = self.handles.get(params["handle"][0])
        if did is None:
            return _error(400, "InvalidRequest", "Unable to resolve handle")
        return httpx.Response(200, json={"did": did})

    def q_com_atproto_repo_getRecord(self, params: dict[str, list[str]]) -> httpx.Response:
        if params["repo"][0] != self.did:
            return _error(400, "InvalidRequest", "Could not find repo")
        rec = self.records.get(params["rkey"][0])
        if rec is None:
            return _error(400, "RecordNotFound", "Could not locate record")
        return httpx.Response(200, json=rec)

    def q_com_atproto_repo_listRecords(self, params: dict[str, list[str]]) -> httpx.Response:
        newest_first = list(reversed(self.records.values()))
        chunk, cursor = self._page(newest_first, params)
        return httpx.Response(
            200, json={"records": chunk, **({"cursor": cursor} if cursor else {})}
        )

    def q_app_bsky_feed_getPosts(self, params: dict[str, list[str]]) -> httpx.Response:
        uris = params.get("uris", [])
        if len(uris) > 25:
            return _error(400, "InvalidRequest", "too many uris")
        views = [v for u in uris if (v := self._view(u)) is not None]
        return httpx.Response(200, json={"posts": views})

    def q_app_bsky_notification_listNotifications(
        self, params: dict[str, list[str]]
    ) -> httpx.Response:
        reasons = params.get("reasons")
        items = [n for n in self.notifications if reasons is None or n["reason"] in reasons]
        chunk, cursor = self._page(items, params)
        body = {"notifications": chunk, **({"cursor": cursor} if cursor else {})}
        return httpx.Response(200, json=body)

    def q_app_bsky_feed_getAuthorFeed(self, params: dict[str, list[str]]) -> httpx.Response:
        own = [{"post": self._view(r["uri"])} for r in reversed(self.records.values())]
        chunk, cursor = self._page(self.reposts + own, params)
        return httpx.Response(200, json={"feed": chunk, **({"cursor": cursor} if cursor else {})})

    def upload_blob(self, request: httpx.Request) -> httpx.Response:
        data = request.content
        cid = "bafkrei" + hashlib.sha256(data).hexdigest()[:40]
        blob = {
            "$type": "blob",
            "ref": {"$link": cid},
            "mimeType": request.headers["Content-Type"],
            "size": len(data),
        }
        self.blobs[cid] = blob
        return httpx.Response(200, json={"blob": blob})

    def _check_record(self, record: dict[str, Any]) -> str | None:
        """What a PDS would refuse in a post record, or None."""
        size = len(record["text"].encode("utf-8"))
        for facet in record.get("facets", []):
            index = facet["index"]
            if not 0 <= index["byteStart"] < index["byteEnd"] <= size:
                return "facet outside the text"
        reply = record.get("reply")
        if reply is not None and not all(
            isinstance(reply.get(k), dict) and {"uri", "cid"} <= set(reply[k])
            for k in ("root", "parent")
        ):
            return "reply needs root and parent strong refs"
        embed = record.get("embed")
        media = embed.get("media", embed) if embed else None
        blobs = []
        if media and media["$type"] == "app.bsky.embed.images":
            blobs = [i["image"] for i in media["images"]]
            if any("alt" not in i for i in media["images"]):
                return "image without alt"
        elif media and media["$type"] == "app.bsky.embed.video":
            blobs = [media["video"]]
        if any(b["ref"]["$link"] not in self.blobs for b in blobs):
            return "embed names a blob that was never uploaded"
        return None

    def create_record(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["repo"] != self.did or body["collection"] != COLLECTION:
            return _error(400, "InvalidRequest", "not this repository")
        if self.create_status is not None:
            return _error(self.create_status, "InternalServerError")
        problem = self._check_record(body["record"])
        if problem is not None:
            return _error(400, "InvalidRequest", problem)
        self.next_rkey += 1
        rkey = f"3mabc{self.next_rkey}"
        uri = self.uri(rkey)
        cid = cid_of(body["record"])
        self.records[rkey] = {"uri": uri, "cid": cid, "value": body["record"]}
        if self.create_raise_after_accept is not None:
            error, self.create_raise_after_accept = self.create_raise_after_accept, None
            raise error("simulated transport failure", request=request)
        return httpx.Response(200, json={"uri": uri, "cid": cid, "validationStatus": "valid"})

    def delete_record(self, body: dict[str, Any]) -> httpx.Response:
        if body["repo"] != self.did:
            return _error(400, "InvalidRequest", "not this repository")
        self.records.pop(body["rkey"], None)  # deleting a missing record succeeds
        return httpx.Response(200, json={})
