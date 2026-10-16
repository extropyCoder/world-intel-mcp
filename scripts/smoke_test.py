"""Disposable headless smoke runner. Stdlib bootstrap; project imports are child-only.

No live smoke runs on import. Workers run only in the newly created venv with
an allowlisted environment. Linux /proc is required to attest the listener PID.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve()
FORBIDDEN = ("qdrant_client", "fastembed", "weasyprint", "torch", "transformers", "onnxruntime")


class SmokeError(Exception):
    pass


def port(value):
    if not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return int(value)


def system_environment():
    """Do not pass repository/Python/loader/APT overrides into system tools."""
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "TERM": "dumb"}


def venv_prerequisites_ready():
    # Check the *same* selected interpreter in isolated mode, without invoking
    # ensurepip.bootstrap() or modifying its installed packages.
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import venv, ensurepip; ensurepip.version()"],
        cwd="/", env=system_environment(), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=15,
    )
    return result.returncode == 0


def venv_package_name():
    major, minor = sys.version_info[:2]
    if type(major) is not int or type(minor) is not int or major < 3 or minor < 0:
        raise SmokeError("Invalid selected Python version")
    return f"python{major}.{minor}-venv"


def debian_family():
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        return False
    return bool(({release.get("ID", "")} | set(release.get("ID_LIKE", "").split())) & {"debian", "ubuntu"})


def trusted_system_tool(name):
    if name not in ("apt-get", "apt-cache", "sudo"):
        raise SmokeError("Unsupported system prerequisite tool")
    candidate = Path("/usr/bin") / name
    try:
        resolved = candidate.resolve(strict=True)
        for directory in resolved.parents:
            info = directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SmokeError(f"Untrusted system executable directory for {name}")
        info = resolved.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or not os.access(resolved, os.X_OK)):
            raise SmokeError(f"Untrusted system executable: {name}")
    except OSError as exc:
        raise SmokeError(f"Required system executable /usr/bin/{name} is unavailable") from exc
    return str(resolved)


def query_system_tool(command):
    result = subprocess.run(command, cwd="/", env=system_environment(),
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise SmokeError("APT prerequisite inspection failed; no package install was attempted")
    return result.stdout


def validate_venv_plan(plan, package):
    # Never install/upgrade an interpreter as a side effect, nor upgrade/remove
    # other packages. Allow only the venv package and missing wheel archives it
    # may need. APT's already-configured repositories remain operator-trusted.
    allowed = {package, "python3-pip-whl", "python3-setuptools-whl", "python3-wheel-whl"}
    for line in plan.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            raise SmokeError("APT proposes removals; refusing automatic prerequisite installation")
        if line.startswith(("Inst ", "Conf ")):
            parts = line.split()
            if len(parts) < 2 or parts[1] not in allowed:
                raise SmokeError("APT proposes unrelated packages or Python interpreter changes; ask an administrator")
            if line.startswith("Inst ") and "[" in line.split("(", 1)[0]:
                raise SmokeError("APT proposes a package upgrade; refusing automatic upgrades")


def install_system_package(command):
    # Keep the terminal/session for sudo authentication and normal APT SIGINT
    # handling. Never elevate a repository script, shell, pip or env wrapper.
    process = subprocess.Popen(command, cwd="/", env=system_environment(), umask=0o022)
    try:
        code = process.wait()
    except BaseException:
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGINT)
                except (ProcessLookupError, PermissionError):
                    pass  # sudo/terminal may already have forwarded the signal.
            # Do not orphan a privileged transaction or force-kill dpkg midway.
            process.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        raise
    if code:
        raise SmokeError(f"System venv package installation failed (exit {code}); no smoke state was started")


def ensure_system_venv():
    print(f"Selected Python {sys.version_info.major}.{sys.version_info.minor}: {sys.executable}; checking venv/ensurepip with -I.", flush=True)
    if sys.version_info >= (3, 14):
        print("Python 3.14+ is not fully end-to-end smoke-tested here; venv support does not guarantee all dependency wheels. If installation is later blocked, explicitly set SMOKE_PYTHON=/path/to/an/already-installed/python3.12; no interpreter is auto-selected.", flush=True)
    if venv_prerequisites_ready():
        print("PASS: selected interpreter provides venv/ensurepip.", flush=True)
        return
    package = venv_package_name()
    manual = f"Required OS package: {package}. An administrator can use apt-get install -- {package} if available."
    print("Selected interpreter's venv/ensurepip is unavailable.", flush=True)
    if not debian_family():
        raise SmokeError("Automatic OS prerequisite setup is supported only on Debian/Ubuntu. " + manual)
    if not sys.stdin.isatty():
        raise SmokeError("Noninteractive run: no OS packages installed. Rerun in a terminal for the default-NO prompt. " + manual)
    print(f"Can install only {package} and any missing pip/setuptools/wheel archives it requires.", flush=True)
    print("APT may contact configured repositories, but no Python installation/upgrades, index update, repositories or PPAs will be added. sudo may request your password.", flush=True)
    print("This may repair only distro Python; the selected interpreter will be rechecked, never switched.", flush=True)
    try:
        answer = input(f"Install the system venv prerequisite {package}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise SmokeError("Prerequisite installation declined; no OS packages installed. " + manual)
    apt = trusted_system_tool("apt-get")
    apt_cache = trusted_system_tool("apt-cache")
    sudo = None if os.geteuid() == 0 else trusted_system_tool("sudo")
    policy = query_system_tool([apt_cache, "policy", package])
    candidate = re.search(r"^\s*Candidate:\s*(\S+)\s*$", policy, re.M)
    if candidate is None or candidate.group(1) == "(none)":
        raise SmokeError(f"No APT candidate for {package} in the configured indexes. No install/update/repository changes attempted. Ask an administrator or explicitly select another installed Python via SMOKE_PYTHON.")
    flags = ["install", "--no-install-recommends", "--no-upgrade", "--no-remove"]
    plan = query_system_tool([apt, "--simulate", *flags, "--", package])
    validate_venv_plan(plan, package)
    command = [apt, *flags, "-y", "--", package]
    if sudo is not None:
        command = [sudo, "--", *command]
    print(f"Installing approved prerequisite {package}; waiting for the system package manager to finish.", flush=True)
    install_system_package(command)
    if not venv_prerequisites_ready():
        raise SmokeError(f"{package} installation completed but the selected interpreter still lacks venv/ensurepip. No smoke state started and Python was not switched. For a custom or wheel-incompatible Python 3.14, explicitly choose an existing supported interpreter (for example SMOKE_PYTHON=/path/to/python3.12).")
    print("PASS: selected interpreter now provides venv/ensurepip; continuing isolated setup.", flush=True)


def check_directory(path, *, private=False):
    """Check the canonical pathname, without accepting symlink ancestors."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise SmokeError("Smoke directory must use a canonical path")
    for current in [*reversed(path.parents), path]:
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise SmokeError("Unsafe smoke directory type, symlink or ownership")
        if info.st_mode & 0o022 and (current == path and private or not info.st_mode & stat.S_ISVTX):
            raise SmokeError("Unsafe writable smoke directory")
    info = path.lstat()
    if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise SmokeError("Smoke state must be owned by this UID and private (0700)")
    return path


def clean_environment(state):
    """No inherited API keys, proxies, pip config, Python paths or AOI settings."""
    state = check_directory(state, private=True)
    for name in ("home", "cache", "config", "tmp"):
        (state / name).mkdir(mode=0o700)
    return {
        "PATH": str(state / "venv/bin") + os.pathsep + os.defpath,
        "HOME": str(state / "home"), "XDG_CACHE_HOME": str(state / "cache"),
        "XDG_CONFIG_HOME": str(state / "config"), "TMPDIR": str(state / "tmp"),
        "WORLD_INTEL_CACHE_DB": str(state / "cache/dashboard.db"),
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "OLLAMA_API_URL": "smoke-disabled://ollama", "OLLAMA_MODEL": "smoke-no-model",
        "_WORLD_INTEL_SMOKE_STATE": str(state), "LANG": "C.UTF-8",
    }


def stop_child(process):
    # A second Ctrl+C must not interrupt cleanup and leave the owned server or
    # build/test descendants behind. Parent cleanup remains bounded.
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        _stop_child_group(process)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stop_child_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process.poll()  # Reap the leader while checking for surviving descendants.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def start_child(command, state, env, log):
    return subprocess.Popen(command, cwd=state, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)


def run_stage(label, command, state, env, log, timeout):
    print(f"{label} (deadline {timeout}s)", flush=True)
    process = start_child(command, state, env, log)
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SmokeError(f"{label}: timed out") from exc
        if code:
            raise SmokeError(f"{label}: failed (exit {code}); see sanitized log below")
        print(f"PASS: {label}", flush=True)
    finally:
        stop_child(process)


def redacted_tail(path):
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 12000))
        text = stream.read().decode("utf-8", "replace")
    # Best-effort diagnostics, not arbitrary free-text secret detection. The
    # subprocess environment contains no inherited credentials in the first place.
    def origin(match):
        try:
            url = urlsplit(match.group())
            return f"{url.scheme}://{url.hostname or '[redacted]'}"
        except ValueError:
            return "[redacted-url]"
    text = re.sub(r"https?://[^\s<>]+", origin, text)
    text = re.sub(r"(?im)(authorization|api[_-]?key|password|secret|token)\s*[:=].*$", r"\1=[redacted]", text)
    text = "".join(c if c in "\n\t" or c.isprintable() else "?" for c in text)
    return "\n".join(text.splitlines()[-30:])


def check_free_port(number):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", number))
        except OSError as exc:
            raise SmokeError(f"Port {number} is unavailable; choose --port. No existing process was stopped.") from exc


def listener_addresses(pid, number, proc_root=Path("/proc")):
    """Attest the child's actual socket, not an unrelated listener on that port."""
    try:
        inodes = set()
        for descriptor in (proc_root / str(pid) / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        matches = []
        for table in ("tcp", "tcp6"):
            for line in (proc_root / str(pid) / "net" / table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and fields[9] in inodes:
                    address, encoded_port = fields[1].split(":")
                    if int(encoded_port, 16) == number:
                        matches.append((table, address))
        loopback_hex = f"{int.from_bytes(bytes((127, 0, 0, 1)), sys.byteorder):08X}"
        if any(item != ("tcp", loopback_hex) for item in matches):
            raise SmokeError("Dashboard listener is not exclusively IPv4 loopback")
        return bool(matches)
    except (OSError, IndexError, ValueError) as exc:
        raise SmokeError("Cannot verify dashboard listener ownership via Linux /proc; refusing to claim a safe bind") from exc


@contextmanager
def probe_deadline():
    def expired(*_):
        raise SmokeError("Dashboard probe exceeded its 3-second overall deadline")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def probe(number, path="/api/health", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", number, timeout=2)
    try:
        with probe_deadline():
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise SmokeError("Dashboard probe response exceeded 1 MiB")
        if any(k.lower() == "access-control-allow-origin" and v.strip() == "*" for k, v in response.getheaders()):
            raise SmokeError("Unexpected wildcard CORS header")
        return response.status, body
    finally:
        connection.close()


def check_dashboard(number):
    status, body = probe(number)
    if status != 200 or json.loads(body).get("status") != "ok":
        raise SmokeError("Dashboard health check failed")
    status, body = probe(number, "/")
    if status != 200 or b"<html" not in body.lower():
        raise SmokeError("Dashboard root HTML check failed")
    if probe(number, headers={"Host": "outside.example"})[0] != 400:
        raise SmokeError("Unexpected hostile Host behavior (expected 400)")
    if probe(number, headers={"Origin": "https://outside.example"})[0] != 403:
        raise SmokeError("Unexpected hostile Origin behavior (expected 403)")
    if probe(number, headers={"Origin": f"http://127.0.0.1:{number}"})[0] != 200:
        raise SmokeError("Same-origin request failed")


def metrics(pid):
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        rss = int(re.search(r"^VmRSS:\s+(\d+)", status, re.M).group(1)) / 1024
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        return f"PID {pid}: RSS {rss:.1f} MiB; cumulative CPU {cpu:.2f}s"
    except (OSError, ValueError, AttributeError, IndexError):
        return f"PID {pid}: resource metrics unavailable"


def serve(command, state, env, log, number, checks_only):
    check_free_port(number)
    process = start_child(command, state, env, log)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise SmokeError("Dashboard exited before readiness")
            if listener_addresses(process.pid, number):
                try:
                    if probe(number)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
            if time.monotonic() >= deadline:
                raise SmokeError("Dashboard readiness exceeded 30 seconds")
            time.sleep(0.2)
        check_dashboard(number)
        print("PASS: loopback/PID verified; health/root=200, hostile Host=400, hostile Origin=403; no wildcard CORS.", flush=True)
        print(metrics(process.pid), flush=True)
        if checks_only:
            return
        print(f"Dashboard ready on remote 127.0.0.1:{number}. Keep this terminal open; Ctrl+C stops it.", flush=True)
        print("On your LOCAL browser machine run scripts/smoke-tunnel.sh (default local port 18501).", flush=True)
        while True:
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print(metrics(process.pid), flush=True)
                continue
            raise SmokeError(f"Dashboard exited unexpectedly (exit {code})")
    finally:
        stop_child(process)


def require_clean_result(result):
    if result is None:
        raise SmokeError("Source returned no fresh data")
    if isinstance(result, dict):
        for key, value in result.items():
            if key in ("error", "_error", "errors", "_stale", "degraded") and value:
                raise SmokeError("Source returned an error, degraded or stale result")
            if isinstance(value, (dict, list)):
                require_clean_result(value)
    elif isinstance(result, list):
        for value in result:
            require_clean_result(value)


def finite(value, low, high):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def validate_result(name, result, *, status_providers=None):
    require_clean_result(result)
    if not isinstance(result, dict):
        raise SmokeError("Source result must be an object")
    if name == "earthquakes":
        rows = result.get("earthquakes")
        if result.get("source") != "usgs" or not isinstance(rows, list) or not rows or type(result.get("count")) is not int or result["count"] != len(rows):
            raise SmokeError("Earthquake result is empty or malformed")
        for row in rows:
            if (not isinstance(row, dict) or not row.get("id") or not row.get("place") or not finite(row.get("magnitude"), 2.5, 12)
                    or not finite(row.get("latitude"), -90, 90) or not finite(row.get("longitude"), -180, 180)):
                raise SmokeError("Implausible earthquake fields")
            when = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if not now - timedelta(hours=25) <= when <= now + timedelta(minutes=5):
                raise SmokeError("Earthquake timestamp outside requested fresh window")
    elif name == "forex":
        rates = result.get("rates", {})
        if result.get("source") not in ("ecb-forex", "frankfurter") or result.get("base") != "USD" or not isinstance(rates, dict) or any(not finite(rates.get(symbol), 0.000001, 10000) for symbol in ("EUR", "GBP")):
            raise SmokeError("Missing/implausible EUR/GBP forex rates")
        when = date.fromisoformat(result["date"])
        if not date.today() - timedelta(days=10) <= when <= date.today() + timedelta(days=1):
            raise SmokeError("Forex reference date is too old or in the future")
    elif name == "service_status":
        if status_providers is None:
            from world_intel_mcp.sources.service_status import _STATUS_FEEDS
            status_providers = {feed["provider"] for feed in _STATUS_FEEDS}
        expected = set(status_providers)
        rows = result.get("incidents")
        if (result.get("source") != "service-status" or set(result.get("providers_checked", [])) != expected
                or not isinstance(rows, list) or type(result.get("count")) is not int or result["count"] != len(rows)
                or result.get("unavailable_providers")):
            raise SmokeError("Not all service-status providers returned fresh feeds")
        for row in rows:
            if not isinstance(row, dict) or row.get("provider") not in expected or not row.get("title") or urlsplit(row.get("link", "")).scheme not in ("http", "https"):
                raise SmokeError("Malformed service-status incident")
        if result.get("by_provider") != dict(Counter(row["provider"] for row in rows)):
            raise SmokeError("Service-status provider counts disagree")
        if type(result.get("active_incidents")) is not int or not 0 <= result["active_incidents"] <= len(rows):
            raise SmokeError("Invalid active incident count")
    else:
        raise SmokeError("Unknown smoke source")


def verify_worker_environment():
    state = check_directory(os.environ["_WORLD_INTEL_SMOKE_STATE"], private=True)
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != state / "venv":
        raise SmokeError("Worker must run in the fresh dedicated smoke venv")
    if any(importlib.util.find_spec(module) is not None for module in FORBIDDEN):
        raise SmokeError("Unexpected vector/PDF/model dependency in smoke venv")
    if os.environ.get("WORLD_INTEL_CACHE_DB") != str(state / "cache/dashboard.db"):
        raise SmokeError("Unexpected dashboard cache path")
    from dotenv import load_dotenv
    sentinel = state / "dotenv-check.env"
    sentinel.write_text("WORLD_INTEL_SMOKE_DOTENV_SENTINEL=must-not-load\n")
    if load_dotenv(sentinel, override=True) or "WORLD_INTEL_SMOKE_DOTENV_SENTINEL" in os.environ:
        raise SmokeError("Installed python-dotenv does not honor PYTHON_DOTENV_DISABLED; refusing app import")
    return state


async def strong_live_checks(state):
    from world_intel_mcp.cache import Cache
    from world_intel_mcp.circuit_breaker import CircuitBreaker
    from world_intel_mcp.fetcher import Fetcher
    from world_intel_mcp.feed_parser import parse_feed_content
    from world_intel_mcp.sources import forex, seismology, service_status

    class CheckedFetcher(Fetcher):
        xml_count = 0
        async def get_json(self, *args, **kwargs):
            data = await super().get_json(*args, **kwargs)
            require_clean_result(data)
            return data

        async def get_xml(self, *args, **kwargs):
            data = await super().get_xml(*args, **kwargs)
            require_clean_result(data)
            parsed = parse_feed_content(data)
            if not parsed.version.startswith(("rss", "atom")) or not parsed.feed.get("title"):
                raise SmokeError("Provider returned a non-feed/invalid response")
            self.xml_count += 1
            return data

    tasks = [
        ("earthquakes", lambda f: seismology.fetch_earthquakes(f, min_magnitude=2.5, hours=24, limit=5)),
        ("forex", lambda f: forex.fetch_forex_rates(f, base="USD", symbols="EUR,GBP")),
        ("service_status", service_status.fetch_service_status),
    ]
    async with asyncio.timeout(60):
        for name, call in tasks:
            print(f"Checking fresh {name}...", flush=True)
            path = state / "cache" / f"fresh-{name}.db"
            if path.exists():
                raise SmokeError("Fresh-check cache already exists; refusing reuse")
            cache = Cache(db_path=path)
            fetcher = CheckedFetcher(cache, CircuitBreaker(), default_timeout=10, max_retries=0, processing_timeout=20)
            try:
                if Path(cache.db_path).resolve() != path:
                    raise SmokeError("Cache fell back outside the requested fresh state")
                result = await call(fetcher)
                validate_result(name, result)
                if name == "service_status" and fetcher.xml_count != len(service_status._STATUS_FEEDS):
                    raise SmokeError("Not all provider feeds were verified")
                print(f"PASS: fresh plausible {name} (no stale/error/degraded result)", flush=True)
            finally:
                try:
                    await fetcher.close()
                finally:
                    cache.close()


def worker(stage, number):
    state = verify_worker_environment()
    if stage == "verify":
        print("PASS: isolated venv; no vector/PDF/model packages; dotenv loading disabled", flush=True)
    elif stage == "live":
        asyncio.run(strong_live_checks(state))
    else:
        from world_intel_mcp.dashboard.app import run
        run(host="127.0.0.1", port=number)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fresh isolated headless smoke run, then loopback dashboard. Performs LIVE public network requests and PyPI installation.")
    parser.add_argument("--port", type=port, default=8501)
    parser.add_argument("--checks-only", action="store_true", help="stop dashboard after checks instead of waiting for Ctrl+C")
    parser.add_argument("--_stage", choices=("verify", "live", "dashboard"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Python >=3.11 is required; select an installed interpreter with SMOKE_PYTHON")
    if args._stage:
        worker(args._stage, args.port)
        return 0
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        raise SmokeError("Run this headless runner on Linux with readable /proc; use smoke-tunnel.sh on the local browser machine")
    def interrupted(*_):
        raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    check_free_port(args.port)
    ensure_system_venv()
    os.umask(0o077)
    print("LIVE NETWORK RUN: installs public PyPI wheels + this project; runs only earthquake, forex, service-status smoke tests.", flush=True)
    print("Fresh disposable venv/cache; no inherited credentials/.env; no collector/history/model installation. Browser use loads more public dashboard sources.", flush=True)
    check_directory(Path("/tmp"))
    with tempfile.TemporaryDirectory(prefix="world-intel-smoke-", dir="/tmp") as directory:
        state = check_directory(directory, private=True)
        env = clean_environment(state)
        logfile = state / "smoke.log"
        print(f"Private state/log (removed on exit): {state} / {logfile.name}", flush=True)
        with logfile.open("ab", buffering=0) as log:
            try:
                run_stage("Create venv (requires OS venv/ensurepip)", [sys.executable, "-I", "-m", "venv", str(state / "venv")], state, env, log, 120)
                python = str(state / "venv/bin/python")
                run_stage("Install dashboard + dev only", [python, "-I", "-m", "pip", "install", "--only-binary=:all:", "--no-cache-dir", "--retries", "1", "--timeout", "15", "--index-url", "https://pypi.org/simple", f"{ROOT}[dashboard,dev]", "python-dotenv>=1.2.0"], state, env, log, 600)
                child = [python, "-I", str(HELPER)]
                run_stage("Verify isolation/dotenv support", child + ["--_stage", "verify"], state, env, log, 30)
                test = str(ROOT / "src/world_intel_mcp/tests/test_smoke.py")
                run_stage("Three existing live tests", [python, "-I", "-m", "pytest", "--import-mode=importlib", "-o", "addopts=", "-m", "smoke", "-q", "--basetemp", str(state / "pytest"), *[test + "::test_live_" + name for name in ("earthquakes", "forex_rates", "service_status")]], state, env, log, 180)
                run_stage("Strict fresh-result verification (one attempt/source)", child + ["--_stage", "live"], state, env, log, 90)
                serve(child + ["--_stage", "dashboard", "--port", str(args.port)], state, env, log, args.port, args.checks_only)
            except KeyboardInterrupt:
                print("Stopped; child processes closed and private state will be removed.", flush=True)
                return 130
            except Exception:
                print("Smoke failed. Sanitized log tail (best effort):\n" + redacted_tail(logfile), file=sys.stderr)
                raise
    print("Completed; dashboard stopped and disposable state removed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped before smoke setup; no dashboard started.", file=sys.stderr)
        raise SystemExit(130)
    except (SmokeError, OSError, ValueError, KeyError, TimeoutError) as exc:
        print(f"Smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
