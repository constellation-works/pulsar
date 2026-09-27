"""The docs as contracts: links resolve, design docs carry their
frontmatter, and every documented ``pulsar`` command parses against the real CLI."""

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from pulsar.cli.main import build_parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(
    [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        *(ROOT / "docs" / "design").rglob("*.md"),
        *(ROOT / "skills").rglob("*.md"),
    ]
)
# Templates hold placeholders (`<feature>`), not links to check.
CHECKED = [p for p in DOCS if "_templates" not in p.parts]
DESIGN = [p for p in CHECKED if "design" in p.parts]
assert CHECKED and DESIGN, "no docs found"

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)
# Required frontmatter by kind (docs/design/CONVENTIONS.md §2 and _templates/).
FRONTMATTER = ("title", "owner", "last_updated", "status")
FRONTMATTER_SPEC = ("type", "summary", "last_validated")
CODE = re.compile(r"`(pulsar [^`]+)`")


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("doc", CHECKED, ids=_rel)
def test_relative_links_resolve(doc):
    broken = []
    for target in LINK.findall(CODE_SPAN.sub("", doc.read_text())):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0].split(":", 1)[0]
        if path and not (doc.parent / path).exists():
            broken.append(target)
    assert not broken, f"{_rel(doc)} links to missing files: {broken}"


@pytest.mark.parametrize("doc", DESIGN, ids=_rel)
def test_design_docs_carry_frontmatter(doc):
    text = doc.read_text()
    assert text.startswith("---\n"), f"{_rel(doc)} has no frontmatter (see CONVENTIONS.md)"
    meta = yaml.safe_load(text.split("---\n", 2)[1])
    lookup = {"specs", "references"} & set(doc.parts)
    missing = [key for key in (FRONTMATTER_SPEC if lookup else FRONTMATTER) if not meta.get(key)]
    assert not missing, f"{_rel(doc)} frontmatter lacks {missing}"


FENCE = re.compile(r"^```(?:sh|bash|shell|console)\n(.*?)^```", re.S | re.M)
ENV_ASSIGNMENT = re.compile(r"^[A-Z_][A-Z0-9_]*=\S*\s+")


def _fenced_commands(text: str) -> list[str]:
    """``pulsar`` commands in shell blocks: continuations joined, comments,
    ``cd … &&``, ``VAR=value`` prefixes and ``uv run`` stripped."""
    found = []
    for block in FENCE.findall(text):
        for line in block.replace("\\\n", " ").splitlines():
            for part in line.split("#", 1)[0].split("&&"):
                command = part.strip()
                while match := ENV_ASSIGNMENT.match(command):
                    command = command[match.end() :]
                command = command.removeprefix("uv run ").strip()
                if command.startswith("pulsar "):
                    found.append(command)
    return found


def _documented_commands() -> list[tuple[str, str]]:
    found = []
    for doc in DOCS:
        text = doc.read_text()
        for span in [*CODE.findall(text), *_fenced_commands(text)]:
            found.append((_rel(doc), span))
    return found


def _command_paths() -> set[tuple[str, ...]]:
    import argparse

    def walk(parser, prefix):
        yield prefix
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield from walk(child, (*prefix, name))

    return set(walk(build_parser(), ()))


COMMAND_PATHS = _command_paths()
COMMANDS = _documented_commands()
assert COMMANDS, "no documented pulsar commands found"
assert any(w == "README.md" and s.startswith("pulsar publish") for w, s in COMMANDS), (
    "the README's shell examples are not being checked"
)

# Placeholders in docs, by the option that takes them.
NUMERIC = {"--limit", "--port"}


def _argv(span: str) -> list[str] | None:
    """The span as argv with placeholders filled, or None for a span that names
    several commands at once (``auth login | status``). In a shell pipeline
    (``history | head -1``) only the pulsar command counts."""
    if "|" in span:
        head, *rest = span.split("|")
        if all(len(part.split()) <= 1 for part in rest):
            return None  # alternatives
        span = head  # a shell pipeline: check the pulsar command
    words = shlex.split(span.replace("[", " ").replace("]", " "))[1:]
    argv: list[str] = []
    for word in words:
        if word == "...":
            continue
        placeholder = "<" in word or re.fullmatch(r"[A-Z][A-Z_]*(\.[a-z]+)?", word)
        if placeholder:
            word = "5" if argv and argv[-1] in NUMERIC else "x:someone"
        argv.append(word)
    return argv


@pytest.mark.parametrize("where, span", COMMANDS, ids=[f"{w}: {s}" for w, s in COMMANDS])
def test_documented_commands_parse(where, span):
    argv = _argv(span)
    if argv is None:
        # `pulsar auth login | status | logout`: each alternative must be a command.
        parts = span.removeprefix("pulsar ").split("|")
        first = parts[0].split()
        alternatives = [first[-1], *(p.split()[0] for p in parts[1:] if p.strip())]
        missing = [a for a in alternatives if (*first[:-1], a) not in COMMAND_PATHS]
        assert not missing, f"{where}: `{span}` names unknown commands {missing}"
        return
    if tuple(argv) in COMMAND_PATHS:
        return  # a command named in prose, without its arguments
    try:
        build_parser().parse_args(argv)
    except SystemExit as exc:
        pytest.fail(f"{where}: `{span}` does not parse (exit {exc.code})")


def test_no_symlink_is_tracked():
    """The plugin installer refuses symlinks anywhere in the tree."""
    listing = subprocess.run(
        ["git", "ls-files", "-s"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    links = [line.split("\t", 1)[1] for line in listing.splitlines() if line.startswith("120000")]
    assert links == [], f"tracked symlinks (the installer refuses them): {links}"
