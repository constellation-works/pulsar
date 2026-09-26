"""The package boundaries the design depends on, checked on the import graph."""

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
    assert not offenders, "\n".join(offenders)
