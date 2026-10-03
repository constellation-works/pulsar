"""The source tree is the architecture (docs/design/ARCHITECTURE.md): every import
between pulsar's units points the way the tree says, a provider's channel
package is reached only through the provider -> channel factory, and a package
with subpackages keeps its ``__init__`` free of imports."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PULSAR = SRC / "pulsar"

INTERNAL = {"internal.errors", "internal.fs", "internal.guard"}
# One package per provider under app/core/channels; nothing else names a provider.
PROVIDERS = {"app.core.channels.x", "app.core.channels.bluesky"}
CORE = {
    "app.core.account",
    "app.core.channels",
    *PROVIDERS,
    "app.core.engagement",
    "app.core.ledger",
    "app.core.publishing",
}
# What each unit may import besides itself. ``app`` is the app's own modules:
# a front end never reaches ``app.core``.
ALLOWED = {
    # the parents themselves: docstring-only (checked below)
    "internal": set(),
    "app.core": set(),
    "internal.errors": INTERNAL,
    "internal.fs": INTERNAL,
    "internal.guard": INTERNAL,
    "app.core.channels": INTERNAL,
    "app.core.ledger": INTERNAL,
    # a provider stands on the contract alone, never on another provider
    **{provider: {"app.core.channels"} | INTERNAL for provider in PROVIDERS},
    "app.core.account": {"app.core.channels"} | INTERNAL,
    "app.core.publishing": {"app.core.account", "app.core.channels", "app.core.ledger"} | INTERNAL,
    "app.core.engagement": {"app.core.channels", "app.core.ledger", "app.core.publishing"}
    | INTERNAL,
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
        provider = ".".join(parts[:4])
        return provider if provider in PROVIDERS else ".".join(parts[:3])
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
                # ``from pulsar.app.core.channels import x`` imports the package ``x``.
                found += [(node.lineno, f"{target}.{a.name}") for a in node.names]
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


# The modules outside a provider's package that may import it. ``app.runtime`` is
# the provider -> channel factory, the one place a new provider is wired in. X
# predates the rule: its login, health check and single-request tools still
# name it, and ``import-posted`` reads an X routine's log.
FACTORY = "app.runtime"
PROVIDER_IMPORTERS = {
    "app.core.channels.bluesky": {FACTORY},
    "app.core.channels.x": {
        FACTORY,
        "app.facade",
        "app.health",
        "app.interfaces",
        "app.login",
        "app.ops",
        "app.tools",
    },
}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_providers_are_reached_only_through_the_factory(path):
    module, _ = _module(path)
    unit = _unit(module)
    wrong = [
        f"line {line}: {target}"
        for line, target in _imports(path)
        if (provider := _unit(target)) in PROVIDERS
        and provider != unit
        and module not in PROVIDER_IMPORTERS[provider]
    ]
    assert not wrong, f"{module} names a provider outside the channel factory: {wrong}"


def test_every_provider_has_its_importers_listed():
    assert set(PROVIDER_IMPORTERS) == PROVIDERS
    on_disk = {
        f"app.core.channels.{p.name}"
        for p in (PULSAR / "app" / "core" / "channels").iterdir()
        if (p / "__init__.py").exists()
    }
    assert on_disk == PROVIDERS, "a channel package is missing from PROVIDERS"


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
