"""How the ``pulsar`` CLI renders a command's payload (STD-01 §R6–§R9, §R14–§R18).

Every command builds one payload; ``json`` prints it as it is, and the human
forms are derived from it through a view: a list of blocks, each a key-value
``Fields`` or a borderless ``Table``. The mode is resolved once per
invocation (``resolve_mode``), and whether to color and how wide the terminal
is are decided once (``resolve_terminal``). Nothing else looks at the TTY or
these environment variables.

- ``table``: for a human on a terminal. Tables have one header row and one
  line per record, ``-`` for an absent cell, right-aligned numbers, and a
  single ``…`` where a value is cut to fit a known width. Semantic colors
  only on a terminal.
- ``plain``: the piped form of ``auto``. A table is one tab-separated line
  per record with no header; fields are ``label: value`` lines. No escapes,
  no truncation.
- ``json``: the payload, one document.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from pulsar.core.jsonx import as_object

FORMAT_ENV = "PULSAR_FORMAT"
FORMATS = ("auto", "table", "json")

Mode = Literal["table", "plain", "json"]
Role = Literal["ok", "warn", "error", "muted", "neutral"]


class ModeConflict(ValueError):
    """``--json`` together with a different ``--format``."""


def resolve_mode(flag: str | None, json_flag: bool, env: Mapping[str, str], isatty: bool) -> Mode:
    """The output mode: explicit flag > ``PULSAR_FORMAT`` > ``auto`` (STD-01 §R8).

    ``--json`` is shorthand for ``--format json``; the two naming different
    modes is a usage error (§R7). An unrecognized environment value is
    ``auto``, not a failure.
    """
    if json_flag and flag not in (None, "json"):
        raise ModeConflict(f"--json conflicts with --format {flag}; pass one of them")
    chosen = "json" if json_flag else flag
    if chosen is None:
        value = env.get(FORMAT_ENV, "").strip().lower()
        chosen = value if value in FORMATS else "auto"
    if chosen == "auto":
        return "table" if isatty else "plain"
    return "json" if chosen == "json" else "table"


def json_requested(argv: Sequence[str], env: Mapping[str, str]) -> bool:
    """Whether this invocation asked for JSON, read from the raw arguments and
    the environment, for a usage error found before parsing is done (§R19)."""
    args = list(argv)
    for i, arg in enumerate(args):
        if arg == "--":
            break
        if arg == "--json" or arg == "--format=json":
            return True
        if arg == "--format":
            return i + 1 < len(args) and args[i + 1] == "json"
        if arg.startswith("--format="):
            return False
    return env.get(FORMAT_ENV, "").strip().lower() == "json"


@dataclass(frozen=True)
class Terminal:
    """Color and width, decided once (STD-01 §R17). ``width`` None: do not truncate."""

    color: bool = False
    width: int | None = None


def resolve_terminal(stream: TextIO, env: Mapping[str, str]) -> Terminal:
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty:
        return Terminal()
    color = not env.get("NO_COLOR") and (
        env.get("TERM") != "dumb" or bool(env.get("CLICOLOR_FORCE"))
    )
    return Terminal(color=color, width=_width(stream, env))


def _width(stream: TextIO, env: Mapping[str, str]) -> int | None:
    columns = env.get("COLUMNS", "")
    if columns.isdigit() and int(columns) > 0:
        return int(columns)
    try:
        return os.get_terminal_size(stream.fileno()).columns or None
    except (AttributeError, ValueError, OSError):
        return None


# -- views ----------------------------------------------------------------------------

Cell = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class Column:
    header: str
    cell: Cell
    number: bool = False
    flex: bool = False  # may be cut with "…" to fit the terminal
    semantic: bool = False  # colored by role_for


@dataclass(frozen=True)
class Table:
    columns: Sequence[Column]
    rows: Sequence[Mapping[str, Any]]
    title: str | None = None


@dataclass(frozen=True)
class Fields:
    pairs: Sequence[tuple[str, Any]]
    title: str | None = None
    semantic: frozenset[str] = field(default_factory=frozenset)


Block = Fields | Table
View = Callable[[Mapping[str, Any]], list[Block]]


def col(
    header: str,
    key: str | Cell,
    *,
    number: bool = False,
    flex: bool = False,
    semantic: bool = False,
) -> Column:
    """A column reading ``key`` (a dotted path) or computing its cell."""
    if isinstance(key, str):
        path = key

        def cell(row: Mapping[str, Any]) -> Any:
            return dig(row, path)

        return Column(header, cell, number=number, flex=flex, semantic=semantic)
    return Column(header, key, number=number, flex=flex, semantic=semantic)


def dig(row: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted ``path``, or None where any step is missing."""
    value: Any = row
    for part in path.split("."):
        found = as_object(value)
        if found is None:
            return None
        value = found.get(part)
    return value


def fields(
    payload: Mapping[str, Any],
    *,
    skip: Sequence[str] = (),
    title: str | None = None,
    semantic: Sequence[str] = ("state", "status", "health", "token_state"),
) -> Fields:
    """Every scalar of ``payload`` as a key-value block, nested objects flattened
    (``ledger from version``); lists of records belong in tables, so ``skip`` them."""
    pairs: list[tuple[str, Any]] = []
    roles: set[str] = set()

    def walk(obj: Mapping[str, Any], prefix: str) -> None:
        for key, value in obj.items():
            if not prefix and key in skip:
                continue
            label = f"{prefix}{key}".replace("_", " ")
            if isinstance(value, Mapping):
                walk(value, f"{prefix}{key} ")  # pyright: ignore[reportUnknownArgumentType]
                continue
            pairs.append((label, value))
            if key in semantic:
                roles.add(label)

    walk(payload, "")
    return Fields(pairs, title=title, semantic=frozenset(roles))


# -- rendering ------------------------------------------------------------------------

ROLES: dict[str, Role] = {
    # One table from domain values to roles (STD-01 §R18); unmapped is neutral.
    "healthy": "ok",
    "published": "ok",
    "active": "ok",
    "valid": "ok",
    "migrated": "ok",
    "yes": "ok",
    "unverified": "warn",
    "unknown": "warn",
    "partial": "warn",
    "pending": "warn",
    "submitting": "warn",
    "expiring": "warn",
    "changed": "warn",
    "unhealthy": "error",
    "failed": "error",
    "expired": "error",
    "revoked": "error",
    "reauth_required": "error",
    "conflict": "error",
    "skipped": "muted",
    "none": "muted",
}
ANSI: dict[Role, str] = {"ok": "32", "warn": "33", "error": "31", "muted": "90"}


def role_for(value: str) -> Role:
    return ROLES.get(value, "neutral")


def paint(text: str, value: str, term: Terminal) -> str:
    code = ANSI.get(role_for(value)) if term.color else None
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def text(value: Any) -> str:
    """One cell or field value as text (types stay in the payload, STD-01 §R11)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple):
        items = [text(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return ", ".join(items) if items else "-"
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).split()) or "-"


def render(blocks: Sequence[Block], mode: Mode, term: Terminal) -> str:
    """The human text of a view: ``table`` or ``plain``. Empty tables render nothing."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Table):
            if not block.rows:
                continue
            lines = _table(block, term) if mode == "table" else _tsv(block)
        else:
            lines = _fields(block, term if mode == "table" else Terminal())
        if mode == "table" and block.title:
            lines = [block.title, *lines]
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n" if parts else ""


def _fields(block: Fields, term: Terminal) -> list[str]:
    if not block.pairs:
        return []
    width = max(len(label) for label, _ in block.pairs) + 1
    lines: list[str] = []
    for label, value in block.pairs:
        shown = text(value)
        if label in block.semantic:
            shown = paint(shown, shown, term)
        lines.append(f"{label + ':':<{width}}  {shown}".rstrip())
    return lines


def _tsv(block: Table) -> list[str]:
    return ["\t".join(text(c.cell(row)) for c in block.columns) for row in block.rows]


def _table(block: Table, term: Terminal) -> list[str]:
    cells = [[text(c.cell(row)) for c in block.columns] for row in block.rows]
    widths = [
        max([len(c.header), *(len(r[i]) for r in cells)]) for i, c in enumerate(block.columns)
    ]
    widths = _fit(block.columns, widths, term.width)
    out = [_line([c.header for c in block.columns], block.columns, widths, term, header=True)]
    out.extend(_line(r, block.columns, widths, term) for r in cells)
    return out


def _fit(columns: Sequence[Column], widths: list[int], width: int | None) -> list[int]:
    """Shrink the flexible columns, rightmost first, until the row fits
    ``width``; with no known width nothing is cut (STD-01 §R15)."""
    if width is None:
        return widths
    widths = list(widths)
    over = sum(widths) + 2 * (len(widths) - 1) - width
    for i in reversed(range(len(columns))):
        if over <= 0:
            break
        if not columns[i].flex:
            continue
        floor = max(len(columns[i].header), 8)
        cut = min(over, widths[i] - floor)
        if cut > 0:
            widths[i] -= cut
            over -= cut
    return widths


def _line(
    cells: Sequence[str],
    columns: Sequence[Column],
    widths: Sequence[int],
    term: Terminal,
    *,
    header: bool = False,
) -> str:
    out: list[str] = []
    last = len(cells) - 1
    for i, (cell, column, width) in enumerate(zip(cells, columns, widths, strict=True)):
        shown = cell if len(cell) <= width else cell[: width - 1] + "…"
        pad = width - len(shown)
        if column.number:
            shown = " " * pad + shown
        elif i != last:
            shown = shown + " " * pad
        if column.semantic and not header:
            shown = paint(shown, cell, term)
        out.append(shown)
    return "  ".join(out).rstrip()
