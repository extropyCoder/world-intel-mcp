"""Offline stdlib tests; runnable without installing the project or pytest.

python3 -m unittest discover -s src/world_intel_mcp/tests -p test_smoke_scripts.py -v
"""

import contextlib
from datetime import date, datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("smoke_script_under_test", ROOT / "scripts/smoke_test.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeScriptTests(unittest.TestCase):
    def test_port_validation(self):
        for value in ("0", "65536", "-1", "1;echo", "abc", "999999", ""):
            with self.subTest(value=value), self.assertRaises(Exception):
                smoke.port(value)
        self.assertEqual(smoke.port("08501"), 8501)

    def test_private_environment_strips_credentials_and_user_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            with patch.dict(os.environ, {"NASA_FIRMS_API_KEY": "secret", "HTTP_PROXY": "secret", "PIP_INDEX_URL": "secret", "PYTHONPATH": "secret"}):
                env = smoke.clean_environment(state)
            self.assertFalse(any("secret" in value for value in env.values()))
            self.assertEqual(env["WORLD_INTEL_CACHE_DB"], str(state / "cache/dashboard.db"))
            self.assertEqual(env["PYTHON_DOTENV_DISABLED"], "1")
            self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
            self.assertEqual(env["OLLAMA_API_URL"], "smoke-disabled://ollama")

    def test_state_symlink_and_public_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "real").mkdir(mode=0o700)
            (parent / "link").symlink_to(parent / "real", target_is_directory=True)
            with self.assertRaises(smoke.SmokeError):
                smoke.check_directory(parent / "link", private=True)
            (parent / "real").chmod(0o777)
            with self.assertRaises(smoke.SmokeError):
                smoke.check_directory(parent / "real", private=True)

    def test_bootstrap_commands_are_scoped_and_state_is_removed(self):
        stages = []
        def stage(label, command, state, env, log, timeout):
            stages.append((label, command, state, env.copy(), timeout))
            self.assertLessEqual(timeout, 600)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        with patch.object(smoke, "run_stage", stage), patch.object(smoke, "serve") as serve, \
             patch.object(smoke, "check_free_port"), patch.object(smoke.signal, "signal"), \
             patch.object(smoke.os, "umask"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(["--checks-only", "--port", "8502"]), 0)
        self.assertEqual(len(stages), 5)
        self.assertFalse(stages[0][2].exists())
        installation = stages[1][1]
        self.assertIn(f"{ROOT}[dashboard,dev]", installation)
        self.assertIn("--only-binary=:all:", installation)
        self.assertFalse(any("vector" in arg or "pdf" in arg for arg in installation))
        tests = stages[3][1]
        self.assertEqual(sum("::test_live_" in arg for arg in tests), 3)
        self.assertTrue(any(arg.endswith("::test_live_forex_rates") for arg in tests))
        self.assertEqual(serve.call_args.args[-2:], (8502, True))

    def test_bootstrap_failure_removes_only_its_disposable_state(self):
        states = []
        def fail(label, command, state, *args):
            states.append(state)
            raise smoke.SmokeError("synthetic failure")
        with patch.object(smoke, "run_stage", fail), patch.object(smoke, "check_free_port"), \
             patch.object(smoke.signal, "signal"), patch.object(smoke.os, "umask"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(smoke.SmokeError):
                smoke.main(["--checks-only"])
        self.assertEqual(len(states), 1)
        self.assertFalse(states[0].exists())
        self.assertTrue(ROOT.is_dir())

    def test_stage_timeout_and_interrupt_cleanup(self):
        for error in (subprocess.TimeoutExpired("test", 1), KeyboardInterrupt()):
            process = Mock()
            process.wait.side_effect = error
            with self.subTest(error=type(error)), patch.object(smoke, "start_child", return_value=process), \
                 patch.object(smoke, "stop_child") as stop, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(smoke.SmokeError if isinstance(error, subprocess.TimeoutExpired) else KeyboardInterrupt):
                    smoke.run_stage("test", ["never-run"], Path("."), {}, io.BytesIO(), 1)
                stop.assert_called_once_with(process)

    def test_actual_child_process_group_is_stopped(self):
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            smoke.stop_child(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_server_cleanup_on_checks_only_failure_and_ctrl_c(self):
        for failure in (None, smoke.SmokeError("synthetic check failure"), KeyboardInterrupt()):
            process = Mock(pid=12345)
            process.poll.return_value = None
            with self.subTest(failure=type(failure)), patch.object(smoke, "check_free_port"), \
                 patch.object(smoke, "start_child", return_value=process), patch.object(smoke, "listener_addresses", return_value=True), \
                 patch.object(smoke, "probe", return_value=(200, b"{}")), \
                 patch.object(smoke, "check_dashboard", side_effect=failure), patch.object(smoke, "metrics", return_value="sample"), \
                 patch.object(smoke, "stop_child") as stop, contextlib.redirect_stdout(io.StringIO()):
                if failure is None:
                    smoke.serve([], Path("."), {}, None, 8501, True)
                else:
                    with self.assertRaises(type(failure)):
                        smoke.serve([], Path("."), {}, None, 8501, True)
                stop.assert_called_once_with(process)

    def test_listener_verification_is_pid_bound_and_rejects_wildcard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fd = root / "42/fd"
            net = root / "42/net"
            fd.mkdir(parents=True)
            net.mkdir()
            (fd / "3").symlink_to("socket:[900]")
            (net / "tcp6").write_text("header\n")
            def table(address, inode="900"):
                (net / "tcp").write_text(f"header\n0: {address}:2135 00000000:0000 0A 0:0 0:0 0 0 0 {inode}\n")
            loopback = f"{int.from_bytes(bytes((127, 0, 0, 1)), sys.byteorder):08X}"
            table(loopback)
            self.assertTrue(smoke.listener_addresses(42, 8501, root))
            table(loopback, "901")
            self.assertFalse(smoke.listener_addresses(42, 8501, root))
            table("00000000")
            with self.assertRaises(smoke.SmokeError):
                smoke.listener_addresses(42, 8501, root)
            with self.assertRaises(smoke.SmokeError):
                smoke.listener_addresses(99, 8501, root)

    def test_security_probes_and_wildcard_cors_failure(self):
        requests = []
        cors = []
        class Connection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, path, headers):
                requests.append((method, path, headers))
                self.path, self.headers = path, headers
            def getresponse(self):
                status = 400 if self.headers.get("Host") else 403 if self.headers.get("Origin", "").endswith("outside.example") else 200
                body = b"<html>dashboard</html>" if self.path == "/" else b'{"status":"ok"}'
                return SimpleNamespace(status=status, read=lambda size: body, getheaders=lambda: cors)
            def close(self):
                pass
        with patch.object(smoke.http.client, "HTTPConnection", Connection):
            smoke.check_dashboard(8501)
            self.assertEqual(len(requests), 5)
            cors.append(("Access-Control-Allow-Origin", "*"))
            with self.assertRaises(smoke.SmokeError):
                smoke.probe(8501)

    def test_fresh_shapes_reject_stale_errors_empty_and_old_forex(self):
        now = datetime.now(timezone.utc).isoformat()
        earthquakes = {"source": "usgs", "count": 1, "earthquakes": [{"id": "test", "place": "Test", "magnitude": 3.0, "latitude": 1.0, "longitude": 2.0, "time": now}]}
        smoke.validate_result("earthquakes", earthquakes)
        for bad in ({"_stale": True}, {"degraded": True}, {"error": "upstream"}, {"earthquakes": [], "count": 0, "source": "usgs"}):
            with self.subTest(bad=bad), self.assertRaises(smoke.SmokeError):
                smoke.validate_result("earthquakes", bad)
        forex = {"source": "ecb-forex", "base": "USD", "rates": {"EUR": 0.9, "GBP": 0.8}, "date": date.today().isoformat()}
        smoke.validate_result("forex", forex)
        forex["date"] = "2000-01-01"
        with self.assertRaises(smoke.SmokeError):
            smoke.validate_result("forex", forex)

    def test_dotenv_guard_fails_before_app_import(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            env = smoke.clean_environment(state)
            (state / "venv").mkdir()
            fake_dotenv = SimpleNamespace(load_dotenv=Mock(return_value=False))
            with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"dotenv": fake_dotenv}), \
                 patch.object(smoke.sys, "prefix", str(state / "venv")), \
                 patch.object(smoke.importlib.util, "find_spec", return_value=None):
                self.assertEqual(smoke.verify_worker_environment(), state)
                fake_dotenv.load_dotenv.return_value = True
                with self.assertRaises(smoke.SmokeError):
                    smoke.verify_worker_environment()

    def test_status_verification_allows_quiet_but_not_unavailable_providers(self):
        result = {"source": "service-status", "incidents": [], "count": 0, "active_incidents": 0,
                  "providers_checked": ["Test"], "by_provider": {}}
        smoke.validate_result("service_status", result, status_providers={"Test"})
        for extra in ({"providers_checked": []}, {"unavailable_providers": ["Test"]},
                      {"degraded": True}, {"active_incidents": 10}):
            with self.subTest(extra=extra), self.assertRaises(smoke.SmokeError):
                smoke.validate_result("service_status", {**result, **extra}, status_providers={"Test"})

    def test_log_tail_masks_labelled_credentials_urls_and_control_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "test.log"
            log.write_text("https://user:password@example.test/path?api_key=secret\nAuthorization: Bearer secret\n\x1b[31m")
            tail = smoke.redacted_tail(log)
            for forbidden in ("user:", "password@", "/path", "api_key=secret", "Bearer secret", "\x1b"):
                self.assertNotIn(forbidden, tail)


class ShellHelperTests(unittest.TestCase):
    def invoke(self, script, args, env=None):
        return subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_tunnel_exact_safe_ssh_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "ssh"
            fake.write_text(f"#!{sys.executable}\nimport json,sys\nprint('SSH_ARGS='+json.dumps(sys.argv[1:]))\n")
            fake.chmod(0o700)
            result = self.invoke("smoke-tunnel.sh", ["operator@headless.example", "--local-port", "08080"], {"PATH": directory + os.pathsep + os.defpath})
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(line for line in result.stdout.splitlines() if line.startswith("SSH_ARGS="))
            args = json.loads(line.split("=", 1)[1])
            self.assertEqual(args[-4:], ["-L", "127.0.0.1:8080:127.0.0.1:8501", "--", "operator@headless.example"])
            self.assertIn("-N", args)
            self.assertIn("-T", args)
            self.assertIn("ExitOnForwardFailure=yes", args)
            self.assertNotIn("StrictHostKeyChecking=no", args)
            self.assertIn("http://127.0.0.1:8080/", result.stdout)

    def test_tunnel_rejects_option_and_shell_injection_and_bad_ports(self):
        for args in (["-oProxyCommand=bad"], ["user@host;touch /tmp/bad"], ["user@-bad"],
                     ["host", "--local-port", "0"], ["host", "--remote-port", "65536"], ["host", "--remote-port"]):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke("smoke-tunnel.sh", args).returncode, 0)

    def test_runner_wrapper_quotes_python_and_args_without_eval(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "python with spaces"
            fake.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            fake.chmod(0o700)
            result = self.invoke("smoke-test.sh", ["--port", "8502"], {"PATH": os.defpath, "SMOKE_PYTHON": str(fake)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["-I", str(ROOT / "scripts/smoke_test.py"), "--port", "8502"])

    def test_help_and_invalid_runner_args_never_start_installation(self):
        result = self.invoke("smoke-test.sh", ["--help"], {"PATH": os.defpath, "SMOKE_PYTHON": sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LIVE public network", " ".join(result.stdout.split()))
        result = self.invoke("smoke-test.sh", ["--port", "0"], {"PATH": os.defpath, "SMOKE_PYTHON": sys.executable})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Install dashboard", result.stdout)


if __name__ == "__main__":
    unittest.main()
