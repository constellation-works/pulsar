"""The CLI's rendering layer: mode resolution, the terminal decision, and the
table, piped and key-value forms."""

import io
import re

import pytest

from pulsar.cli.toolkit.render import (
    Fields,
    ModeConflict,
    Table,
    Terminal,
    col,
    fields,
    json_requested,
    render,
    resolve_mode,
    resolve_terminal,
)

ESC = re.compile(r"\x1b\[[0-9;]*m")


class Tty(io.StringIO):
    """A stream that claims to be a terminal and has no real size."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise io.UnsupportedOperation("no descriptor")


# -- mode -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "json_flag", "env", "tty", "mode"),
    [
        (None, False, {}, True, "table"),
        (None, False, {}, False, "plain"),
        (None, True, {}, True, "json"),
        ("json", True, {}, True, "json"),
        ("table", False, {}, False, "table"),
        ("auto", False, {"PULSAR_FORMAT": "json"}, True, "table"),
        (None, False, {"PULSAR_FORMAT": "json"}, True, "json"),
        (None, False, {"PULSAR_FORMAT": " JSON "}, False, "json"),
        (None, False, {"PULSAR_FORMAT": "yaml"}, False, "plain"),
        (None, False, {"PULSAR_FORMAT": "table"}, False, "table"),
    ],
)
def test_the_mode_is_flag_then_environment_then_auto(flag, json_flag, env, tty, mode):
    assert resolve_mode(flag, json_flag, env, tty) == mode


@pytest.mark.parametrize("flag", ["table", "auto"])
def test_json_with_another_format_is_refused(flag):
    with pytest.raises(ModeConflict, match=f"--json conflicts with --format {flag}"):
        resolve_mode(flag, True, {}, True)


@pytest.mark.parametrize(
    ("argv", "env", "wanted"),
    [
        (["status", "--json"], {}, True),
        (["--format", "json", "status"], {}, True),
        (["status", "--format=json"], {}, True),
        (["--format", "table", "status"], {"PULSAR_FORMAT": "json"}, False),
        (["status"], {"PULSAR_FORMAT": "json"}, True),
        (["status"], {}, False),
        (["publish", "--", "--json"], {}, False),
    ],
)
def test_a_usage_error_knows_whether_json_was_asked_for(argv, env, wanted):
    assert json_requested(argv, env) is wanted


# -- terminal -------------------------------------------------------------------


def test_a_pipe_gets_no_color_and_no_width():
    assert resolve_terminal(io.StringIO(), {"COLUMNS": "80", "CLICOLOR_FORCE": "1"}) == Terminal()


@pytest.mark.parametrize(
    ("env", "color"),
    [
        ({}, True),
        ({"NO_COLOR": "1"}, False),
        ({"NO_COLOR": ""}, True),
        ({"TERM": "dumb"}, False),
        ({"TERM": "dumb", "CLICOLOR_FORCE": "1"}, True),
        ({"NO_COLOR": "1", "CLICOLOR_FORCE": "1"}, False),
    ],
)
def test_color_on_a_terminal_follows_the_conventions(env, color):
    assert resolve_terminal(Tty(), env).color is color


def test_the_width_is_columns_else_unknown():
    assert resolve_terminal(Tty(), {"COLUMNS": "72"}).width == 72
    assert resolve_terminal(Tty(), {"COLUMNS": "wide"}).width is None
    assert resolve_terminal(Tty(), {}).width is None


# -- rendering --------------------------------------------------

ROWS = [
    {"name": "x:constworks", "state": "published", "count": 3, "text": "Orbit v0.26 is out"},
    {"name": "x:other", "state": "failed", "count": 12, "text": None},
]
TABLE = Table(
    [
        col("ACCOUNT", "name"),
        col("STATE", "state", semantic=True),
        col("POSTS", "count", number=True),
        col("TEXT", "text", flex=True),
    ],
    ROWS,
)


def test_a_table_is_borderless_one_line_per_record():
    out = render([TABLE], "table", Terminal())
    assert out == (
        "ACCOUNT       STATE      POSTS  TEXT\n"
        "x:constworks  published      3  Orbit v0.26 is out\n"
        "x:other       failed        12  -\n"
    )


def test_the_piped_form_is_tab_separated_without_header():
    out = render([TABLE], "plain", Terminal(color=True, width=10))
    assert out == "x:constworks\tpublished\t3\tOrbit v0.26 is out\nx:other\tfailed\t12\t-\n"


def test_only_a_known_width_truncates_and_only_flexible_columns():
    narrow = render([TABLE], "table", Terminal(width=40)).splitlines()
    assert narrow[1] == "x:constworks  published      3  Orbit v…"
    assert all(len(line) <= 40 for line in narrow)
    # Too narrow even then: identifiers are never cut, the line runs long instead.
    assert render([TABLE], "table", Terminal(width=20)).splitlines()[1].startswith("x:constworks  ")
    long = Table(TABLE.columns, [{**ROWS[0], "text": "x" * 200}])
    assert "x" * 200 in render([long], "table", Terminal(width=None)), "no width: never cut"


def test_a_value_never_breaks_its_line():
    table = Table([col("TEXT", "text")], [{"text": "one\ntwo\tthree"}])
    assert render([table], "plain", Terminal()) == "one two three\n"


def test_color_marks_meaning_and_stripping_it_loses_nothing():
    colored = render([TABLE], "table", Terminal(color=True))
    assert "\x1b[32mpublished" in colored and "\x1b[31mfailed" in colored
    assert ESC.sub("", colored) == render([TABLE], "table", Terminal())


def test_color_never_reaches_the_piped_form():
    fieldset = fields({"state": "failed"})
    assert "\x1b" not in render([TABLE, fieldset], "plain", Terminal(color=True))


def test_an_empty_table_prints_nothing():
    assert render([Table(TABLE.columns, [])], "table", Terminal()) == ""


def test_fields_flatten_nested_objects_and_skip_record_lists():
    block = fields(
        {"applied": False, "ledger": {"from_version": 1, "to_version": 2}, "rows": [{"a": 1}]},
        skip=("rows",),
    )
    assert block == Fields(
        [("applied", False), ("ledger from version", 1), ("ledger to version", 2)],
        semantic=frozenset(),
    )
    assert render([block], "table", Terminal()) == (
        "applied:              no\nledger from version:  1\nledger to version:    2\n"
    )


def test_blocks_are_separated_and_titled_on_a_terminal():
    out = render(
        [fields({"account": "x:a"}), Table(TABLE.columns, ROWS[:1], title="Posts")],
        "table",
        Terminal(),
    )
    assert out.split("\n\n")[1].startswith("Posts\nACCOUNT")
    piped = render([Table(TABLE.columns, ROWS[:1], title="Posts")], "plain", Terminal())
    assert "Posts" not in piped
