"""The source tree is the architecture (docs/design/ARCHITECTURE.md): every import
between pulsar's units points the way the tree says, and a package with
subpackages keeps its ``__init__`` free of imports."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PULSAR = SRC / "pulsar"

INTERNAL = {"internal.errors", "internal.fs", "internal.guard"}
CORE = {
    "app.core.account",
    "app.core.channels",
    "app.core.channels.x",
    "app.core.ledger",
    "app.core.publishing",
}
# What each unit may import besides itself. ``app`` is the app's own modules;
# a front end reaching ``app.core`` goes through ``app/exports.py``.
ALLOWED = {
    # the parents themselves: docstring-only (checked below)
    "internal": set(),
    "app.core": set(),
    "internal.errors": INTERNAL,
    "internal.fs": INTERNAL,
    "internal.guard": INTERNAL,
    "app.core.channels": INTERNAL,
    "app.core.ledger": INTERNAL,
    "app.core.channels.x": {"app.core.channels"} | INTERNAL,
    "app.core.account": {"app.core.channels"} | INTERNAL,
    "app.core.publishing": {"app.core.account", "app.core.channels", "app.core.ledger"} | INTERNAL,
    "app": CORE | INTERNAL,
    "mcp": {"app"} | INTERNAL,
    "orbit": {"app"} | INTERNAL,
    "cli": {"app", "mcp", "orbit"} | INTERNAL,
    "main": {"app", "cli"},
    "__main__": {"main"},
}


def _unit(module: str) -> str:
    """The unit a dotted module (without the ``pulsar.`` prefix) belongs to."""
    parts = module.split(".")
    if parts[:2] == ["app", "core"]:
        return ".".join(parts[:4] if parts[2:4] == ["channels", "x"] else parts[:3])
    if parts[0] == "internal":
        return ".".join(parts[:2])
    return parts[0]


def _module(path: Path) -> tuple[str, str]:
    """(module, package) for a source file, both without the ``pulsar.`` prefix."""
    parts = path.relative_to(PULSAR).with_suffix("").parts
    if parts[-1] == "__init__":
        package = ".".join(parts[:-1])
        return package, package
    return ".".join(parts), ".".join(parts[:-1])


def _imports(path: Path) -> list[tuple[int, str]]:
    module, package = _module(path)
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [
                (node.lineno, a.name.removeprefix("pulsar."))
                for a in node.names
                if a.name.startswith("pulsar.")
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            elif node.module and node.module.startswith("pulsar."):
                target = node.module.removeprefix("pulsar.")
            else:
                continue
            if target:
                found.append((node.lineno, target))
    return found


SOURCES = sorted(p for p in PULSAR.rglob("*.py") if p.parent != PULSAR or p.name != "__init__.py")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_imports_point_down_the_tree(path):
    module, _ = _module(path)
    unit = _unit(module)
    assert unit in ALLOWED, f"{module} is in no unit of the architecture; add it to ALLOWED"
    wrong = [
        f"line {line}: {target}"
        for line, target in _imports(path)
        if _unit(target) != unit and _unit(target) not in ALLOWED[unit]
    ]
    assert not wrong, f"{module} ({unit}) imports against the architecture: {wrong}"


PARENTS = sorted(
    p / "__init__.py"
    for p in PULSAR.rglob("*")
    if p.is_dir()
    and p != PULSAR
    and any(c.is_dir() and (c / "__init__.py").exists() for c in p.iterdir())
)


@pytest.mark.parametrize("init", PARENTS, ids=lambda p: str(p.relative_to(SRC)))
def test_a_package_with_subpackages_imports_nothing_in_its_init(init):
    body = ast.parse(init.read_text()).body
    imports = [
        n.lineno
        for n in body
        if isinstance(n, ast.Import | ast.ImportFrom)
        and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    ]
    assert not imports, (
        f"{init.relative_to(SRC)} imports on lines {imports}: keep it to a docstring"
    )
