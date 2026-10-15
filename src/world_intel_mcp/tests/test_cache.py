"""Tests for SQLite TTL cache."""

import os
import socket
import sqlite3
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from world_intel_mcp import cache as cache_module
from world_intel_mcp.cache import Cache, CachePathError


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(db_path=tmp_path / "test_cache.db")


def test_set_and_get(cache: Cache) -> None:
    cache.set("key1", {"value": 42}, ttl_seconds=60)
    result = cache.get("key1")
    assert result == {"value": 42}


def test_get_missing_key(cache: Cache) -> None:
    assert cache.get("nonexistent") is None


def test_ttl_expiration(cache: Cache) -> None:
    cache.set("ephemeral", "data", ttl_seconds=1)
    assert cache.get("ephemeral") == "data"
    time.sleep(1.1)
    assert cache.get("ephemeral") is None


def test_overwrite(cache: Cache) -> None:
    cache.set("key", "v1", ttl_seconds=60)
    cache.set("key", "v2", ttl_seconds=60)
    assert cache.get("key") == "v2"


def test_delete(cache: Cache) -> None:
    cache.set("key", "val", ttl_seconds=60)
    cache.delete("key")
    assert cache.get("key") is None


def test_evict_expired(cache: Cache) -> None:
    cache.set("fresh", "yes", ttl_seconds=300)
    cache.set("stale", "no", ttl_seconds=1)
    time.sleep(1.1)
    removed = cache.evict_expired()
    assert removed == 1
    assert cache.get("fresh") == "yes"
    assert cache.get("stale") is None


def test_stats(cache: Cache) -> None:
    cache.set("a", 1, ttl_seconds=300)
    cache.set("b", 2, ttl_seconds=300)
    stats = cache.stats()
    assert stats["total_entries"] == 2
    assert stats["active_entries"] == 2


def test_complex_values(cache: Cache) -> None:
    data = {"nested": {"list": [1, 2, 3], "bool": True, "null": None}}
    cache.set("complex", data, ttl_seconds=60)
    assert cache.get("complex") == data


def test_default_path_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "custom-cache.db"
    monkeypatch.setenv("WORLD_INTEL_CACHE_DB", str(db_path))

    cache = Cache()
    try:
        assert cache.db_path == db_path
        cache.set("env", "ok", ttl_seconds=60)
        assert cache.get("env") == "ok"
    finally:
        cache.close()


def test_env_override_fails_closed_when_unsafe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad_path = tmp_path / "cache-dir"
    bad_path.mkdir()
    monkeypatch.setenv("WORLD_INTEL_CACHE_DB", str(bad_path))

    with pytest.raises(CachePathError, match="regular file"):
        Cache()


def test_cache_db_file_is_owner_only(tmp_path: Path) -> None:
    """Issue #21 hardening: whatever sits in the cache gets served as
    intelligence (stale fallback included), so the file must not be
    writable — or readable — by other local users."""
    db = tmp_path / "perms" / "cache.db"
    cache = Cache(db)
    try:
        mode = db.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        cache.close()


@pytest.fixture
def isolated_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("WORLD_INTEL_CACHE_DB", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    default = home / ".cache" / "world-intel-mcp" / "cache.db"
    monkeypatch.setattr(cache_module, "_DEFAULT_DB", default)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    temp = tmp_path / "temp"
    temp.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    return default, temp


def _unavailable_home(monkeypatch, default):
    real_mkdir = os.mkdir

    def mkdir(path, *args, **kwargs):
        if path == ".cache":
            raise PermissionError("home cache unavailable")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "mkdir", mkdir)


def test_default_creation_is_private_before_sqlite(tmp_path, monkeypatch, isolated_defaults):
    default, _ = isolated_defaults
    real_connect = sqlite3.connect
    observed = []

    def connect(*args, **kwargs):
        # New files must already be protected, not chmodded after SQLite opens.
        assert default.stat().st_mode & 0o777 == 0o600
        assert default.parent.stat().st_mode & 0o777 == 0o700
        assert default.parent.parent.stat().st_mode & 0o777 == 0o700
        observed.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(cache_module.sqlite3, "connect", connect)
    old_umask = os.umask(0)
    try:
        cache = Cache()
        try:
            cache.set("private", "data", 60)
            for path in (default, Path(str(default) + "-wal"), Path(str(default) + "-shm")):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert observed == [default.as_uri() + "?mode=rw"]
        finally:
            cache.close()
    finally:
        os.umask(old_umask)


def test_fallback_is_private_stable_and_ignores_old_shared_location(isolated_defaults, monkeypatch):
    default, temp = isolated_defaults
    _unavailable_home(monkeypatch, default)
    old = temp / "world-intel-mcp"
    old.mkdir()
    poison = old / "cache.db"
    poison.write_text("attacker-controlled old location")
    expected = temp / f"world-intel-mcp-{os.geteuid()}" / "cache.db"
    first = Cache()
    try:
        assert first.db_path == expected
        first.set("fallback", "persisted", 60)
    finally:
        first.close()
    second = Cache()
    try:
        assert second.db_path == expected
        assert second.get("fallback") == "persisted"
        assert stat.S_IMODE(expected.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(expected.stat().st_mode) == 0o600
        assert poison.read_text() == "attacker-controlled old location"
    finally:
        second.close()


@pytest.mark.parametrize("target", ["database", "directory", "ancestor", "wal", "shm", "journal"])
def test_symlinks_rejected_before_sqlite_or_chmod(tmp_path, monkeypatch, target):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    victim = real / "victim"
    victim.write_text("do not touch")
    victim.chmod(0o644)
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    db = parent / "cache.db"
    if target == "database":
        db.symlink_to(victim)
    elif target == "directory":
        db = tmp_path / "linked" / "cache.db"
        db.parent.symlink_to(real, target_is_directory=True)
    elif target == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        db = alias / "nested" / "cache.db"
    else:
        Path(str(db) + "-" + target).symlink_to(victim)
    connect = Mock(side_effect=AssertionError("SQLite must not open unsafe paths"))
    monkeypatch.setattr(cache_module.sqlite3, "connect", connect)
    with pytest.raises(CachePathError):
        Cache(db)
    connect.assert_not_called()
    assert victim.read_text() == "do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket", "hardlink"])
def test_nonregular_or_hardlinked_database_refused(tmp_path, kind):
    db = tmp_path / "cache.db"
    sock = None
    if kind == "directory":
        db.mkdir()
    elif kind == "fifo":
        os.mkfifo(db)
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(db))
    else:
        other = tmp_path / "other"
        other.write_text("unchanged")
        db.hardlink_to(other)
    try:
        with pytest.raises(CachePathError):
            Cache(db)
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.parametrize("target", ["database", "directory", "wal", "shm", "journal"])
def test_wrong_owner_is_not_opened_or_repaired(tmp_path, monkeypatch, target):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    db = parent / "cache.db"
    victim = parent if target == "directory" else db if target == "database" else Path(str(db) + "-" + target)
    if victim != parent:
        victim.write_bytes(b"")
    real_stat = os.stat
    inode = victim.stat().st_ino

    def wrong_owner(*args, **kwargs):
        info = real_stat(*args, **kwargs)
        if info.st_ino == inode:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1,
                                   st_nlink=info.st_nlink, st_ino=info.st_ino, st_dev=info.st_dev)
        return info

    monkeypatch.setattr(cache_module.os, "stat", wrong_owner)
    connect = Mock(side_effect=AssertionError("unsafe ownership reached SQLite"))
    monkeypatch.setattr(cache_module.sqlite3, "connect", connect)
    with pytest.raises(CachePathError, match="ownership"):
        Cache(db)
    connect.assert_not_called()


@pytest.mark.parametrize("target", ["database", "directory", "ancestor", "wal", "shm", "journal"])
def test_shared_writable_paths_refused_without_chmod(tmp_path, target):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    db = parent / "cache.db"
    if target == "ancestor":
        db = parent / "nested" / "cache.db"
        parent.chmod(0o777)
        unsafe = parent
    elif target == "directory":
        parent.chmod(0o770)
        unsafe = parent
    else:
        unsafe = db if target == "database" else Path(str(db) + "-" + target)
        unsafe.write_bytes(b"")
        unsafe.chmod(0o666)
    mode = stat.S_IMODE(unsafe.stat().st_mode)
    with pytest.raises(CachePathError, match="writable"):
        Cache(db)
    assert stat.S_IMODE(unsafe.stat().st_mode) == mode


@pytest.mark.parametrize("unsafe", ["symlink", "wrong-owner", "writable"])
def test_poisoned_fallback_is_refused(isolated_defaults, monkeypatch, unsafe):
    default, temp = isolated_defaults
    _unavailable_home(monkeypatch, default)
    directory = temp / f"world-intel-mcp-{os.geteuid()}"
    if unsafe == "symlink":
        directory.symlink_to(default.parent.parent.parent, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        if unsafe == "writable":
            directory.chmod(0o777)
        else:
            real_stat = os.stat
            inode = directory.stat().st_ino

            def wrong_owner(*args, **kwargs):
                info = real_stat(*args, **kwargs)
                if info.st_ino == inode:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
                return info

            monkeypatch.setattr(cache_module.os, "stat", wrong_owner)
    with pytest.raises(CachePathError):
        Cache()


def test_xdg_override_and_explicit_relative_path(tmp_path, monkeypatch, isolated_defaults):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    cache = Cache()
    try:
        assert cache.db_path == xdg / "world-intel-mcp" / "cache.db"
    finally:
        cache.close()
    monkeypatch.chdir(tmp_path)
    cache = Cache(Path("relative") / "cache.db")
    try:
        assert cache.db_path == tmp_path / "relative" / "cache.db"
    finally:
        cache.close()


def test_ordinary_owned_database_and_shared_aoi_access(tmp_path):
    from world_intel_mcp.analysis.aoi import AOIStore

    db = tmp_path / "existing.db"
    # AOI-first use creates an ordinary owned SQLite file, potentially 0644.
    store = AOIStore(db)
    store.close()
    db.chmod(0o644)
    cache = Cache(db)
    store = AOIStore(cache.db_path)
    try:
        cache.set("shared", "ok", 60)
        store.define("Home", 40.0, -80.0, 10.0)
        assert cache.get("shared") == "ok"
        assert store.list_all()[0]["name"] == "Home"
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        other = Cache(db)
        try:
            assert other.get("shared") == "ok"
        finally:
            other.close()
    finally:
        store.close()
        cache.close()


def test_owned_readable_parent_is_preserved_and_reopen_rechecks_leaf(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    db = parent / "cache.db"
    cache = Cache(db)
    cache.close()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    db.unlink()
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    db.symlink_to(victim)
    with pytest.raises(CachePathError):
        cache.get("key")
    assert victim.read_text() == "unchanged"


def test_existing_sidecar_modes_tightened_before_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    first = Cache(db)
    first.set("key", "value", 60)
    sidecars = [Path(str(db) + suffix) for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.chmod(0o644)
    connect = sqlite3.connect

    def checked_connect(*args, **kwargs):
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sidecars)
        return connect(*args, **kwargs)

    monkeypatch.setattr(cache_module.sqlite3, "connect", checked_connect)
    try:
        second = Cache(db)
        try:
            assert second.get("key") == "value"
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("change", ["unlink", "replace"])
def test_sidecar_lifecycle_during_verification_is_safe(tmp_path, monkeypatch, suffix, change):
    db = tmp_path / "cache.db"
    sidecar = Path(str(db) + suffix)
    sidecar.write_bytes(b"")
    sidecar.chmod(0o600)
    real_open = os.open

    def changing_open(name, flags, *args, **kwargs):
        if name == sidecar.name and not flags & os.O_CREAT:
            if change == "replace":
                sidecar.unlink()
                sidecar.write_bytes(b"new")
                sidecar.chmod(0o600)
            fd = real_open(name, flags, *args, **kwargs)
            if change == "unlink":
                sidecar.unlink()
            return fd
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "open", changing_open)
    with cache_module._secure_cache_file(db, private_dir=False):
        assert db.exists()
    assert sidecar.exists() == (change == "replace")


@pytest.mark.parametrize("iteration", range(5))
def test_parallel_first_opens_share_database(tmp_path, iteration):
    db = tmp_path / "parallel" / "cache.db"

    def open_cache(i):
        cache = Cache(db)
        try:
            cache.set(f"key-{i}", i, 60)
        finally:
            cache.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(open_cache, range(12)))
    cache = Cache(db)
    try:
        assert [cache.get(f"key-{i}") for i in range(12)] == list(range(12))
    finally:
        cache.close()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_wal_initialization_retries_only_lock_contention(tmp_path, monkeypatch, code):
    real_connect = sqlite3.connect
    attempts = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.conn = real_connect(*args, **kwargs)

        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = code
                    raise error
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(cache_module.sqlite3, "connect", Connection)
    sleep = Mock()
    monkeypatch.setattr(cache_module.time, "sleep", sleep)
    cache = Cache(tmp_path / "cache.db")
    try:
        assert len(attempts) == 2
        sleep.assert_called_once_with(0.01)
        cache.set("key", "ok", 60)
        assert cache.get("key") == "ok"
    finally:
        cache.close()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_READONLY])
def test_wal_initialization_retry_is_bounded_and_closes_on_error(tmp_path, monkeypatch, code):
    error = sqlite3.OperationalError("initialization failed")
    error.sqlite_errorcode = code
    connection = Mock()
    connection.execute.side_effect = [None, error]
    monkeypatch.setattr(cache_module.sqlite3, "connect", Mock(return_value=connection))
    monotonic = Mock(side_effect=[0, 6])
    monkeypatch.setattr(cache_module.time, "monotonic", monotonic)
    with pytest.raises(sqlite3.OperationalError, match="initialization failed"):
        Cache(tmp_path / "cache.db")
    assert connection.execute.call_count == 2
    connection.close.assert_called_once()
