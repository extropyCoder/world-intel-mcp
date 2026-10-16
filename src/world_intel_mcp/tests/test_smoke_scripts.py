"""Offline stdlib tests; runnable without installing the project or pytest.

python3 -m unittest discover -s src/world_intel_mcp/tests -p test_smoke_scripts.py -v
"""

import contextlib
from collections import namedtuple
from datetime import date, datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
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
             patch.object(smoke, "ensure_system_venv"), patch.object(smoke.os, "umask"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(["--checks-only", "--port", "8502"]), 0)
        self.assertEqual(len(stages), 5)
        self.assertFalse(stages[0][2].exists())
        self.assertEqual(stages[0][1][:4], [sys.executable, "-I", "-m", "venv"])
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
             patch.object(smoke, "ensure_system_venv"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(smoke.SmokeError):
                smoke.main(["--checks-only"])
        self.assertEqual(len(states), 1)
        self.assertFalse(states[0].exists())
        self.assertTrue(ROOT.is_dir())

    def test_prerequisite_failure_occurs_before_disposable_app_state(self):
        with patch.object(smoke, "check_free_port"), patch.object(smoke.signal, "signal"), \
             patch.object(smoke, "ensure_system_venv", side_effect=smoke.SmokeError("missing prerequisite")), \
             patch.object(smoke.tempfile, "TemporaryDirectory") as state, patch.object(smoke, "run_stage") as run:
            with self.assertRaisesRegex(smoke.SmokeError, "missing prerequisite"):
                smoke.main(["--checks-only"])
        state.assert_not_called()
        run.assert_not_called()

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
    def invoke(self, script, args, env=None, cwd=None):
        return subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=env,
                              cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

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

    def test_runner_help_from_scripts_cwd_and_spaced_checkout(self):
        result = self.invoke("smoke-test.sh", ["--help"],
                             {"PATH": os.defpath, "SMOKE_PYTHON": sys.executable}, cwd=ROOT / "scripts")
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory(prefix="smoke checkout with spaces ") as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            for name in ("smoke-test.sh", "smoke_test.py"):
                (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            result = subprocess.run(["bash", str(scripts / "smoke-test.sh"), "--help"],
                                    cwd=scripts, env={"PATH": os.defpath, "SMOKE_PYTHON": sys.executable},
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


class PrerequisiteTests(unittest.TestCase):
    """All interpreter/apt/sudo execution is faked; no system command is run."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        version = namedtuple("Version", "major minor micro releaselevel serial")(3, 14, 4, "final", 0)
        stack.enter_context(patch.object(smoke.sys, "version_info", version))
        stack.enter_context(patch.object(smoke.sys, "executable", "/selected/python3.14"))
        stack.enter_context(patch.object(smoke.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.ready = stack.enter_context(patch.object(smoke, "venv_prerequisites_ready", side_effect=[False, True]))
        self.family = stack.enter_context(patch.object(smoke, "debian_family", return_value=True))
        self.uid = stack.enter_context(patch.object(smoke.os, "geteuid", return_value=1000))
        self.confirm = stack.enter_context(patch("builtins.input", return_value="yes"))
        self.tools = stack.enter_context(patch.object(smoke, "trusted_system_tool", side_effect=lambda name: "/usr/bin/" + name))
        self.query = stack.enter_context(patch.object(smoke, "query_system_tool", side_effect=[
            "python3.14-venv:\n  Candidate: 3.14.4-1\n",
            "Inst python3.14-venv (3.14.4-1 Debian)\nInst python3-pip-whl (26.0 Debian)\nConf python3.14-venv (3.14.4-1 Debian)\n",
        ]))
        self.install = stack.enter_context(patch.object(smoke, "install_system_package"))

    def test_accepted_exact_numeric_package_only_via_sudo(self):
        smoke.ensure_system_venv()
        self.assertEqual(self.install.call_args.args[0], [
            "/usr/bin/sudo", "--", "/usr/bin/apt-get", "install", "--no-install-recommends",
            "--no-upgrade", "--no-remove", "-y", "--", "python3.14-venv",
        ])
        self.assertEqual(self.query.call_args_list[0].args[0], ["/usr/bin/apt-cache", "policy", "python3.14-venv"])
        self.assertIn("--simulate", self.query.call_args_list[1].args[0])
        self.assertEqual(self.ready.call_count, 2)
        self.assertEqual(smoke.sys.executable, "/selected/python3.14")

    def test_root_still_needs_confirmation(self):
        self.uid.return_value = 0
        self.confirm.return_value = ""
        with self.assertRaisesRegex(smoke.SmokeError, "declined"):
            smoke.ensure_system_venv()
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_root_accepted_command_is_only_apt(self):
        self.uid.return_value = 0
        smoke.ensure_system_venv()
        self.assertEqual(self.install.call_args.args[0][:2], ["/usr/bin/apt-get", "install"])
        self.assertNotIn("sudo", [call.args[0] for call in self.tools.call_args_list])

    def test_default_decline_and_no_leave_zero_apt_calls(self):
        for answer in ("", "n", "no", "anything-else"):
            with self.subTest(answer=answer):
                self.ready.side_effect = [False]
                self.confirm.return_value = answer
                with self.assertRaisesRegex(smoke.SmokeError, "declined"):
                    smoke.ensure_system_venv()
        self.query.assert_not_called()
        self.install.assert_not_called()
        self.tools.assert_not_called()

    def test_noninteractive_never_prompts_or_runs_apt(self):
        with patch.object(smoke.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            with self.assertRaisesRegex(smoke.SmokeError, "Noninteractive"):
                smoke.ensure_system_venv()
        self.confirm.assert_not_called()
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_unsupported_platform_never_prompts_or_installs(self):
        self.family.return_value = False
        with self.assertRaisesRegex(smoke.SmokeError, "Debian/Ubuntu"):
            smoke.ensure_system_venv()
        self.confirm.assert_not_called()
        self.install.assert_not_called()

    def test_missing_sudo_fails_without_apt_actions(self):
        def tools(name):
            if name == "sudo":
                raise smoke.SmokeError("sudo unavailable")
            return "/usr/bin/" + name
        self.tools.side_effect = tools
        with self.assertRaisesRegex(smoke.SmokeError, "sudo unavailable"):
            smoke.ensure_system_venv()
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_unavailable_candidate_never_installs_or_updates(self):
        self.query.side_effect = ["python3.14-venv:\n Candidate: (none)\n"]
        with self.assertRaisesRegex(smoke.SmokeError, "No APT candidate"):
            smoke.ensure_system_venv()
        self.assertEqual(self.query.call_count, 1)
        self.install.assert_not_called()

    def test_recheck_failure_does_not_switch_python(self):
        self.ready.side_effect = [False, False]
        with self.assertRaisesRegex(smoke.SmokeError, "still lacks"):
            smoke.ensure_system_venv()
        self.install.assert_called_once()
        self.assertEqual(smoke.sys.executable, "/selected/python3.14")

    def test_approved_prerequisite_continues_original_isolated_venv_command(self):
        self.uid.return_value = os.getuid()
        commands = []
        def stage(label, command, *args):
            self.assertEqual(self.ready.call_count, 2)
            self.install.assert_called_once()
            commands.append(command)
        with patch.object(smoke, "check_free_port"), patch.object(smoke.signal, "signal"), \
             patch.object(smoke.os, "umask"), patch.object(smoke, "run_stage", stage), \
             patch.object(smoke, "serve"):
            self.assertEqual(smoke.main(["--checks-only"]), 0)
        self.assertEqual(commands[0][:4], ["/selected/python3.14", "-I", "-m", "venv"])

    def test_python_or_unrelated_upgrade_removal_plan_is_rejected(self):
        for plan in ("Inst python3.14 (3.14 Debian)", "Inst python3-pip-whl [25.0] (26.0 Debian)",
                     "Remv some-package [1.0]", "Conf unrelated (1.0 Debian)"):
            with self.subTest(plan=plan):
                self.ready.side_effect = [False]
                self.query.side_effect = ["Candidate: 3.14.4-1\n", plan]
                with self.assertRaises(smoke.SmokeError):
                    smoke.ensure_system_venv()
        self.install.assert_not_called()

    def test_abort_at_prompt_runs_nothing(self):
        self.confirm.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            smoke.ensure_system_venv()
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_help_and_supported_ready_interpreter_never_install(self):
        with self.assertRaises(SystemExit) as exited:
            smoke.main(["--help"])
        self.assertEqual(exited.exception.code, 0)
        self.ready.assert_not_called()
        self.ready.side_effect = [True]
        smoke.ensure_system_venv()
        self.confirm.assert_not_called()
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_unsupported_python_never_probes_or_installs(self):
        with patch.object(smoke.sys, "version_info", (3, 10)), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exited:
                smoke.main(["--checks-only"])
        self.assertEqual(exited.exception.code, 2)
        self.ready.assert_not_called()
        self.install.assert_not_called()


class SystemToolBoundaryTests(unittest.TestCase):
    def test_debian_ubuntu_detection_uses_readonly_os_release(self):
        for release, expected in (({"ID": "debian"}, True), ({"ID": "ubuntu"}, True),
                                  ({"ID": "derivative", "ID_LIKE": "ubuntu debian"}, True),
                                  ({"ID": "fedora", "ID_LIKE": "rhel"}, False)):
            with self.subTest(release=release), patch.object(smoke.platform, "freedesktop_os_release", return_value=release):
                self.assertEqual(smoke.debian_family(), expected)
        with patch.object(smoke.platform, "freedesktop_os_release", side_effect=OSError("unavailable")):
            self.assertFalse(smoke.debian_family())

    def test_probe_uses_original_python_isolated_without_bootstrap_or_install(self):
        with patch.object(smoke.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(smoke.venv_prerequisites_ready())
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-I", "-c"])
        self.assertNotIn("bootstrap()", command[3])
        self.assertNotIn("PIP_INDEX_URL", run.call_args.kwargs["env"])

    def test_readonly_apt_queries_use_arrays_clean_env_and_deadline(self):
        command = ["/usr/bin/apt-cache", "policy", "python3.14-venv"]
        with patch.object(smoke.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="Candidate: 1")) as run:
            self.assertEqual(smoke.query_system_tool(command), "Candidate: 1")
        self.assertEqual(run.call_args.args, (command,))
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("APT_CONFIG", run.call_args.kwargs["env"])

    def test_numeric_package_name_rejects_non_numeric_version(self):
        with patch.object(smoke.sys, "version_info", (3, "14;unsafe")):
            with self.assertRaises(smoke.SmokeError):
                smoke.venv_package_name()

    def test_trusted_tool_ignores_path_and_rejects_unsafe_permissions(self):
        target = Path("/usr/bin/apt-get")
        def info(path, *args, **kwargs):
            return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG if path == target else stat.S_IFDIR) | 0o755)
        with patch.object(Path, "resolve", return_value=target), patch.object(Path, "stat", info), patch.object(smoke.os, "access", return_value=True):
            self.assertEqual(smoke.trusted_system_tool("apt-get"), str(target))
        with patch.object(Path, "resolve", return_value=target), patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o777)):
            with self.assertRaises(smoke.SmokeError):
                smoke.trusted_system_tool("apt-get")
        with patch.object(Path, "resolve", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(smoke.SmokeError, "/usr/bin/sudo"):
                smoke.trusted_system_tool("sudo")

    def test_install_interrupt_forwards_signal_and_waits_without_orphan(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt(), 130]
        process.poll.return_value = None
        command = ["/usr/bin/sudo", "--", "/usr/bin/apt-get", "install", "-y", "--", "python3.14-venv"]
        with patch.object(smoke.subprocess, "Popen", return_value=process) as popen, patch.object(smoke.signal, "signal"):
            with self.assertRaises(KeyboardInterrupt):
                smoke.install_system_package(command)
        self.assertEqual(popen.call_args.args, (command,))
        self.assertEqual(popen.call_args.kwargs["cwd"], "/")
        self.assertEqual(popen.call_args.kwargs["umask"], 0o022)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(process.wait.call_count, 2)

    def test_system_install_failure_is_nonzero_and_not_continued(self):
        process = Mock()
        process.wait.return_value = 100
        with patch.object(smoke.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(smoke.SmokeError, "exit 100"):
                smoke.install_system_package(["/usr/bin/apt-get", "install", "--", "python3.14-venv"])


if __name__ == "__main__":
    unittest.main()
