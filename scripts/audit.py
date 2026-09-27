"""Audit every PyPI package and artifact pinned in uv.lock."""

from __future__ import annotations

import datetime as dt
import json
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYPI = "https://pypi.org/pypi"
OSV = "https://api.osv.dev/v1/querybatch"
LICENSE_CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF",
}


class AuditError(Exception):
    """The audit could not establish a trustworthy result."""


def request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AuditError(f"request failed for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"invalid JSON object from {url}")
    return value


def packages_from_lock(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        lock = tomllib.load(handle)
    packages = lock.get("package")
    if not isinstance(packages, list) or not packages:
        raise AuditError("uv.lock has no packages")
    external = []
    for package in packages:
        if not isinstance(package, dict):
            raise AuditError("invalid package in uv.lock")
        source = package.get("source")
        if source == {"editable": "."} and package.get("name") == "pulsar":
            continue  # The first-party project is not a PyPI dependency.
        if source != {"registry": "https://pypi.org/simple"}:
            raise AuditError(f"unexpected package source for {package.get('name')}: {source}")
        if not isinstance(package.get("name"), str) or not isinstance(package.get("version"), str):
            raise AuditError("package without name or version in uv.lock")
        external.append(package)
    return external


def license_value(info: dict[str, Any]) -> str:
    expression = info.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        return expression.strip()
    legacy = info.get("license")
    if isinstance(legacy, str) and legacy.strip() and "\n" not in legacy:
        return legacy.strip()
    classifiers = info.get("classifiers")
    if isinstance(classifiers, list):
        values = {LICENSE_CLASSIFIERS[item] for item in classifiers if item in LICENSE_CLASSIFIERS}
        if len(values) == 1:
            return values.pop()
    return "unknown"


def load_policy(path: Path, today: dt.date) -> tuple[set[str], set[tuple[str, str, str, str]]]:
    with path.open("rb") as handle:
        policy = tomllib.load(handle)
    allowlist = policy.get("license_allowlist")
    if (
        not isinstance(allowlist, list)
        or not allowlist
        or any(not isinstance(item, str) or not item.strip() for item in allowlist)
    ):
        raise AuditError("license_allowlist must contain nonempty strings")
    exceptions = policy.get("exceptions", [])
    if not isinstance(exceptions, list):
        raise AuditError("exceptions must be a list")
    approved: set[tuple[str, str, str, str]] = set()
    for entry in exceptions:
        if not isinstance(entry, dict) or set(entry) != {
            "kind",
            "name",
            "version",
            "value",
            "reason",
            "review_date",
        }:
            raise AuditError(
                "each exception needs kind, name, version, value, reason and review_date"
            )
        if (
            not isinstance(entry["kind"], str)
            or entry["kind"] not in {"advisory", "yanked", "license"}
            or any(
                not isinstance(entry[field], str) or not entry[field].strip()
                for field in ("name", "version", "value", "reason")
            )
        ):
            raise AuditError("invalid exception value or missing reason")
        if not isinstance(entry["review_date"], dt.date) or isinstance(
            entry["review_date"], dt.datetime
        ):
            raise AuditError("review_date must be a TOML date")
        key = (entry["kind"], entry["name"], entry["version"], entry["value"])
        if key in approved:
            raise AuditError(f"duplicate exception: {key}")
        if today >= entry["review_date"]:
            raise AuditError(f"exception due for re-review: {key} ({entry['review_date']})")
        approved.add(key)
    return set(allowlist), approved


def artifact_hashes(package: dict[str, Any]) -> set[str]:
    artifacts = [*package.get("wheels", [])]
    if package.get("sdist") is not None:
        artifacts.append(package["sdist"])
    if any(not isinstance(item, dict) for item in artifacts):
        raise AuditError(f"invalid artifacts for {package['name']}")
    hashes = {item.get("hash") for item in artifacts}
    if not hashes or any(
        not isinstance(item, str) or not item.startswith("sha256:") for item in hashes
    ):
        raise AuditError(f"missing or invalid artifact hashes for {package['name']}")
    return hashes


def audit(
    lock_path: Path,
    policy_path: Path,
    fetch: Callable[[str, dict[str, Any] | None], dict[str, Any]],
    today: dt.date,
) -> list[str]:
    packages = packages_from_lock(lock_path)
    allowed, exceptions = load_policy(policy_path, today)
    findings: list[str] = []
    for package in packages:
        name, version = package["name"], package["version"]
        url = f"{PYPI}/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"
        release = fetch(url, None)
        info, files = release.get("info"), release.get("urls")
        if not isinstance(info, dict) or not isinstance(files, list) or not files:
            raise AuditError(f"missing PyPI release metadata: {name}=={version}")
        license_name = license_value(info)
        if (
            license_name not in allowed
            and ("license", name, version, license_name) not in exceptions
        ):
            findings.append(f"{name}=={version}: license {license_name!r} is not allowed")
        locked_hashes = artifact_hashes(package)
        found_hashes: set[str] = set()
        for artifact in files:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("digests"), dict):
                raise AuditError(f"invalid PyPI artifact metadata: {name}=={version}")
            digest = artifact["digests"].get("sha256")
            hash_value = f"sha256:{digest}"
            if hash_value in locked_hashes:
                found_hashes.add(hash_value)
                if (
                    artifact.get("yanked") is not False
                    and ("yanked", name, version, hash_value) not in exceptions
                ):
                    findings.append(f"{name}=={version}: locked artifact {hash_value} is yanked")
        missing = locked_hashes - found_hashes
        if missing:
            raise AuditError(
                f"locked artifacts missing from PyPI: {name}=={version}: {sorted(missing)}"
            )

    pending = [(package, None) for package in packages]
    while pending:
        queries = [
            {
                "version": package["version"],
                "package": {"name": package["name"], "ecosystem": "PyPI"},
                **({"page_token": token} if token else {}),
            }
            for package, token in pending
        ]
        response = fetch(OSV, {"queries": queries})
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(pending):
            raise AuditError("invalid OSV querybatch response")
        following = []
        for (package, _), result in zip(pending, results, strict=True):
            if not isinstance(result, dict):
                raise AuditError("invalid OSV result")
            vulns = result.get("vulns", [])
            if not isinstance(vulns, list):
                raise AuditError("invalid OSV vulnerabilities")
            for vuln in vulns:
                advisory = vuln.get("id") if isinstance(vuln, dict) else None
                if not isinstance(advisory, str) or not advisory:
                    raise AuditError("OSV vulnerability without id")
                name, version = package["name"], package["version"]
                if ("advisory", name, version, advisory) not in exceptions:
                    findings.append(f"{name}=={version}: advisory {advisory}")
            token = result.get("next_page_token")
            if token is not None:
                if not isinstance(token, str) or not token:
                    raise AuditError("invalid OSV page token")
                following.append((package, token))
        pending = following
    return findings


def main() -> int:
    try:
        findings = audit(
            ROOT / "uv.lock", ROOT / "audit-exceptions.toml", request_json, dt.date.today()
        )
    except (AuditError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"audit finding: {finding}", file=sys.stderr)
        return 1
    print("audit passed: locked PyPI packages have no unexcepted advisories, yanks or licenses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
