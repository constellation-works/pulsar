"""The human view of each CLI command's payload.

A view only selects and arranges what the payload holds: it never adds a
value, and every record in the payload is a line of some table. Detail
results are key-value fields; lists are tables. ``note`` fields are printed
as stderr notices by the CLI, not here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .render import Block, Table, col, dig, fields

NOTE = "note"


def _count(key: str) -> Any:
    def cell(row: Mapping[str, Any]) -> Any:
        value = row.get(key)
        return len(value) if isinstance(value, list) else None  # pyright: ignore[reportUnknownArgumentType]

    return cell


def _records(payload: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = payload.get(key)
    return value if isinstance(value, list) else []  # pyright: ignore[reportUnknownVariableType]


def auth_status(payload: Mapping[str, Any]) -> list[Block]:
    return [
        Table(
            [
                col("ACCOUNT", "alias"),
                col("HEALTH", "health", semantic=True),
                col("STATUS", "status", semantic=True),
                col("HANDLE", "account.username"),
                col("TOKEN", "token_state", semantic=True),
                col("EXPIRES IN (s)", "access_token_expires_in_s", number=True),
                col("VERIFIED", "verified"),
                col("REASON", "reason", flex=True),
            ],
            _records(payload, "accounts"),
        )
    ]


def status(payload: Mapping[str, Any]) -> list[Block]:
    return [
        Table(
            [
                col("ACCOUNT", "alias"),
                col("POSTS", "posts.used", number=True),
                col("CAP", "posts.cap", number=True),
                col("DAY (USD)", "day.spent_usd", number=True),
                col("DAY BUDGET (USD)", "day.budget_usd", number=True),
                col("MONTH (USD)", "month.spent_usd", number=True),
                col("MONTH BUDGET (USD)", "month.budget_usd", number=True),
                col("QUIET", _quiet),
                col("UNRESOLVED", _count("unresolved"), number=True),
            ],
            _records(payload, "accounts"),
        )
    ]


def _quiet(row: Mapping[str, Any]) -> Any:
    if dig(row, "quiet.active"):
        return f"until {dig(row, 'quiet.until')}"
    return dig(row, "quiet.window")


def history(payload: Mapping[str, Any]) -> list[Block]:
    return [
        Table(
            [
                col("UPDATED", "updated_at"),
                col("ACCOUNT", "account_alias"),
                col("STATE", "state", semantic=True),
                col("TOOL", "tool"),
                col("POSTS", _count("items"), number=True),
                col("URL", "url", flex=True),
                col("KEY", "idempotency_key", flex=True),
            ],
            _records(payload, "writes"),
        )
    ]


def approvals(payload: Mapping[str, Any]) -> list[Block]:
    """``approvals`` lists them; ``approve`` shows the ones it recorded."""
    rows = _records(payload, "approvals")
    return [
        fields(payload, skip=("approvals", NOTE)),
        Table(
            [
                col("ID", "id", number=True),
                col("ACCOUNT", "account"),
                col("STATE", "state", semantic=True),
                col("POSTS", "posts", number=True),
                col("EXPIRES", "expires_at"),
                col("BY", "approved_by"),
                col("DIGEST", "digest"),
                col("SOURCE", "source", flex=True),
            ],
            rows,
        ),
    ]


POST_COLUMNS = [
    col("#", lambda row: row.get("idx"), number=True),
    col("LENGTH", "length", number=True),
    col("MAX", "max_length", number=True),
    col("COST (USD)", "estimated_cost_usd", number=True),
    col("URL", "has_url"),
    col("MEDIA", _count("media"), number=True),
    col("TEXT", "text", flex=True),
]


def plan_report(payload: Mapping[str, Any]) -> list[Block]:
    """``validate`` and ``publish`` without ``--confirm``: per account, its
    summary and one line per post."""
    blocks: list[Block] = [fields(payload, skip=("accounts", NOTE))]
    for account in _records(payload, "accounts"):
        posts = [{"idx": i, **post} for i, post in enumerate(_records(account, "posts"))]
        blocks.append(fields(account, skip=("posts",), title=f"{account.get('account')}"))
        blocks.append(Table(POST_COLUMNS, posts))
    return blocks


def publish(payload: Mapping[str, Any]) -> list[Block]:
    if not payload.get("published"):
        return plan_report(payload)
    results = _records(payload, "results")
    items = [
        {"account": result.get("account"), **item}
        for result in results
        for item in _records(result, "items")
    ]
    return [
        fields(payload, skip=("results", NOTE)),
        Table(
            [
                col("ACCOUNT", "account"),
                col("STATE", "state", semantic=True),
                col("REPLAYED", "replayed"),
                col("POSTS", _count("items"), number=True),
                col("ERROR", "error.message", flex=True),
                col("KEY", "idempotency_key", flex=True),
            ],
            results,
            title="Writes",
        ),
        Table(
            [
                col("ACCOUNT", "account"),
                col("#", "idx", number=True),
                col("STATE", "state", semantic=True),
                col("POST ID", "post_id"),
                col("URL", "url", flex=True),
            ],
            items,
            title="Posts",
        ),
    ]


def reconcile(payload: Mapping[str, Any]) -> list[Block]:
    return [
        fields(payload, skip=("results",)),
        Table(
            [
                col("STATE", "state", semantic=True),
                col("POSTS", _count("items"), number=True),
                col("ERROR", "error.message", flex=True),
                col("NOTE", "note", flex=True),
                col("KEY", "idempotency_key", flex=True),
            ],
            _records(payload, "results"),
            title="Writes",
        ),
    ]


def import_posted(payload: Mapping[str, Any]) -> list[Block]:
    return [
        fields(payload, skip=("conflicts", "errors", NOTE)),
        Table(
            [
                col("LINE", "line", number=True),
                col("KEY", "key"),
                col("REASON", "reason", flex=True),
            ],
            _records(payload, "conflicts"),
            title="Conflicts",
        ),
        Table(
            [col("LINE", "line", number=True), col("REASON", "reason", flex=True)],
            _records(payload, "errors"),
            title="Errors",
        ),
    ]


def detail(payload: Mapping[str, Any]) -> list[Block]:
    """A single result (login, logout, migrate): every field, key-value."""
    return [fields(payload, skip=(NOTE,))]
