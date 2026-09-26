"""bin/pulsar, the entry point Orbit runs (STD-04 §R1), driven as a subprocess.

A stub ``uv`` stands in for the dependency sync: it records each call and
makes ``$UV_PROJECT_ENVIRONMENT/bin/python`` a wrapper around the interpreter
running these tests, which already has pulsar's dependencies. The plugin root
is a copy (launcher, lock, src) so a test can change the lock; the child's
environment is built from scratch (STD-03 §R20) and nothing touches the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
ENVELOPE = {"schema_version": 1, "tool": "pulsar.status", "input": {}, "context": {}}
BASE_PATH = "/usr/bin:/bin"

STUB_UV = """#!/bin/sh
printf '%s\\n' "$*" >>"{log}"
[ "${{1:-}}" = "--version" ] && {{ echo "uv 0.0.0-stub"; exit 0; }}
[ -e "{fail}" ] && {{ echo "stub: sync refused" >&2; exit 3; }}
echo "sync noise that must not reach stdout"
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/sh\\nexec "{python}" "$@"\\n' >"$UV_PROJECT_ENVIRONMENT/bin/python"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
"""


@dataclass
class Plugin:
    root: Path
    state: Path
    home: Path
    stub_dir: Path
    log: Path
    fail_flag: Path

    @property
    def venv(self) -> Path:
        return self.state / "venv"

    @property
    def stamp(self) -> Path:
        return self.venv / ".pulsar-lock"

    def syncs(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.startswith("sync")]

    def env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.stub_dir}:{BASE_PATH}",
            "ORBIT_PLUGIN_ROOT": str(self.root),
            "ORBIT_PLUGIN_STATE": str(self.state),
            "LC_ALL": "C",
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run(
        self, envelope: object = ENVELOPE, **overrides: str | None
    ) -> subprocess.CompletedProcess[str]:
        # Its own session, so the launcher, timeout(1), the stub uv and the
        # Python it execs are all reaped with the group whatever happens
        # (STD-03 §R11, §R18).
        proc = subprocess.Popen(
            ["/bin/sh", str(self.root / "bin" / "pulsar")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(**overrides),
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(json.dumps(envelope), timeout=120)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@pytest.fixture
def plugin(tmp_path: Path) -> Plugin:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    shutil.copy2(REPO / "bin" / "pulsar", root / "bin" / "pulsar")
    shutil.copy2(REPO / "uv.lock", root / "uv.lock")
    shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    log = tmp_path / "uv.log"
    fail_flag = tmp_path / "fail-sync"
    uv = stub_dir / "uv"
    uv.write_text(STUB_UV.format(log=log, fail=fail_flag, python=sys.executable))
    uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return Plugin(root, state, home, stub_dir, log, fail_flag)


def one_envelope(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    envelope = json.loads(lines[0])
    assert isinstance(envelope, dict)
    return envelope


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("missing", ["ORBIT_PLUGIN_ROOT", "ORBIT_PLUGIN_STATE"])
def test_a_missing_orbit_variable_is_an_envelope_not_a_crash(plugin, missing):
    envelope = one_envelope(plugin.run(**{missing: None}))
    assert envelope == {
        "ok": False,
        "error": {"code": "launcher_env", "message": f"{missing} is not set", "retryable": False},
    }
    assert plugin.syncs() == []


# The launcher's fixed fallbacks after PATH and $HOME. A host may really have
# a uv there, so this test points them into its own temp dir (STD-04 §R7, §R8:
# it runs everywhere, never skips).
UV_FALLBACKS = ("/opt/homebrew/bin/uv", "/usr/local/bin/uv")


def test_no_runnable_uv_is_named(plugin):
    launcher = plugin.root / "bin" / "pulsar"
    text = launcher.read_text()
    for fixed in UV_FALLBACKS:
        assert fixed in text, f"the launcher no longer probes {fixed}; update this test"
        text = text.replace(fixed, str(plugin.root / "absent" / "uv"))
    launcher.write_text(text)
    envelope = one_envelope(plugin.run(PATH=BASE_PATH))
    assert envelope["ok"] is False and envelope["error"]["code"] == "uv_not_found"


def test_the_first_call_syncs_and_the_next_reuses_the_environment(plugin):
    cold = plugin.run()
    envelope = one_envelope(cold)  # the stub's stdout noise went to stderr
    assert envelope["ok"] is True and envelope["output"]["accounts"] == []
    assert "sync noise" in cold.stderr
    (sync,) = plugin.syncs()
    assert "--frozen" in sync and "--no-dev" in sync and f"--project {plugin.root}" in sync
    lock = subprocess.run(
        ["cksum"], stdin=(plugin.root / "uv.lock").open("rb"), capture_output=True, check=True
    )
    assert plugin.stamp.read_bytes() == lock.stdout
    assert not list(plugin.venv.glob(".pulsar-lock.tmp.*"))

    warm = one_envelope(plugin.run())
    assert warm == envelope
    assert len(plugin.syncs()) == 1


def test_a_changed_lock_resyncs(plugin):
    one_envelope(plugin.run())
    with (plugin.root / "uv.lock").open("a") as lock:
        lock.write("\n# a new plugin version\n")
    assert one_envelope(plugin.run())["ok"] is True
    assert len(plugin.syncs()) == 2


def test_a_failed_sync_is_an_envelope_and_leaves_no_stamp(plugin):
    plugin.fail_flag.touch()
    result = plugin.run()
    envelope = one_envelope(result)
    assert envelope["ok"] is False and envelope["error"]["code"] == "dependency_sync"
    assert "sync refused" in result.stderr
    assert not plugin.stamp.exists()
    plugin.fail_flag.unlink()
    assert one_envelope(plugin.run())["ok"] is True  # the next call retries


def test_everything_the_launcher_creates_is_owner_only(plugin):
    assert one_envelope(plugin.run())["ok"] is True
    assert not (plugin.state / "home").exists()  # a read-only status creates no home
    for directory in (plugin.venv, plugin.state / "tmp", plugin.state / "pycache"):
        assert mode(directory) == 0o700, directory
    assert mode(plugin.stamp) == 0o600
    created = [p for p in plugin.state.rglob("*") if p.is_file() and not os.access(p, os.X_OK)]
    assert created and all(mode(p) & 0o077 == 0 for p in created), created


def test_bytecode_stays_out_of_the_plugin_root(plugin):
    assert one_envelope(plugin.run())["ok"] is True
    assert not list(plugin.root.rglob("__pycache__"))
    assert list((plugin.state / "pycache").rglob("*.pyc"))


def test_a_bad_request_still_gets_one_envelope(plugin):
    envelope = one_envelope(plugin.run({"schema_version": 1, "tool": "pulsar.nope"}))
    assert envelope["ok"] is False and envelope["error"]["code"] == "invalid_argument"
