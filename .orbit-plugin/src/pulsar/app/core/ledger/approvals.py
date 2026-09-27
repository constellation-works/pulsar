"""Approvals: a human's yes to one plan digest for one account.

An approval is recorded by a human from a terminal and is in force until it
expires, is revoked, or is used. It is single-use: the first write that
claims under it records its key there (``used_key``), and from then on it
admits only that write again (a retry, a resumed thread), never a second
post of the same content under another key.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from pulsar.internal.errors import APPROVAL_REQUIRED, INVALID_ARGUMENT, PulsarError

from . import text


@dataclass(frozen=True)
class ApprovalRecord:
    id: int
    account_alias: str
    digest: str
    approved_by: str
    source: str | None
    posts: int
    est_cost_usd: float
    created_at: str
    expires_at: str
    revoked_at: str | None
    used_key: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ApprovalRecord:
        return cls(
            id=int(row["id"]),
            account_alias=str(row["account_alias"]),
            digest=str(row["plan_digest"]),
            approved_by=str(row["approved_by"]),
            source=row["source"],
            posts=int(row["posts"]),
            est_cost_usd=float(row["est_cost_usd"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            revoked_at=row["revoked_at"],
            used_key=row["used_key"],
        )

    def state(self, now: str) -> str:
        """``revoked``, ``used``, ``expired`` or ``active``, in that order."""
        if self.revoked_at is not None:
            return "revoked"
        if self.used_key is not None:
            return "used"
        if self.expires_at <= now:
            return "expired"
        return "active"

    def admits(self, key: str, now: str) -> bool:
        """Whether a claim of ``key`` may publish under this approval now."""
        if self.revoked_at is not None or self.expires_at <= now:
            return False
        return self.used_key is None or self.used_key == key

    def to_dict(self, now: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account_alias,
            "digest": self.digest,
            "state": self.state(now),
            "approved_by": self.approved_by,
            "source": self.source,
            "posts": self.posts,
            "estimated_cost_usd": self.est_cost_usd,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "used_key": self.used_key,
        }


def record(
    conn: sqlite3.Connection,
    now: str,
    *,
    account_alias: str,
    digest: str,
    approved_by: str,
    source: str | None,
    posts: int,
    est_cost_usd: float,
    expires_at: str,
) -> ApprovalRecord:
    cur = conn.execute(
        "INSERT INTO approvals (account_alias, plan_digest, approved_by, source, posts,"
        " est_cost_usd, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (account_alias, digest, text.persisted_text(approved_by), text.persisted_text(source),
         posts, float(est_cost_usd), now, expires_at),
    )  # fmt: skip
    return get(conn, int(cur.lastrowid or 0))


def get(conn: sqlite3.Connection, approval_id: int) -> ApprovalRecord:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise PulsarError(
            INVALID_ARGUMENT, f"no approval {approval_id}", detail={"id": approval_id}
        )
    return ApprovalRecord.from_row(row)


def for_digest(conn: sqlite3.Connection, account_alias: str, digest: str) -> list[ApprovalRecord]:
    """Every approval of ``digest`` for the account, newest first."""
    rows = conn.execute(
        "SELECT * FROM approvals WHERE account_alias = ? AND plan_digest = ? ORDER BY id DESC",
        (account_alias, digest),
    ).fetchall()
    return [ApprovalRecord.from_row(r) for r in rows]


def consume(
    conn: sqlite3.Connection, now: str, *, account_alias: str, digest: str, key: str
) -> ApprovalRecord:
    """The approval a claim of ``key`` publishes under, marked used by it, or
    ``approval_required`` naming why there is none."""
    found = for_digest(conn, account_alias, digest)
    chosen = next((a for a in found if a.used_key == key and a.admits(key, now)), None)
    chosen = chosen or next((a for a in found if a.admits(key, now)), None)
    if chosen is None:
        raise missing(account_alias, digest, found[0].state(now) if found else None)
    if chosen.used_key is None:
        conn.execute("UPDATE approvals SET used_key = ? WHERE id = ?", (key, chosen.id))
    return get(conn, chosen.id)


def missing(
    account_alias: str, digest: str, last_state: str | None, *, command: str | None = None
) -> PulsarError:
    """``approval_required``, naming why and the command a human runs
    (``command``, else a generic ``pulsar approve``)."""
    command = command or f"pulsar approve <plan> --account {account_alias}"
    why = {
        None: "none was recorded",
        "revoked": "the last one was revoked",
        "used": "the last one was used by another write",
        "expired": "the last one expired",
    }.get(last_state, "none is in force")
    return PulsarError(
        APPROVAL_REQUIRED,
        f"{account_alias}: publishing this plan needs a human approval of its digest "
        f"{digest[:12]}, and {why}; a human reads the plan and runs `{command}` at a terminal",
        detail={
            "account": account_alias,
            "digest": digest,
            "last_approval": last_state,
            "command": command,
        },
        retryable=False,
    )


def listing(
    conn: sqlite3.Connection, *, account_alias: str | None, limit: int
) -> list[ApprovalRecord]:
    if account_alias is None:
        rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE account_alias = ? ORDER BY id DESC LIMIT ?",
            (account_alias, limit),
        )
    return [ApprovalRecord.from_row(r) for r in rows.fetchall()]


def revoke(conn: sqlite3.Connection, now: str, approval_id: int) -> ApprovalRecord:
    """Revoke an approval; revoking one twice keeps the first time."""
    get(conn, approval_id)
    conn.execute(
        "UPDATE approvals SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
        (now, approval_id),
    )
    return get(conn, approval_id)
