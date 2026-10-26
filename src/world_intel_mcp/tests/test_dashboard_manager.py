"""Controller tests: private temporary state and fake loopback HTTP only.

Never invokes setup's package installation, upstream sources or deployment app.
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("dashboard_manager_test", ROOT / "scripts/dashboard_manager.py")
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


FAKE_SERVER = '''
import argparse, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p=argparse.ArgumentParser()
p.add_argument('--port',type=int); p.add_argument('--token'); a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  status=400 if self.headers.get('Host')=='outside.example' else 403 if self.headers.get('Origin')=='https://outside.example' else 200
  data=b'<html>fake dashboard</html>' if self.path=='/' else b'{"status":"ok"}'
  self.send_response(status); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
'''


@pytest.fixture
def state(tmp_path):
    result = manager.private_dir(tmp_path / "private state", create=True)
    for name in ("cache", "home", "tmp"):
        manager.private_dir(result / name, create=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    manager.write_private(result / "dashboard.toml", f"version=1\nport={port}\n".encode())
    return result


def test_private_paths_files_links_and_modes(state, tmp_path):
    assert state.stat().st_mode & 0o777 == 0o700
    path = state / "file"
    manager.write_private(path, b"preserve")
    assert path.stat().st_mode & 0o777 == 0o600
    (state / "symlink").symlink_to(path)
    with pytest.raises((manager.ManagerError, OSError)):
        manager.read_private(state / "symlink")
    os.link(path, state / "hardlink")
    with pytest.raises(manager.ManagerError):
        manager.write_private(path, b"must not overwrite")
    assert path.read_bytes() == b"preserve"
    (tmp_path / "dirlink").symlink_to(state, target_is_directory=True)
    with pytest.raises(manager.ManagerError):
        manager.private_dir(tmp_path / "dirlink")


def test_single_instance_control_lock(state):
    with manager.lock(state):
        with pytest.raises(BlockingIOError):
            with manager.lock(state, blocking=False):
                pytest.fail("Second lock unexpectedly acquired")


def test_clean_child_environment_and_credentials_not_inherited(state, monkeypatch):
    monkeypatch.setenv("ACLED_PASSWORD", "DUMMY_PRIVATE_SECRET")
    monkeypatch.setenv("HTTP_PROXY", "DUMMY_PRIVATE_SECRET")
    monkeypatch.setenv("PYTHONPATH", "/arbitrary/source")
    env = manager.environment(state, state / "dashboard.toml")
    assert "DUMMY_PRIVATE_SECRET" not in repr(env) and "PYTHONPATH" not in env
    assert env["WORLD_INTEL_CACHE_DB"] == str(state / "cache/dashboard.db")
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


def test_real_fake_server_lifecycle_pid_identity_port_conflict_and_persistence(state):
    if not hasattr(os, "pidfd_open"):
        pytest.skip("Linux pidfds unavailable")
    fake = state / "fake server.py"
    fake.write_text(FAKE_SERVER)
    port = manager.schema.load(state / "dashboard.toml").port
    command = lambda token: [sys.executable, "-I", str(fake), "--port", str(port), "--token", token]
    cache = state / "cache/dashboard.db"
    cache.write_bytes(b"persistent-test-state")
    with contextlib.redirect_stdout(io.StringIO()):
        record = manager.start(state, state / "dashboard.toml", command_factory=command)
    try:
        assert manager.matches(record)
        assert os.getpgid(record["pid"]) == record["pid"]  # detached from caller/SSH group
        assert manager.smoke.listener_addresses(record["pid"], port)
        assert manager.record_for(state) == record
        if os.geteuid() != 0:
            old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    assert manager.main(["status", "--state", str(state)]) == 0
                    manager.write_private(state / "dashboard.log", b"fake lifecycle log\n")
                    assert manager.main(["logs", "--state", str(state)]) == 0
                assert "loopback ready" in output.getvalue() and "fake lifecycle log" in output.getvalue()
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        assert (state / "server.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(manager.ManagerError, match="already running"):
            manager.start(state, state / "dashboard.toml", command_factory=command)
        altered = {**record, "start": "wrong"}
        assert not manager.stop_record(altered, grace=0.1)
        assert manager.matches(record)
        altered = {**record, "token": "0" * 48}
        assert not manager.stop_record(altered, grace=0.1)
        assert manager.matches(record)
        with pytest.raises(manager.smoke.SmokeError, match="unavailable"):
            manager.smoke.check_free_port(port)
        assert manager.stop_record(record, grace=1)
        assert not manager.matches(record)
        assert cache.read_bytes() == b"persistent-test-state"
        assert (state / "dashboard.toml").is_file()
    finally:
        manager.stop_record(record, grace=1)
        try:
            os.waitpid(record["pid"], os.WNOHANG)
        except ChildProcessError:
            pass


@pytest.mark.parametrize("record", [None, {}, {"pid": True}, {"pid": os.getpid(), "command": 7},
                                    {"pid": os.getpid(), "command": [], "token": "x", "port": 8501}])
def test_malformed_identity_never_signals(record, monkeypatch):
    monkeypatch.setattr(manager.signal, "pidfd_send_signal", lambda *a: pytest.fail("Unverified process signalled"))
    assert not manager.stop_record(record)


def test_interrupt_during_readiness_closes_only_owned_fake_server(state, monkeypatch):
    fake = state / "interrupted server.py"
    fake.write_text(FAKE_SERVER)
    port = manager.schema.load(state / "dashboard.toml").port
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(manager.smoke, "probe", interrupted)
    with pytest.raises(KeyboardInterrupt):
        manager.start(state, state / "dashboard.toml", command_factory=lambda token: [sys.executable, "-I", str(fake), "--port", str(port), "--token", token])
    record = manager.record_for(state)
    assert record and not manager.matches(record)
    assert (state / "dashboard.toml").is_file()


def test_status_before_setup_does_not_create_state(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("CLI deliberately refuses root")
    state = tmp_path / "not created"
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        assert manager.main(["status", "--state", str(state)]) == 1
        assert not state.exists()
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def test_setup_commands_are_isolated_wheel_only_and_config_not_overwritten(state, monkeypatch):
    calls = []
    original = (state / "dashboard.toml").read_bytes()
    def stage(label, command, *args):
        calls.append(command)
        if "venv" in command:
            manager.private_dir(state / "venv", create=True)
    monkeypatch.setattr(manager.smoke, "ensure_system_venv", lambda **kwargs: None)
    monkeypatch.setattr(manager.smoke, "run_stage", stage)
    monkeypatch.setattr(manager.subprocess, "run", lambda *a, **k: None)
    manager.setup(state)
    assert (state / "dashboard.toml").read_bytes() == original
    assert len(calls) == 2
    assert str(ROOT) + "[dashboard]" in calls[1]
    assert "--only-binary=:all:" in calls[1]
    assert not any("dev" in v or "vector" in v or "pdf" in v for v in calls[1])
    assert (state / "setup.log").stat().st_mode & 0o777 == 0o600
    manager.setup(state)
    assert len(calls) == 2  # safe reuse, no reinstall


def test_preexisting_unowned_venv_and_unsafe_pid_or_log_are_not_overwritten(state):
    manager.private_dir(state / "venv", create=True)
    with pytest.raises(FileNotFoundError):
        manager.setup(state)
    target = state / "private-other-file"
    manager.write_private(target, b"user work")
    for name in ("control.lock", "server.json", "dashboard.log"):
        (state / name).symlink_to(target)
        with pytest.raises((manager.ManagerError, OSError)):
            manager.read_private(state / name)
    assert target.read_bytes() == b"user work"


def test_failed_refresh_or_attestation_cannot_leave_a_ready_marker(state, monkeypatch):
    manager.private_dir(state / "venv", create=True)
    marker = json.dumps({"repository": str(ROOT)}).encode()
    manager.write_private(state / "venv-owner.json", marker)
    manager.write_private(state / "ready.json", marker)
    original = (state / "dashboard.toml").read_bytes()
    monkeypatch.setattr(manager.smoke, "ensure_system_venv", lambda **kwargs: None)
    monkeypatch.setattr(manager.smoke, "run_stage", lambda *a, **k: None)
    def failed(*a, **k):
        raise subprocess.CalledProcessError(1, "isolation check")
    monkeypatch.setattr(manager.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        manager.setup(state, refresh=True)
    assert not (state / "ready.json").exists()
    assert (state / "dashboard.toml").read_bytes() == original


def test_wrapper_works_from_scripts_directory_and_does_not_eval_python(tmp_path):
    executable = tmp_path / "python with spaces"
    executable.write_text(f"#!{sys.executable}\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)
    result = subprocess.run(["bash", str(ROOT / "scripts/dashboard.sh"), "status"], cwd=ROOT / "scripts",
                            env={"PATH": os.defpath, "DASHBOARD_PYTHON": str(executable)},
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == ["-I", "-B", str(ROOT / "scripts/dashboard_manager.py"), "status"]


def test_rotation_redaction_and_permissions_do_not_depend_on_parent_umask(state):
    handler = manager.log_handler(state, ["DUMMY_SECRET"])
    try:
        for n in range(1100):
            handler.handle(logging.LogRecord("test", logging.INFO, "", 0,
                                             "DUMMY_SECRET https://user:pass@example.test/path?token=abc \x1b " + "x" * 4000, (), None))
        handler.flush()
    finally:
        handler.close()
    logs = list(state.glob("dashboard.log*"))
    assert len(logs) == 4
    for log in logs:
        assert log.stat().st_mode & 0o777 == 0o600
        assert log.stat().st_size <= 1024 * 1024
        text = log.read_text()
        assert "DUMMY_SECRET" not in text and "token=abc" not in text and "\x1b" not in text


def test_explicit_credentials_are_known_keys_not_shell_or_interpolated(state, monkeypatch):
    file = state / "credentials.env"
    monkeypatch.setenv("PRIVATE_VALUE", "MUST_NOT_INTERPOLATE")
    manager.write_private(file, b"FRED_API_KEY=${PRIVATE_VALUE}\nACLED_PASSWORD=$(touch /never-run)\n")
    values = manager.credentials(file)
    assert values["FRED_API_KEY"] == "${PRIVATE_VALUE}"
    assert values["ACLED_PASSWORD"] == "$(touch /never-run)"
    manager.write_private(file, b"HTTP_PROXY=http://untrusted\n")
    with pytest.raises(manager.ManagerError, match="unsupported"):
        manager.credentials(file)
