"""Offline checks for the networked lock audit."""

import datetime as dt
from pathlib import Path

import pytest

from scripts import audit as checker

TODAY = dt.date(2026, 9, 27)
ADVISORY = "GHSA-462w-v97r-4m45"  # OSV reports this for Jinja2 2.4.1.


def inputs(tmp_path: Path, exception: str = "") -> tuple[Path, Path]:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "jinja2"\nversion = "2.4.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ hash = "sha256:locked" }]\n'
    )
    policy = tmp_path / "audit-exceptions.toml"
    policy.write_text('license_allowlist = ["MIT"]\n' + exception)
    return lock, policy


def fake_fetch(*, license_name: str = "MIT", yanked: bool = False, advisory: str = ""):
    def fetch(url: str, payload: object) -> dict[str, object]:
        if url == checker.OSV:
            assert payload is not None
            return {"results": [{"vulns": [{"id": advisory}]} if advisory else {}]}
        return {
            "info": {"license_expression": license_name},
            "urls": [{"digests": {"sha256": "locked"}, "yanked": yanked}],
        }

    return fetch


@pytest.mark.parametrize(
    ("fetch", "message"),
    [
        (fake_fetch(advisory=ADVISORY), f"advisory {ADVISORY}"),
        (fake_fetch(yanked=True), "locked artifact sha256:locked is yanked"),
        (fake_fetch(license_name="GPL-3.0-only"), "license 'GPL-3.0-only' is not allowed"),
    ],
)
def test_rejects_advisory_yank_and_disallowed_license(
    tmp_path: Path, fetch: object, message: str
) -> None:
    lock, policy = inputs(tmp_path)
    assert any(message in finding for finding in checker.audit(lock, policy, fetch, TODAY))


def test_dated_exception_allows_exact_finding_until_review_date(tmp_path: Path) -> None:
    lock, policy = inputs(
        tmp_path,
        '[[exceptions]]\nkind = "advisory"\nname = "jinja2"\nversion = "2.4.1"\n'
        f'value = "{ADVISORY}"\nreason = "Pending patched upstream release"\n'
        "review_date = 2026-10-01\n",
    )
    assert checker.audit(lock, policy, fake_fetch(advisory=ADVISORY), TODAY) == []
    with pytest.raises(checker.AuditError, match="due for re-review"):
        checker.audit(lock, policy, fake_fetch(advisory=ADVISORY), dt.date(2026, 10, 1))


def test_missing_locked_artifact_fails_closed(tmp_path: Path) -> None:
    lock, policy = inputs(tmp_path)

    def missing(url: str, payload: object) -> dict[str, object]:
        assert payload is None
        return {
            "info": {"license_expression": "MIT"},
            "urls": [{"digests": {"sha256": "other"}, "yanked": False}],
        }

    with pytest.raises(checker.AuditError, match="missing from PyPI"):
        checker.audit(lock, policy, missing, TODAY)
