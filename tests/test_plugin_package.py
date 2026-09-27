"""The installable plugin contains a fresh copy of the canonical runtime package."""

from scripts.build_plugin import PLUGIN, ROOT, check


def test_generated_package_matches_canonical_files():
    assert not check(), "run make plugin to refresh the generated package"
    assert (PLUGIN / "README.md").is_file()
    assert not any(
        path.name == "__pycache__" or path.suffix == ".pyc" for path in (PLUGIN / "src").rglob("*")
    )


def test_plugin_layout_and_manifest_paths():
    import yaml

    assert not list(ROOT.glob("plugin.yaml"))
    assert not list(ROOT.rglob("orbit_plugin.yaml"))
    assert list(ROOT.rglob("plugin.yaml")) == [PLUGIN / "plugin.yaml"]
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
    for pattern in [*spec["definitions"]["auto_tasks"], *spec["tests"]]:
        matches = list(PLUGIN.glob(pattern))
        assert matches and all(p.resolve().is_relative_to(PLUGIN.resolve()) for p in matches)
