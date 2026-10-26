"""Linux-only, non-root persistent dashboard control. No work on import.

The controller owns a private state directory; the server is a single detached
process, not a shell/daemon chain. Linux pidfds signal that exact process, never
a numeric PID or a possibly reused process group. No server subprocesses are
started. Same-UID/root path replacement is outside this isolation boundary.
"""
from __future__ import annotations

import sys

# Doctor must remain read-only even when run directly with Python -I (which
# ignores PYTHONDONTWRITEBYTECODE). Set this before any helper imports.
sys.dont_write_bytecode = True

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
if sys.version_info < (3, 11):
    raise SystemExit("Select installed Python >=3.11 using DASHBOARD_PYTHON; no interpreter is auto-switched.")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


# Reuse the existing default-NO Debian/Ubuntu prerequisite flow and loopback
# probes, without running smoke main() or adding a second package manager.
smoke = module("dashboard_smoke_util", ROOT / "scripts/smoke_test.py")
schema = module("dashboard_config_schema", ROOT / "src/world_intel_mcp/dashboard/settings.py")


class ManagerError(Exception):
    pass


def quoted_path(path):
    # Paths are useful operator diagnostics, but must not inject terminal/log
    # controls or extra lines. Do not resolve() an untrusted symlink to print it.
    return json.dumps(os.fspath(path), ensure_ascii=True)


def observed(path, info):
    return (f"path={quoted_path(path)}, mode={stat.S_IMODE(info.st_mode):04o}, "
            f"owner_uid={info.st_uid}, current_uid={os.geteuid()}")


def directory_problem(path, info, final):
    details = observed(path, info)
    if not stat.S_ISDIR(info.st_mode):
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "not a directory"
        return f"Unsafe directory type ({kind}): {details}; expected a directory, never a symlink"
    if info.st_uid not in (0, os.geteuid()):
        return f"Unsafe directory ownership: {details}; expected owner UID 0 or {os.geteuid()} for ancestors, UID {os.geteuid()} for the final state directory"
    if info.st_mode & 0o022 and (final or not info.st_mode & stat.S_ISVTX):
        writable = " and ".join(name for bit, name in ((0o020, "group-writable"), (0o002, "world-writable")) if info.st_mode & bit)
        role = "state directory" if final else "ancestor"
        expectation = "no group/world write bits on the final directory (even with sticky bit)" if final else "no group/world write bits unless this ancestor has the sticky bit"
        return f"Unsafe writable {role} ({writable}): {details}; expected {expectation}"
    if final and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        return f"Final directory is not UID-owned/private: {details}; expected owner UID {os.geteuid()} and no group/other permissions (normally 0700)"
    return None


def private_dir(path, create=False):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ManagerError(f"Noncanonical state path {quoted_path(path)}; expected an absolute path without '..'")
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                raise
            try:
                part.mkdir(mode=0o700)
                info = part.lstat()
            except OSError as exc:
                raise ManagerError(f"Cannot create/inspect directory: path={quoted_path(part)}, mode/owner unavailable, current_uid={os.geteuid()}, errno={exc.errno}; expected creation as a private 0700 directory; no permissions were changed") from None
        except OSError as exc:
            raise ManagerError(f"Cannot inspect directory: path={quoted_path(part)}, mode/owner unavailable, current_uid={os.geteuid()}, errno={exc.errno}; expected lstat access to a directory; no traversal below it") from None
        problem = directory_problem(part, info, final=part == path)
        if problem:
            raise ManagerError(problem)
    return path


def doctor(path):
    """Read-only lstat walk. Never inspect a child below an unsafe ancestor."""
    path = Path(path).absolute()
    print(f"State path (absolute; symlinks not resolved): {quoted_path(path)}")
    print("Read-only: no creation, locks, permission changes, credentials or installs.")
    if ".." in path.parts:
        print("UNSAFE: expected a canonical path without '..'; no traversal performed.")
        return 1
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            print(f"MISSING: {quoted_path(part)}; no mode/owner to inspect. Remaining components were not inspected.")
            print("Existing ancestors passed policy; setup would attempt missing directories with mode 0700. Creation/access has not been verified.")
            return 0
        except OSError as exc:
            print(f"UNVERIFIED: path={quoted_path(part)}, errno={exc.errno}, current_uid={os.geteuid()}; mode/owner unavailable. Cannot inspect this component; no traversal below it.")
            return 1
        problem = directory_problem(part, info, final=part == path)
        if problem:
            print("UNSAFE: " + problem)
            print("Stopped at this component; no links followed, repairs attempted or children inspected.")
            return 1
        print("PASS: " + observed(part, info))
    print("Existing state directory passes path policy. File contents, write access and deployment readiness were not checked.")
    return 0


def file_problem(path, info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "invalid type/owner/links/permissions"
        return f"Unsafe dashboard file ({kind}): {observed(path, info)}, hard_links={info.st_nlink}; expected a regular UID-owned file with one link and no group/other permissions (normally 0600)"
    return None


def open_private(path, flags=os.O_RDONLY):
    # Parents must already exist and pass the directory walk. Do not chmod an
    # existing file or follow a symlink/hardlink masquerading as a PID/log/lock.
    private_dir(Path(path).parent)
    try:
        problem = file_problem(path, Path(path).lstat())
    except FileNotFoundError:
        problem = None
    if problem:
        raise ManagerError(problem)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    problem = file_problem(path, info)
    if problem:
        os.close(fd)
        raise ManagerError(problem)
    return fd


def read_private(path):
    with os.fdopen(open_private(path), "rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ManagerError("Dashboard control file is too large")
    return data


def write_private(path, data):
    fd = open_private(path, os.O_WRONLY | os.O_CREAT)
    with os.fdopen(fd, "wb") as stream:
        stream.truncate(0)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def lock(state, name="control.lock", blocking=True):
    fd = open_private(state / name, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def config_path(state, path=None):
    result = Path(path).absolute() if path else state / "dashboard.toml"
    # Validate before the schema opens it. Same-UID races remain operator-trust.
    read_private(result)
    return result


def environment(state, config):
    return {
        "PATH": str(state / "venv/bin") + os.pathsep + os.defpath,
        "HOME": str(state / "home"), "XDG_CACHE_HOME": str(state / "cache"),
        "XDG_CONFIG_HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
        "WORLD_INTEL_CACHE_DB": str(state / "cache/dashboard.db"),
        "WORLD_INTEL_CONFIG": str(config), "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "LANG": "C.UTF-8",
    }


def identity(pid):
    try:
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.geteuid():
            return None
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start": fields[19], "command": (proc / "cmdline").read_bytes().split(b"\0")[:-1]}
    except (OSError, IndexError):
        return None


def matches(record):
    if not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 1:
        return False
    if (not isinstance(record.get("command"), list) or not all(isinstance(x, str) for x in record["command"])
            or not isinstance(record.get("token"), str) or not re.fullmatch(r"[0-9a-f]{48}", record["token"])
            or type(record.get("port")) is not int or not 1 <= record["port"] <= 65535):
        return False
    current = identity(record["pid"])
    if current is None:
        return False
    return (current["start"] == record.get("start")
            and [x.decode("utf-8", "surrogateescape") for x in current["command"]] == record.get("command")
            and "--token" in record.get("command", []) and record.get("token") in record.get("command", []))


def record_for(state):
    try:
        return json.loads(read_private(state / "server.json"))
    except FileNotFoundError:
        return None


def stop_record(record, grace=10):
    if not matches(record):
        return False
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return False
    try:
        if not matches(record):
            return False
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while matches(record) and time.monotonic() < deadline:
            time.sleep(0.05)
        if matches(record):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while matches(record) and time.monotonic() < deadline:
            time.sleep(0.05)
        if matches(record):
            raise ManagerError("Owned server has not exited after bounded shutdown")
        return True
    except ProcessLookupError:
        return True
    finally:
        os.close(fd)


def setup(state, refresh=False):
    previous = os.umask(0o077)
    try:
        return _setup(state, refresh)
    finally:
        os.umask(previous)


def _setup(state, refresh=False):
    for name in ("home", "cache", "tmp"):
        private_dir(state / name, create=True)
    default_config = state / "dashboard.toml"
    if not default_config.exists() and not default_config.is_symlink():
        fd = open_private(default_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as stream:
            stream.write((ROOT / "dashboard.example.toml").read_bytes())
    schema.load(config_path(state))
    venv = state / "venv"
    owner = state / "venv-owner.json"
    if venv.exists() or venv.is_symlink():
        private_dir(venv)
        if json.loads(read_private(owner)).get("repository") != str(ROOT):
            raise ManagerError("Refusing an existing venv not created for this checkout")
    else:
        if owner.exists() or owner.is_symlink():
            if json.loads(read_private(owner)).get("repository") != str(ROOT):
                raise ManagerError("Existing venv ownership marker belongs to another checkout")
        else:
            write_private(owner, json.dumps({"repository": str(ROOT)}).encode())
    ready = state / "ready.json"
    if ready.exists() or ready.is_symlink():
        if json.loads(read_private(ready)).get("repository") != str(ROOT):
            raise ManagerError("Existing setup marker belongs to another checkout")
    if not ready.exists() or refresh:
        if ready.exists():
            ready.unlink()  # validated managed marker, never cache/config/user data
        smoke.ensure_system_venv(python_selector="DASHBOARD_PYTHON")
        env = environment(state, default_config)
        logpath = state / "setup.log"
        # Setup logs are bounded by finite install stages, then truncated to a
        # sanitized tail. Runtime logging uses independent size-based rotation.
        with os.fdopen(open_private(logpath, os.O_WRONLY | os.O_CREAT), "wb") as log:
            log.truncate(0)
            try:
                smoke.run_stage("Create private dashboard venv", [sys.executable, "-I", "-m", "venv", str(venv)], state, env, log, 120)
                smoke.run_stage("Install dashboard only (official PyPI wheels)", [str(venv / "bin/python"), "-I", "-m", "pip", "install", "--force-reinstall", "--only-binary=:all:", "--no-cache-dir", "--retries", "1", "--timeout", "15", "--index-url", "https://pypi.org/simple", str(ROOT) + "[dashboard]", "python-dotenv>=1.2.0"], state, env, log, 600)
            finally:
                log.flush()
                tail = smoke.redacted_tail(logpath)
        write_private(logpath, tail.encode())
    # Isolation attestation also runs on reuse. No system/user packages or model SDKs.
    check = "import sys,importlib.util,pathlib; from dotenv import load_dotenv; import io; assert sys.prefix!=sys.base_prefix; assert pathlib.Path(sys.prefix).resolve()==pathlib.Path(sys.argv[1]).resolve(); assert not load_dotenv(stream=io.StringIO('SMOKE_TEST_SECRET=x')); assert not any(importlib.util.find_spec(n) for n in ('fastembed','qdrant_client','torch','weasyprint'))"
    try:
        subprocess.run([str(venv / "bin/python"), "-I", "-c", check, str(venv)], env=environment(state, default_config), cwd=state, check=True, timeout=30, stdout=subprocess.DEVNULL)
    except BaseException:
        if ready.exists():
            ready.unlink()
        raise
    write_private(ready, json.dumps({"repository": str(ROOT)}).encode())
    print(f"Setup ready. Private config: {default_config}. No existing config/cache was overwritten.")


def start(state, config, env_file=None, command_factory=None):
    existing = record_for(state)
    if matches(existing):
        raise ManagerError("Dashboard already running; use status/stop")
    settings = schema.load(config)
    try:
        smoke.check_free_port(settings.port)
    except smoke.SmokeError:
        raise ManagerError("Dashboard port is occupied; change port in the private TOML or select --config. No process was stopped.") from None
    token = secrets.token_hex(24)
    command = (command_factory(token) if command_factory else
               [str(state / "venv/bin/python"), "-I", str(SCRIPT), "_serve", "--state", str(state), "--config", str(config), "--token", token])
    if env_file:
        # Only the explicit private file is read, by the worker after dotenv
        # disable mode is set. It is never sourced or passed through a shell.
        read_private(env_file)
        command += ["--env-file", str(Path(env_file).absolute())]
    process = subprocess.Popen(command, cwd=state, env=environment(state, config),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, umask=0o077)
    record = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = identity(process.pid)
            if current and b"--token" in current["command"] and token.encode() in current["command"]:
                record = {**current, "command": [x.decode("utf-8", "surrogateescape") for x in current["command"]], "token": token, "port": settings.port}
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if record is None:
            raise ManagerError("Server failed before identity attestation; inspect logs")
        write_private(state / "server.json", json.dumps(record).encode())
        deadline = time.monotonic() + 30
        while matches(record) and time.monotonic() < deadline:
            if smoke.listener_addresses(process.pid, settings.port):
                try:
                    if smoke.probe(settings.port)[0] == 200:
                        smoke.check_dashboard(settings.port)
                        print(f"Running on 127.0.0.1:{settings.port}; PID {process.pid}. SSH disconnect will not stop it.")
                        print(f"Run scripts/smoke-tunnel.sh locally; remote port {settings.port}.")
                        return record
                except (OSError, ValueError):
                    pass
            time.sleep(0.1)
        raise ManagerError("Server did not become ready within 30s; inspect dashboard.sh logs")
    except BaseException:
        if record:
            stop_record(record)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        raise


CREDENTIALS = {"ACLED_EMAIL", "ACLED_PASSWORD", "UCDP_ACCESS_TOKEN", "FRED_API_KEY", "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"}


def credentials(path):
    from dotenv import dotenv_values
    import io
    values = dotenv_values(stream=io.StringIO(read_private(path).decode()), interpolate=False)
    if set(values) - CREDENTIALS or any(not isinstance(v, str) for v in values.values()):
        raise ManagerError("Credential file contains unsupported keys or empty assignments")
    return values


def log_handler(state, hidden_values=()):
    logfile = state / "dashboard.log"
    for path in [logfile, *[state / ("dashboard.log." + str(n)) for n in range(1, 4)]]:
        if path.exists() or path.is_symlink():
            os.close(open_private(path))
    class PrivateRotation(RotatingFileHandler):
        def _open(self):
            return os.fdopen(open_private(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND), "a", encoding="utf-8")
    class SafeLog(logging.Filter):
        def filter(self, record):
            message = record.getMessage()
            for value in hidden_values:
                message = message.replace(value, "[redacted]")
            message = re.sub(r"https?://\S+", "[url]", message)
            message = "".join(c if c in "\n\t" or c.isprintable() else "?" for c in message)
            record.msg, record.args, record.exc_info, record.exc_text = message, (), None, None
            return True
    handler = PrivateRotation(logfile, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.addFilter(SafeLog())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return handler


def serve(state, config, token, env_file):
    with lock(state, "server.lock", blocking=False):
        os.environ["WORLD_INTEL_CONFIG"] = str(config)
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        hidden_values = []
        handler = log_handler(state, hidden_values)
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        if env_file:
            try:
                values = credentials(env_file)
            except Exception:
                logging.error("Credential file rejected: require private UTF-8 dotenv with only documented credential keys")
                raise
            os.environ.update(values)
            hidden_values.extend(v for v in values.values() if v)
        # No raw upstream library diagnostics in persistent logs.
        for name in ("world_intel_mcp", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        import uvicorn
        from world_intel_mcp.dashboard.app import app, _settings
        if _settings is None:
            raise ManagerError("Refusing to start without a focused profile")
        logging.info("Focused dashboard starting; optional services disabled")
        uvicorn.run(app, host="127.0.0.1", port=_settings.port, workers=1,
                    proxy_headers=False, access_log=False, log_config=None, timeout_graceful_shutdown=5)
        logging.info("Focused dashboard stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent focused loopback dashboard (Linux, non-root). No automatic reboot service.")
    parser.add_argument("action", nargs="?", choices=("setup", "start", "stop", "status", "logs", "doctor", "_serve"))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "world-intel-dashboard")
    parser.add_argument("--config", type=Path, default=Path(os.environ["WORLD_INTEL_CONFIG"]) if os.environ.get("WORLD_INTEL_CONFIG") else None)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--refresh", action="store_true", help="setup only: reinstall this checkout in its managed venv")
    parser.add_argument("--follow", action="store_true", help="logs only: explicitly keep following until Ctrl+C")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11) or sys.platform != "linux" or os.geteuid() == 0:
        raise ManagerError("Requires Linux and installed Python >=3.11; never run this script with sudo/root")
    if not args.action:
        if not sys.stdin.isatty():
            parser.error("Choose setup/start/stop/status/logs/doctor")
        print("1 Setup   2 Start   3 Stop   4 Status   5 Logs (bounded tail)   6 Doctor (read-only)")
        try:
            choice = input("Choose [4]: ").strip() or "4"
        except EOFError:
            return 1
        args.action = {"1": "setup", "2": "start", "3": "stop", "4": "status", "5": "logs", "6": "doctor"}.get(choice)
        if args.action is None:
            parser.error("Invalid menu selection")
    if args.action == "doctor":
        return doctor(args.state)  # before pidfd probes, path creation or locking
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ManagerError("This Linux/Python needs pidfd support for safe persistent process control")
    os.close(os.pidfd_open(os.getpid()))  # Require kernel support before any launch.
    if not args.state.exists() and args.action != "setup":
        print("Not set up; run scripts/dashboard.sh setup first.")
        return 1
    state = private_dir(args.state, create=args.action == "setup")
    if args.action == "_serve":
        if not args.token:
            raise ManagerError("Missing worker identity token")
        serve(state, config_path(state, args.config), args.token, args.env_file)
        return 0
    def interrupted(*_):
        raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    with lock(state, blocking=False):
        if args.action == "setup":
            if matches(record_for(state)):
                raise ManagerError("Stop the dashboard before setup/refresh")
            setup(state, args.refresh)
        elif args.action == "start":
            if json.loads(read_private(state / "ready.json")).get("repository") != str(ROOT):
                raise ManagerError("Run setup for this checkout before start")
            start(state, config_path(state, args.config), args.env_file)
        elif args.action == "stop":
            stopped = stop_record(record_for(state))
            print("Stopped owned server; cache/config/logs retained." if stopped else "Not running; no process was signalled.")
        elif args.action == "status":
            record = record_for(state)
            if not matches(record):
                print("Not running (stale/missing identity); no process was signalled.")
                return 1
            try:
                ready = smoke.listener_addresses(record["pid"], record["port"]) and smoke.probe(record["port"])[0] == 200
            except OSError:
                ready = False
            print(f"PID {record['pid']}: {'running, loopback ready' if ready else 'running, not ready'}")
            return 0 if ready else 1
        elif args.action == "logs":
            path = state / "dashboard.log"
            with os.fdopen(open_private(path), "rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                print(stream.read().decode("utf-8", "replace"))
    # Following must not hold the control lock and prevent a separate stop.
    if args.action == "logs" and args.follow:
        position = path.stat().st_size
        inode = (path.stat().st_dev, path.stat().st_ino)
        while True:
            time.sleep(0.5)
            with os.fdopen(open_private(path), "rb") as stream:
                info = os.fstat(stream.fileno())
                current_inode = (info.st_dev, info.st_ino)
                if current_inode != inode or info.st_size < position:
                    position = 0
                inode = current_inode
                stream.seek(position)
                text = stream.read(65536)
                position = stream.tell()
                if text:
                    print(text.decode("utf-8", "replace"), end="", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Controller interrupted; use status/stop to inspect a detached server.", file=sys.stderr)
        raise SystemExit(130)
    except (ManagerError, smoke.SmokeError, schema.ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except BlockingIOError:
        print("Another dashboard control action is already in progress; try status again when it finishes.", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Do not print arbitrary exception strings carrying credential paths/URLs.
        print("Dashboard action failed. Check prerequisites, private file permissions, configuration and logs. No unrelated process was signalled.", file=sys.stderr)
        raise SystemExit(1)
