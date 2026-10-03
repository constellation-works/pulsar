"""The installable plugin contains a fresh copy of the canonical runtime package."""

import os
import subprocess
from pathlib import Path

from scripts.build_plugin import PLUGIN, ROOT, check


def _repository_files(root: Path) -> set[Path]:
    """Return existing tracked and non-ignored untracked files in a checkout."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return {
        root / os.fsdecode(relative)
        for relative in result.stdout.split(b"\0")
        if relative and (root / os.fsdecode(relative)).is_file()
    }


def test_generated_package_matches_canonical_files():
    assert not check(), "run make plugin to refresh the generated package"
    assert (PLUGIN / "README.md").is_file()
    assert not any(
        path.name == "__pycache__" or path.suffix == ".pyc" for path in (PLUGIN / "src").rglob("*")
    )


def test_plugin_layout_and_manifest_paths():
    import yaml

    assert not list(ROOT.glob("plugin.yaml"))
    repository_files = _repository_files(ROOT)
    assert not any(path.name == "orbit_plugin.yaml" for path in repository_files)
    assert {path for path in repository_files if path.name == "plugin.yaml"} == {
        PLUGIN / "plugin.yaml"
    }
    assert not any(path.is_symlink() for path in PLUGIN.rglob("*"))
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    spec = manifest["spec"]
    paths = [spec["backend"]["command"], spec["config"]["schema"]]
    paths += [
        tool[key]["$ref"] for tool in spec["tools"] for key in ("input_schema", "output_schema")
    ]
    paths += spec["skills"]
    for path in paths:
        resolved = (PLUGIN / path).resolve()
        assert resolved.is_relative_to(PLUGIN.resolve()) and resolved.exists(), path
    for pattern in [*(g for globs in spec["definitions"].values() for g in globs), *spec["tests"]]:
        matches = list(PLUGIN.glob(pattern))
        assert matches and all(p.resolve().is_relative_to(PLUGIN.resolve()) for p in matches)


def test_repository_file_inventory_ignores_nested_orbit_worktrees(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)

    plugin_manifest = root / ".orbit-plugin" / "plugin.yaml"
    plugin_manifest.parent.mkdir()
    plugin_manifest.write_text("plugin: true\n")
    source = root / "src" / "package"
    source.mkdir(parents=True)
    (source / "module.py").write_text("\n")
    (root / ".gitignore").write_text(".orbit/\n")
    subprocess.run(
        ["git", "add", ".gitignore", ".orbit-plugin/plugin.yaml", "src/package/module.py"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=root, check=True)

    nested_worktree = root / ".orbit" / "state" / "worktrees" / "run"
    nested_manifest = nested_worktree / "plugin.yaml"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "-b", "nested-run", str(nested_worktree)],
        cwd=root,
        check=True,
    )
    nested_manifest.write_text("nested: true\n")
    root_manifest = root / "plugin.yaml"
    root_manifest.write_text("stray: true\n")
    source_manifest = source / "plugin.yaml"
    source_manifest.write_text("stray: true\n")
    subprocess.run(["git", "add", "src/package/plugin.yaml"], cwd=root, check=True)

    repository_files = _repository_files(root)
    assert plugin_manifest in repository_files
    assert root_manifest in repository_files
    assert source_manifest in repository_files
    assert nested_manifest not in repository_files
