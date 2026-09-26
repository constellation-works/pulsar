"""The package boundaries the design depends on, checked on the import graph.

The order is written down in docs/design/ARCHITECTURE.md; this is its check.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pulsar"

# core is provider-neutral: no HTTP, no MCP, nothing from providers or surfaces.
# providers never reach up into surfaces.
FORBIDDEN = {
    "core": ("httpx", "mcp", "pulsar.providers", "pulsar.surfaces"),
    "providers": ("mcp", "pulsar.surfaces"),
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


# Inside surfaces: composition first, then the reports built on it, then the
# front ends. A module imports only modules of a lower rank; the CLI is the one
# dispatcher, so nothing imports it.
SURFACE_RANK = {
    "runtime": 0,
    "output": 0,
    "health": 1,
    "ops": 1,
    "views": 1,
    "mcp": 2,
    "orbit_tool": 2,
    "cli": 3,
}


def test_surface_modules_are_all_ranked():
    found = {p.stem for p in (SRC / "surfaces").glob("*.py") if p.stem != "__init__"}
    assert found == set(SURFACE_RANK), "rank every surfaces module in SURFACE_RANK"


@pytest.mark.parametrize("module", sorted(SURFACE_RANK))
def test_surface_imports_point_down(module):
    offenders = []
    for name in _imports(SRC / "surfaces" / f"{module}.py"):
        parts = name.split(".")
        if parts[:2] != ["pulsar", "surfaces"] or len(parts) < 3:
            continue
        target = parts[2]
        if target in SURFACE_RANK and SURFACE_RANK[target] >= SURFACE_RANK[module]:
            offenders.append(f"surfaces/{module} imports surfaces/{target}")
    assert not offenders, "surfaces import upward or sideways:\n" + "\n".join(
        sorted(set(offenders))
    )


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
