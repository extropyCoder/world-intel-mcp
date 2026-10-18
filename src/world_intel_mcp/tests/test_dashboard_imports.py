"""Fresh processes, no real HOME/cache or network: focused import has no DB I/O."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("focused", [True, False])
def test_fresh_import_isolates_focused_mode_from_legacy_temporal_side_effect(tmp_path, focused):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    env = {"PATH": os.defpath, "HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
           "XDG_STATE_HOME": str(tmp_path / "xdg-state"), "WORLD_INTEL_CACHE_DB": str(tmp_path / "configured-cache.db"),
           "PYTHON_DOTENV_DISABLED": "1", "HF_HUB_OFFLINE": "1"}
    if focused:
        env["WORLD_INTEL_CONFIG"] = str(ROOT / "dashboard.example.toml")
    code = f"""
import sys, sqlite3, socket
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'src')!r})
def denied(*args, **kwargs): raise AssertionError('Unexpected focused import I/O')
socket.socket.connect=denied
socket.getaddrinfo=denied
if {focused!r}: sqlite3.connect=denied
from world_intel_mcp.dashboard import app
assert app._fetcher is None and app._vector_store is None
assert 'world_intel_mcp.vector_store' not in sys.modules
assert 'fastembed' not in sys.modules and 'qdrant_client' not in sys.modules
if {focused!r}:
    assert app._settings is not None
    from importlib import import_module
    from world_intel_mcp.dashboard.focused import REGISTRY
    for name in app._settings.sources:
        module, function = REGISTRY[name]
        assert callable(getattr(import_module('world_intel_mcp.sources.' + module), function))
    for module in ('world_intel_mcp.sources.intelligence','world_intel_mcp.analysis.temporal','world_intel_mcp.analysis.situation','world_intel_mcp.analysis.posture','world_intel_mcp.sources.fleet'):
        assert module not in sys.modules, module
    assert not list(Path({str(tmp_path)!r}).rglob('*.db*'))
else:
    assert app._settings is None
    assert Path.home().joinpath('.cache/world-intel-mcp/temporal.db').is_file()
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if focused:
        assert not list(tmp_path.rglob("*.db*"))
