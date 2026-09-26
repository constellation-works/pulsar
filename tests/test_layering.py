"""The package boundaries the design depends on, checked on the import graph.

The order is written down in docs/design/ARCHITECTURE.md; this is its check.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pulsar"

# Third-party boundaries; the order between pulsar's own layers is RANK below.
# core is provider-neutral: no HTTP, no MCP. Only the MCP front end speaks MCP.
FORBIDDEN = {
    "core": ("httpx", "mcp"),
    "providers": ("mcp",),
    "app": ("mcp",),
}


def _imports(path: Path) -> set[str]:
    package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_imports_stay_inside_their_boundary(layer):
    offenders = []
    for path in sorted((SRC / layer).rglob("*.py")):
        for name in _imports(path):
            for banned in FORBIDDEN[layer]:
                if name == banned or name.startswith(banned + "."):
                    offenders.append(f"{path.relative_to(SRC)} imports {name}")
    assert not offenders, f"{layer} crossed its boundary (see ARCHITECTURE.md):\n" + "\n".join(
        offenders
    )


# The layers, bottom up: a module (or subpackage) imports only lower ranks.
# core supplies everything; providers plug a channel into it; app joins them
# into the verbs every front end calls; the front ends sit on top. The CLI
# starts the other two (`serve`, `orbit-tool`), so it ranks above them and
# nothing imports it.
PULSAR_RANK = {
    "core": 0,
    "providers": 1,
    "app": 2,
    "mcp": 3,
    "orbit_tool": 3,
    "cli": 4,
}

# Inside app: the runtime first, then the reports built on it.
APP_RANK = {
    "runtime": 0,
    "health": 1,
    "ops": 1,
}

# Inside the CLI: rendering and errors first, then what commands share, then
# the commands, then the entry point that registers them.
CLI_RANK = {
    "render": 0,
    "errors": 0,
    "views": 1,
    "parser": 1,
    "context": 2,
    "commands": 3,
    "main": 4,
}

RANKED = {"pulsar": PULSAR_RANK, "pulsar.app": APP_RANK, "pulsar.cli": CLI_RANK}


def _package_dir(package: str) -> Path:
    return SRC.parent.joinpath(*package.split("."))


def _members(package: str) -> set[str]:
    """A package's modules and subpackages; its dunder entry points are not ranked."""
    root = _package_dir(package)
    modules = {p.stem for p in root.glob("*.py") if not p.stem.startswith("__")}
    return modules | {p.parent.name for p in root.glob("*/__init__.py")}


@pytest.mark.parametrize("package", sorted(RANKED))
def test_members_are_all_ranked(package):
    assert _members(package) == set(RANKED[package]), f"rank every member of {package}"


@pytest.mark.parametrize(
    ("package", "member"), [(pkg, m) for pkg, ranks in RANKED.items() for m in sorted(ranks)]
)
def test_imports_point_down(package, member):
    ranks = RANKED[package]
    prefix = package.split(".")
    root = _package_dir(package)
    files = [root / f"{member}.py"] if (root / f"{member}.py").exists() else []
    files += sorted((root / member).rglob("*.py"))
    assert files, f"{package}.{member} has no source"
    offenders = []
    for path in files:
        for name in _imports(path):
            parts = name.split(".")
            if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
                continue
            target = parts[len(prefix)]
            if target != member and target in ranks and ranks[target] >= ranks[member]:
                offenders.append(f"{path.relative_to(SRC)} imports {package}.{target}")
    assert not offenders, "imports point upward or sideways:\n" + "\n".join(sorted(set(offenders)))


# core takes the environment, the cwd and the home as arguments (STD-02 §R3).
AMBIENT = {("os", "environ"), ("os", "getenv"), ("os", "getcwd"), ("Path", "cwd"), ("Path", "home")}
# Methods that read $HOME (or the cwd) whatever they are called on.
AMBIENT_METHODS = {"expanduser", "getcwd", "cwd"}


def test_core_reads_no_ambient_state():
    offenders = []
    for path in sorted((SRC / "core").rglob("*.py")):
        rel = str(path.relative_to(SRC))
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and (node.value.id, node.attr) in AMBIENT
            ):
                offenders.append(f"{rel}:{node.lineno} uses {node.value.id}.{node.attr}")
            elif isinstance(node, ast.Attribute) and node.attr in AMBIENT_METHODS:
                offenders.append(f"{rel}:{node.lineno} calls .{node.attr}()")
    assert not offenders, "core reads ambient state; take it as an argument:\n" + "\n".join(
        offenders
    )
