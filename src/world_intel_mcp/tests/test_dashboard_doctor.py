"""Read-only local diagnostics; never repairs permissions or runs installers."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("dashboard_doctor_test", ROOT / "scripts/dashboard_manager.py")
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


@pytest.mark.parametrize("entrypoint", ["launcher", "direct-isolated-python"])
@pytest.mark.parametrize("unsafe", [False, True])
def test_fresh_checkout_cli_never_writes_bytecode_or_state(tmp_path, entrypoint, unsafe):
    checkout = tmp_path / "checkout"
    shutil.copytree(ROOT / "scripts", checkout / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    settings = checkout / "src/world_intel_mcp/dashboard/settings.py"
    settings.parent.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "src/world_intel_mcp/dashboard/settings.py", settings)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    ancestor = home / "state-parent"
    ancestor.mkdir(mode=0o700)
    if unsafe:
        ancestor.chmod(0o777)  # Disposable fixture only; doctor must not repair it.
    state = ancestor / "not-created"

    def snapshot():
        return {str(p.relative_to(tmp_path)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")}

    assert not list(checkout.rglob("__pycache__"))
    before = snapshot()
    # No bytecode environment opt-out: -I ignores it anyway. The direct path
    # deliberately omits -B to exercise the manager's own early safeguard.
    env = {"PATH": os.defpath, "HOME": str(home), "XDG_STATE_HOME": str(ancestor),
           "DASHBOARD_PYTHON": sys.executable}
    command = (["bash", str(checkout / "scripts/dashboard.sh")] if entrypoint == "launcher"
               else [sys.executable, "-I", str(checkout / "scripts/dashboard_manager.py")])
    result = subprocess.run([*command, "doctor", "--state", str(state)], cwd=checkout,
                            env=env, capture_output=True, text=True, timeout=10)
    assert snapshot() == before, result.stdout + result.stderr
    assert not list(checkout.rglob("__pycache__")) and not state.exists()
    if os.geteuid() == 0:
        assert result.returncode != 0 and "never run" in result.stderr
    elif unsafe:
        assert result.returncode != 0
        assert "UNSAFE:" in result.stdout and json.dumps(str(ancestor)) in result.stdout
        assert "world-writable" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert "MISSING:" in result.stdout and json.dumps(str(state)) in result.stdout


@pytest.mark.parametrize("mode,reason", [(0o770, "group-writable"), (0o707, "world-writable"), (0o777, "group-writable and world-writable")])
def test_writable_ancestor_reports_exact_component_without_creating_child(tmp_path, capsys, mode, reason):
    ancestor = tmp_path / "shared ancestor"
    ancestor.mkdir(mode=0o700)
    ancestor.chmod(mode)
    state = ancestor / "state"
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(state, create=True)
    text = str(error.value)
    for expected in (json.dumps(str(ancestor)), f"mode={mode:04o}", f"owner_uid={os.geteuid()}", f"current_uid={os.geteuid()}", reason, "ancestor", "expected"):
        assert expected in text
    assert manager.doctor(state) == 1
    assert text in capsys.readouterr().out
    assert not state.exists() and stat.S_IMODE(ancestor.stat().st_mode) == mode


@pytest.mark.parametrize("mode,reason", [(0o755, "not UID-owned/private"), (0o777, "writable state directory"), (0o1777, "writable state directory")])
def test_final_directory_private_policy_is_unchanged(tmp_path, mode, reason):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(mode)
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(state)
    assert reason in str(error.value) and f"mode={mode:04o}" in str(error.value)
    assert json.dumps(str(state)) in str(error.value)
    assert manager.doctor(state) == 1
    assert stat.S_IMODE(state.stat().st_mode) == mode


def test_sticky_shared_ancestor_remains_allowed(tmp_path):
    ancestor = tmp_path / "sticky"
    ancestor.mkdir(mode=0o700)
    ancestor.chmod(0o1777)
    state = ancestor / "state"
    state.mkdir(mode=0o700)
    assert manager.private_dir(state) == state
    assert manager.doctor(state) == 0


@pytest.mark.parametrize("owner,reason", [(0, "not UID-owned/private"), (34567, "Unsafe directory ownership")])
def test_root_and_unrelated_owners_are_reported_without_chown(tmp_path, monkeypatch, capsys, owner, reason):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    original = Path.lstat
    monkeypatch.setattr(manager.os, "geteuid", lambda: 12345)
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        # Simulate root-owned ancestors plus the selected final owner, without
        # altering any real UID or permissions on the test machine.
        return SimpleNamespace(st_mode=info.st_mode, st_uid=owner if path == state else 0)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(state)
    assert reason in str(error.value)
    assert f"owner_uid={owner}" in str(error.value) and "current_uid=12345" in str(error.value)
    assert manager.doctor(state) == 1
    assert str(error.value) in capsys.readouterr().out


def test_symlink_walk_stops_before_child_and_never_resolves_target(tmp_path, monkeypatch, capsys):
    target = tmp_path / "private target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    state = link / "child"
    visited = []
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        visited.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("Doctor must not resolve state links"))
    assert manager.doctor(state) == 1
    text = capsys.readouterr().out
    assert "symlink" in text and json.dumps(str(link)) in text
    assert state not in visited and target not in visited
    assert not (target / "child").exists()


def test_non_directory_has_type_mode_owner_and_expectation(tmp_path):
    path = tmp_path / "file"
    path.write_text("untouched")
    path.chmod(0o600)
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(path)
    assert "not a directory" in str(error.value) and "mode=0600" in str(error.value)
    assert "expected a directory" in str(error.value)
    assert path.read_text() == "untouched"


def test_uninspectable_component_reports_path_errno_and_stops(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "blocked"
    state = blocked / "state"
    visited = []
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        visited.append(path)
        if path == blocked:
            raise PermissionError(13, "UNSAFE_RAW_MESSAGE\n\x1b")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    assert manager.doctor(state) == 1
    text = capsys.readouterr().out
    assert "UNVERIFIED:" in text and "errno=13" in text and json.dumps(str(blocked)) in text
    assert "UNSAFE_RAW_MESSAGE" not in text and state not in visited
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(state, create=True)
    assert "errno=13" in str(error.value) and json.dumps(str(blocked)) in str(error.value)
    assert "UNSAFE_RAW_MESSAGE" not in str(error.value)


def test_paths_with_terminal_controls_are_quoted_on_one_line(tmp_path, capsys):
    state = tmp_path / 'bad\n\x1b[31m\u202ename"'
    state.mkdir(mode=0o700)
    state.chmod(0o777)
    assert manager.doctor(state) == 1
    text = capsys.readouterr().out
    assert json.dumps(str(state), ensure_ascii=True) in text
    assert str(state) not in text and "\x1b" not in text and "\u202e" not in text
    with pytest.raises(manager.ManagerError) as error:
        manager.private_dir(state)
    assert "\n" not in str(error.value) and "\x1b" not in str(error.value)


def test_doctor_missing_path_runs_before_guards_and_never_mutates(tmp_path, monkeypatch, capsys):
    state = tmp_path / "not-created" / "state"
    secret = tmp_path / "credentials.env"
    secret.write_text("FRED_API_KEY=DUMMY_PRIVATE_SECRET\n")
    before = {p: (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")}
    def forbidden(*a, **k):
        pytest.fail("Doctor attempted a write, lock, install, probe or credential/config read")
    for name in ("private_dir", "lock", "setup", "read_private", "credentials", "write_private"):
        monkeypatch.setattr(manager, name, forbidden)
    monkeypatch.setattr(manager.smoke, "ensure_system_venv", forbidden)
    monkeypatch.setattr(manager.schema, "load", forbidden)
    monkeypatch.setattr(manager.os, "pidfd_open", forbidden)
    monkeypatch.setattr(manager.os, "chmod", forbidden)
    monkeypatch.setattr(manager.subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(manager.os, "geteuid", lambda: os.getuid())
    if os.getuid() == 0:
        pytest.skip("Launcher deliberately refuses root")
    assert manager.main(["doctor", "--state", str(state), "--env-file", str(secret), "--config", str(tmp_path / "not-read.toml")]) == 0
    text = capsys.readouterr().out
    assert "MISSING:" in text and "0700" in text and "not been verified" in text
    assert "DUMMY_PRIVATE_SECRET" not in text and "not-read.toml" not in text
    after = {p: (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")}
    assert before == after and not state.parent.exists()


def test_readonly_cli_uses_xdg_default_and_does_not_dump_environment(tmp_path):
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "xdg"),
           "DASHBOARD_PYTHON": sys.executable, "FRED_API_KEY": "DUMMY_PRIVATE_SECRET"}
    result = subprocess.run(["bash", str(ROOT / "scripts/dashboard.sh"), "doctor"], env=env,
                            cwd=ROOT / "scripts", capture_output=True, text=True, timeout=10)
    if os.geteuid() == 0:
        assert result.returncode != 0
        return
    assert result.returncode == 0, result.stderr
    assert json.dumps(str(tmp_path / "xdg/world-intel-dashboard")) in result.stdout
    assert "DUMMY_PRIVATE_SECRET" not in result.stdout and not (tmp_path / "xdg").exists()


def test_private_file_errors_are_quoted_and_leave_links_untouched(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / "user data"
    target.write_text("preserve")
    target.chmod(0o600)
    link = state / "link"
    link.symlink_to(target)
    with pytest.raises(manager.ManagerError) as error:
        manager.read_private(link)
    assert "symlink" in str(error.value) and json.dumps(str(link)) in str(error.value)
    assert "owner_uid=" in str(error.value) and "current_uid=" in str(error.value)
    assert target.read_text() == "preserve"
