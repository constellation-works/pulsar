"""Copy the canonical Python package into the installable Orbit plugin."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".orbit-plugin"
COPIED_FILES = ("pyproject.toml", "uv.lock")


def package_files(base: Path) -> dict[Path, Path]:
    """Return runtime package files, excluding local Python build output."""
    result = {}
    for path in (base / "src").rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink in package: {path}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(base)] = path
    return result


def check() -> list[str]:
    expected = package_files(ROOT)
    actual = package_files(PLUGIN)
    problems = []
    for relative in sorted(expected.keys() | actual.keys()):
        if relative not in actual:
            problems.append(f"missing: {relative}")
        elif relative not in expected:
            problems.append(f"extra: {relative}")
        elif expected[relative].read_bytes() != actual[relative].read_bytes():
            problems.append(f"changed: {relative}")
    for name in COPIED_FILES:
        if (
            not (PLUGIN / name).is_file()
            or (ROOT / name).read_bytes() != (PLUGIN / name).read_bytes()
        ):
            problems.append(f"changed or missing: {name}")
    return problems


def build() -> None:
    package_files(ROOT)  # reject links before removing the prior generated copy
    target = PLUGIN / "src"
    if target.is_symlink():
        raise ValueError(f"symlink in generated package: {target}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        ROOT / "src",
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        symlinks=False,
    )
    for name in COPIED_FILES:
        shutil.copy2(ROOT / name, PLUGIN / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed copy drifted")
    args = parser.parse_args()
    if not args.check:
        build()
    problems = check()
    if problems:
        parser.exit(1, "\n".join(problems) + "\n")
