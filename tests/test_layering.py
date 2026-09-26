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


def _from_module(path: Path, node: ast.ImportFrom) -> str:
    """The absolute module a ``from ... import`` in ``path`` names."""
    if not node.level:
        return node.module or ""
    base = path.relative_to(SRC.parent).with_suffix("").parts[:-1]
    base = list(base[: len(base) - node.level + 1])
    return ".".join(base + ([node.module] if node.module else []))


def _statements(path: Path):
    """Each import in ``path``: (absolute module, imported names or None for a
    plain ``import``, line)."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None, node.lineno
        elif isinstance(node, ast.ImportFrom):
            yield _from_module(path, node), [alias.name for alias in node.names], node.lineno


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for module, names, _ in _statements(path):
        found.add(module)
        found.update(f"{module}.{name}" for name in names or ())
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
# starts the other two (`serve`, `orbit-tool`), so it ranks above them. main,
# the entry point, builds the app and supplies it to a front end; nothing
# imports it.
PULSAR_RANK = {
    "core": 0,
    "providers": 1,
    "app": 2,
    "mcp": 3,
    "orbit_tool": 3,
    "cli": 4,
    "main": 5,
}

# Inside app: the runtime first, then the reports built on it, then App over them.
APP_RANK = {
    "runtime": 0,
    "health": 1,
    "ops": 1,
    "facade": 2,
}

# Inside the CLI: main parses and dispatches to the commands; beneath both,
# the toolkit they are written against.
CLI_RANK = {
    "toolkit": 0,
    "commands": 1,
    "main": 2,
}

# Inside the toolkit: rendering and errors first, then what commands declare
# and print with.
TOOLKIT_RANK = {
    "render": 0,
    "errors": 0,
    "views": 1,
    "parser": 1,
    "context": 2,
}

# The commands stand side by side: none imports another.
COMMANDS_RANK = {
    "auth": 0,
    "history": 0,
    "maintenance": 0,
    "publish": 0,
    "reconcile": 0,
    "services": 0,
    "status": 0,
}

RANKED = {
    "pulsar": PULSAR_RANK,
    "pulsar.app": APP_RANK,
    "pulsar.cli": CLI_RANK,
    "pulsar.cli.toolkit": TOOLKIT_RANK,
    "pulsar.cli.commands": COMMANDS_RANK,
}


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


# A lower layer's public API is its package's __init__: code outside the
# package imports from the package root, and only the names __all__ lists.
FACADES = (
    "pulsar.core",
    "pulsar.providers.x",
    "pulsar.app",
    "pulsar.cli",
    "pulsar.cli.toolkit",
    "pulsar.cli.commands",
)


def _public(package: str) -> set[str]:
    tree = ast.parse((_package_dir(package) / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    return set()


def _namespace_misuse(path: Path, facade: str, public: set[str]) -> list[str]:
    """A facade imported whole (``from pulsar.cli import toolkit``) is used only
    through the names its ``__all__`` lists (``toolkit.notice``)."""
    parent, _, leaf = facade.rpartition(".")
    tree = ast.parse(path.read_text())
    bound = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and _from_module(path, node) == parent
        for alias in node.names
        if alias.name == leaf
    }
    return [
        f"{path.relative_to(SRC)}:{node.lineno} uses {leaf}.{node.attr}, not in {facade}.__all__"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in bound
        and node.attr not in public
    ]


@pytest.mark.parametrize("facade", FACADES)
def test_other_layers_use_only_the_public_api(facade):
    public = _public(facade)
    assert public, f"{facade}/__init__.py declares no __all__"
    parent, _, leaf = facade.rpartition(".")
    inside = _package_dir(facade)
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.is_relative_to(inside):
            continue
        for module, names, line in _statements(path):
            where = f"{path.relative_to(SRC)}:{line}"
            if module.startswith(facade + "."):
                offenders.append(f"{where} reaches into {module}")
            elif module == facade and names is None:
                offenders.append(f"{where} imports {facade} as a module; import names from it")
            elif module == facade:
                hidden = sorted(set(names) - public)
                if hidden:
                    offenders.append(f"{where} imports {hidden}, not in {facade}.__all__")
        offenders += _namespace_misuse(path, facade, public)
    assert not offenders, f"{facade} used past its public API:\n" + "\n".join(offenders)


# The front ends and the entry point reach everything below through app:
# never core or providers.
FRONT_ENDS = ("cli", "mcp", "orbit_tool", "main")
BELOW_APP = ("pulsar.core", "pulsar.providers")


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_front_ends_go_through_app(front_end):
    single = SRC / f"{front_end}.py"
    files = ([single] if single.exists() else []) + sorted((SRC / front_end).rglob("*.py"))
    assert files, f"{front_end} has no source"
    offenders = []
    for path in files:
        for module, names, line in _statements(path):
            for below in BELOW_APP:
                parent, _, leaf = below.rpartition(".")
                if (
                    module == below
                    or module.startswith(below + ".")
                    or (module == parent and names and leaf in names)
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{line} imports {below}")
    assert not offenders, "front ends skip app:\n" + "\n".join(offenders)


# Imports never climb the tree: no `from ..`. What a module needs sits beneath
# it (its package's modules and subpackages) or in a lower layer's root. The
# ledger still reaches core's shared modules (errors, jsonx, paths, ...) this
# way; it is the one known exception until those move beneath it.
CLIMBS_ALLOWED = ("core/ledger",)


def test_imports_never_climb():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if any(rel.as_posix().startswith(prefix + "/") for prefix in CLIMBS_ALLOWED):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level >= 2:
                offenders.append(f"{rel}:{node.lineno} from {'.' * node.level}{node.module or ''}")
    assert not offenders, "imports climb the tree; move the module beneath or pass it down:\n" + (
        "\n".join(offenders)
    )


# A package holds only what it is named for. cli/commands holds commands: each
# module there declares its commands in `register`. What they are written
# against is not a command; it lives in cli/toolkit, beneath them.
@pytest.mark.parametrize("member", sorted(COMMANDS_RANK))
def test_commands_holds_only_commands(member):
    tree = ast.parse((_package_dir("pulsar.cli.commands") / f"{member}.py").read_text())
    top = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "register" in top, f"cli/commands/{member}.py declares no command; move it beneath"


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
